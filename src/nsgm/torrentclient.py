"""Hand magnets and torrents to a torrent client, and watch them finish.

The app has no BitTorrent engine of its own. Handing a magnet to the
operating system works, but it throws the person out to another program
and the app loses sight of the download. qBittorrent and Transmission both
take orders over a local web API instead, so the client can run in the
background with its window closed:

* the magnet is added with the finished-downloads folder as its save
  location, so the folder watcher picks it up, and an automatic install
  follows when that is switched on
* progress is read back and shown in the app

qBittorrent needs its Web UI switched on (Tools, Options, Web UI). Its
"bypass authentication for clients on localhost" option makes the
username and password unnecessary. Transmission's remote access is the
same idea.
"""

from __future__ import annotations

import base64
import binascii
import http.cookiejar
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

USER_AGENT = "NonSteamGameManager/1.0 (+torrent client bridge)"
KINDS = ("system", "qbittorrent", "transmission")
LABELS = {"qbittorrent": "qBittorrent", "transmission": "Transmission"}


class TorrentClientError(RuntimeError):
    pass


def info_hash(magnet: str) -> str:
    """The hex info hash in a magnet link, converting base32 if needed."""
    found = re.search(r"(?i)urn:btih:([a-f0-9]{40}|[a-z2-7]{32})", magnet or "")
    if not found:
        return ""
    value = found.group(1)
    if len(value) == 32:
        try:
            return binascii.hexlify(base64.b32decode(value.upper())).decode()
        except (binascii.Error, ValueError):
            return ""
    return value.lower()


def _base(url: str, default_port: int) -> str:
    text = str(url or "").strip().rstrip("/")
    if not text:
        text = f"http://127.0.0.1:{default_port}"
    if not re.match(r"(?i)^https?://", text):
        text = "http://" + text
    return text


class QBittorrent:
    label = "qBittorrent"

    def __init__(self, url: str = "", username: str = "", password: str = "", timeout: int = 10):
        self.base = _base(url, 8080)
        self.username = username
        self.password = password
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.logged_in = False

    def _request(self, path: str, data: bytes | None = None, headers: dict | None = None,
                 retry: bool = True) -> str:
        request = urllib.request.Request(self.base + path, data=data,
                                         headers={"User-Agent": USER_AGENT, **(headers or {})})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403) and retry and not self.logged_in:
                self.login()
                return self._request(path, data, headers, retry=False)
            if exc.code in (401, 403):
                raise TorrentClientError("qBittorrent refused the username or password.") from exc
            raise TorrentClientError(f"qBittorrent answered HTTP {exc.code}.") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TorrentClientError(
                f"qBittorrent is not answering at {self.base}. Is it running, with its "
                "Web UI switched on?") from exc

    def login(self) -> None:
        body = urllib.parse.urlencode({"username": self.username,
                                       "password": self.password}).encode()
        answer = self._request("/api/v2/auth/login", body,
                               {"Content-Type": "application/x-www-form-urlencoded"},
                               retry=False)
        if not answer.strip().lower().startswith("ok"):
            raise TorrentClientError("qBittorrent refused the username or password.")
        self.logged_in = True

    def version(self) -> str:
        return self._request("/api/v2/app/version").strip()

    def add(self, magnet: str = "", torrent: bytes = b"", save_path: str = "",
            tag: str = "") -> None:
        boundary = "nsgm" + uuid.uuid4().hex
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\""
                         f"\r\n\r\n{value}\r\n".encode())

        if magnet:
            field("urls", magnet)
        if torrent:
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"torrents\"; "
                         f"filename=\"download.torrent\"\r\nContent-Type: "
                         f"application/x-bittorrent\r\n\r\n".encode() + torrent + b"\r\n")
        if save_path:
            field("savepath", save_path)
            field("autoTMM", "false")
        if tag:
            field("tags", tag)              # older versions ignore it, which is fine
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        answer = self._request("/api/v2/torrents/add", body,
                               {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        if "fail" in answer.lower():
            raise TorrentClientError("qBittorrent did not accept that torrent. It may "
                                     "already be in its list.")

    STATES = {
        "error": "error", "missingFiles": "error", "uploading": "done", "stalledUP": "done",
        "pausedUP": "done", "stoppedUP": "done", "queuedUP": "done", "forcedUP": "done",
        "checkingUP": "checking", "allocating": "checking", "downloading": "downloading",
        "forcedDL": "downloading", "metaDL": "fetching details", "pausedDL": "paused",
        "stoppedDL": "paused", "queuedDL": "queued", "stalledDL": "stalled",
        "checkingDL": "checking", "checkingResumeData": "checking", "moving": "moving",
    }

    def status(self, hashes: list[str]) -> list[dict]:
        if not hashes:
            return []
        query = urllib.parse.urlencode({"hashes": "|".join(hashes)})
        try:
            items = json.loads(self._request(f"/api/v2/torrents/info?{query}") or "[]")
        except ValueError as exc:
            raise TorrentClientError("qBittorrent sent something unreadable.") from exc
        out = []
        for item in items:
            out.append({
                "hash": str(item.get("hash", "")).lower(),
                "name": item.get("name", ""),
                "progress": float(item.get("progress") or 0),
                "state": self.STATES.get(item.get("state", ""), item.get("state", "")),
                "speed": int(item.get("dlspeed") or 0),
                "eta": int(item.get("eta") or 0),
                "size": int(item.get("size") or item.get("total_size") or 0),
                "path": item.get("content_path") or item.get("save_path", ""),
                "content": item.get("content_path") or "",
                "error": "",
            })
        return out

    def remove(self, hashes: list[str], delete_files: bool = False) -> None:
        body = urllib.parse.urlencode({"hashes": "|".join(hashes),
                                       "deleteFiles": "true" if delete_files else "false"}).encode()
        self._request("/api/v2/torrents/delete", body,
                      {"Content-Type": "application/x-www-form-urlencoded"})

    def files(self, info_hash: str) -> list[dict]:
        """The torrent's files, empty until its details have arrived."""
        query = urllib.parse.urlencode({"hash": info_hash})
        try:
            items = json.loads(self._request(f"/api/v2/torrents/files?{query}") or "[]")
        except ValueError as exc:
            raise TorrentClientError("qBittorrent sent something unreadable.") from exc
        return [{"index": int(item.get("index", position)), "name": item.get("name", ""),
                 "size": int(item.get("size") or 0)}
                for position, item in enumerate(items)]

    def skip(self, info_hash: str, indexes: list[int]) -> None:
        """Do not download these files of the torrent."""
        if not indexes:
            return
        body = urllib.parse.urlencode({"hash": info_hash,
                                       "id": "|".join(str(i) for i in indexes),
                                       "priority": "0"}).encode()
        self._request("/api/v2/torrents/filePrio", body,
                      {"Content-Type": "application/x-www-form-urlencoded"})


class Transmission:
    label = "Transmission"

    def __init__(self, url: str = "", username: str = "", password: str = "", timeout: int = 10):
        base = _base(url, 9091)
        self.endpoint = base if base.endswith("/rpc") else base + "/transmission/rpc"
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = ""

    def _call(self, method: str, arguments: dict, retry: bool = True) -> dict:
        body = json.dumps({"method": method, "arguments": arguments}).encode()
        headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
        if self.session:
            headers["X-Transmission-Session-Id"] = self.session
        if self.username:
            token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        request = urllib.request.Request(self.endpoint, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if exc.code == 409 and retry:
                # Transmission's guard against cross-site requests: the first
                # call is refused and names the session id to send back.
                self.session = exc.headers.get("X-Transmission-Session-Id", "")
                return self._call(method, arguments, retry=False)
            if exc.code == 401:
                raise TorrentClientError("Transmission refused the username or password.") from exc
            raise TorrentClientError(f"Transmission answered HTTP {exc.code}.") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise TorrentClientError(
                f"Transmission is not answering at {self.endpoint}. Is remote access on?") from exc
        if payload.get("result") != "success":
            raise TorrentClientError(f"Transmission said: {payload.get('result')}")
        return payload.get("arguments") or {}

    def version(self) -> str:
        return str(self._call("session-get", {"fields": ["version"]}).get("version", ""))

    def add(self, magnet: str = "", torrent: bytes = b"", save_path: str = "",
            tag: str = "") -> None:
        arguments: dict = {}
        if torrent:
            arguments["metainfo"] = base64.b64encode(torrent).decode()
        else:
            arguments["filename"] = magnet
        if save_path:
            arguments["download-dir"] = save_path
        if tag:
            arguments["labels"] = [tag]     # Transmission 3.00 and later; others ignore it
        try:
            self._call("torrent-add", arguments)
        except TorrentClientError:
            if "labels" not in arguments:
                raise
            del arguments["labels"]
            self._call("torrent-add", arguments)

    STATES = {0: "paused", 1: "checking", 2: "checking", 3: "queued", 4: "downloading",
              5: "done", 6: "done"}

    def status(self, hashes: list[str]) -> list[dict]:
        if not hashes:
            return []
        fields = ["hashString", "name", "percentDone", "status", "rateDownload", "eta",
                  "downloadDir", "totalSize", "error", "errorString",
                  "metadataPercentComplete", "peersSendingToUs"]
        found = self._call("torrent-get", {"fields": fields, "ids": hashes})
        out = []
        for item in found.get("torrents") or []:
            done = float(item.get("percentDone") or 0)
            state = self.STATES.get(int(item.get("status") or 0), "")
            if state == "paused" and done >= 1:
                state = "done"
            if state == "downloading":
                # Transmission has no states of its own for these two; a
                # magnet waiting for its details and a download nobody is
                # sending to both just say "downloading".
                if float(item.get("metadataPercentComplete", 1) or 0) < 1:
                    state = "fetching details"
                elif not item.get("peersSendingToUs") and not item.get("rateDownload"):
                    state = "stalled"
            # Errors 1 and 2 are a tracker warning and a tracker error. With
            # a magnet listing twenty trackers some are always down, and the
            # download carries on through the others; only 3, a local error
            # such as a full disk, stops it.
            failed = int(item.get("error") or 0) == 3
            out.append({
                "hash": str(item.get("hashString", "")).lower(),
                "name": item.get("name", ""),
                "progress": done,
                "state": "error" if failed else state,
                "speed": int(item.get("rateDownload") or 0),
                "eta": int(item.get("eta") or 0),
                "size": int(item.get("totalSize") or 0),
                "path": item.get("downloadDir", ""),
                # Transmission reports the folder it saves into; the
                # torrent's own file or folder is that plus its name.
                "content": os.path.join(item.get("downloadDir", ""), item.get("name", ""))
                           if item.get("downloadDir") and item.get("name") else "",
                "error": item.get("errorString", "") if failed else "",
            })
        return out

    def remove(self, hashes: list[str], delete_files: bool = False) -> None:
        self._call("torrent-remove", {"ids": hashes, "delete-local-data": bool(delete_files)})

    def files(self, info_hash: str) -> list[dict]:
        """The torrent's files, empty until its details have arrived."""
        found = self._call("torrent-get", {"fields": ["files"], "ids": [info_hash]})
        torrents = found.get("torrents") or []
        items = torrents[0].get("files") or [] if torrents else []
        return [{"index": position, "name": item.get("name", ""),
                 "size": int(item.get("length") or 0)} for position, item in enumerate(items)]

    def skip(self, info_hash: str, indexes: list[int]) -> None:
        """Do not download these files of the torrent."""
        if indexes:
            self._call("torrent-set", {"ids": [info_hash], "files-unwanted": list(indexes)})


class Aria2:
    """aria2, driven over its local JSON-RPC: the store's own, built-in engine.

    aria2 is started hidden by ``torrentengine`` and only ever serves the
    store, so every torrent in it is the store's. It downloads and stops:
    it is started with seeding switched off, and a magnet's download waits,
    paused, until the store has chosen which of its files are wanted.
    """

    label = "the built-in downloader"
    leech_only = True

    def __init__(self, port: int, secret: str, timeout: int = 10):
        self.endpoint = f"http://127.0.0.1:{port}/jsonrpc"
        self.base = self.endpoint
        self.secret = secret
        self.timeout = timeout

    def _call(self, method: str, *params):
        body = json.dumps({"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method,
                           "params": [f"token:{self.secret}", *params]}).encode()
        request = urllib.request.Request(self.endpoint, data=body, headers={
            "User-Agent": USER_AGENT, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8", "replace"))
            except (ValueError, OSError):
                raise TorrentClientError(f"The built-in downloader answered HTTP {exc.code}.") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise TorrentClientError("The built-in downloader is not answering.") from exc
        if payload.get("error"):
            raise TorrentClientError(
                f"The built-in downloader said: {payload['error'].get('message', 'error')}")
        return payload.get("result")

    def version(self) -> str:
        return str((self._call("aria2.getVersion") or {}).get("version", ""))

    def add(self, magnet: str = "", torrent: bytes = b"", save_path: str = "",
            tag: str = "") -> None:
        options = {"dir": save_path} if save_path else {}
        if torrent:
            # A magnet waits after its details by itself (--pause-metadata);
            # a .torrent has its details already, so it waits from the start.
            options["pause"] = "true"
            self._call("aria2.addTorrent", base64.b64encode(torrent).decode(), [], options)
        else:
            self._call("aria2.addUri", [magnet], options)

    KEYS = ["gid", "status", "totalLength", "completedLength", "downloadSpeed", "connections",
            "numSeeders", "infoHash", "dir", "bittorrent", "followedBy", "errorMessage", "files"]

    def _entries(self) -> list[dict]:
        found = list(self._call("aria2.tellActive", self.KEYS) or [])
        found += self._call("aria2.tellWaiting", 0, 1000, self.KEYS) or []
        found += self._call("aria2.tellStopped", 0, 1000, self.KEYS) or []
        return found

    @staticmethod
    def _is_metadata(entry: dict) -> bool:
        """The first stage of a magnet, which only fetches the torrent's details."""
        info = (entry.get("bittorrent") or {}).get("info")
        files = entry.get("files") or []
        first = str((files[0] if files else {}).get("path", ""))
        return not info or first.startswith("[METADATA]") or bool(entry.get("followedBy"))

    def _pick(self, info_hash: str, entries: list[dict] | None = None) -> tuple:
        """(the download itself or None, its details stage or None)."""
        info_hash = info_hash.lower()
        mine = [e for e in (entries if entries is not None else self._entries())
                if str(e.get("infoHash", "")).lower() == info_hash]
        real = [e for e in mine if not self._is_metadata(e) and e.get("status") != "removed"]
        meta = [e for e in mine if self._is_metadata(e)]
        return (real[0] if real else None), (meta[0] if meta else None)

    @staticmethod
    def _content(entry: dict) -> str:
        folder = entry.get("dir") or ""
        name = ((entry.get("bittorrent") or {}).get("info") or {}).get("name") or ""
        files = entry.get("files") or []
        if name:
            return os.path.join(folder, name)
        return str(files[0].get("path", "")) if len(files) == 1 else folder

    def status(self, hashes: list[str]) -> list[dict]:
        entries = self._entries()
        out = []
        for info_hash in hashes:
            real, meta = self._pick(info_hash, entries)
            if real is None and meta is None:
                continue
            if real is None:
                failed = meta.get("status") == "error"
                out.append({"hash": info_hash.lower(), "name": "", "progress": 0.0,
                            "state": "error" if failed else "fetching details", "speed": 0,
                            "eta": 0, "size": 0, "path": meta.get("dir", ""), "content": "",
                            "error": meta.get("errorMessage", "") if failed else ""})
                continue
            total = int(real.get("totalLength") or 0)
            done = int(real.get("completedLength") or 0)
            speed = int(real.get("downloadSpeed") or 0)
            status = real.get("status", "")
            if status == "complete":
                state, progress = "done", 1.0
            else:
                progress = (done / total) if total else 0.0
                state = {"active": "downloading", "waiting": "queued", "paused": "paused",
                         "error": "error", "removed": "error"}.get(status, status)
                if state == "downloading" and not speed and not int(real.get("connections") or 0):
                    state = "stalled"
            out.append({
                "hash": info_hash.lower(),
                "name": ((real.get("bittorrent") or {}).get("info") or {}).get("name", ""),
                "progress": progress, "state": state, "speed": speed,
                "eta": int((total - done) / speed) if speed and total > done else 0,
                "size": total, "path": real.get("dir", ""), "content": self._content(real),
                "error": real.get("errorMessage", "") if state == "error" else "",
            })
        return out

    def remove(self, hashes: list[str], delete_files: bool = False) -> None:
        entries = self._entries()
        for info_hash in hashes:
            mine = [e for e in entries if str(e.get("infoHash", "")).lower() == info_hash.lower()]
            for entry in mine:
                gid = entry.get("gid", "")
                if entry.get("status") in ("active", "waiting", "paused"):
                    try:
                        self._call("aria2.forceRemove", gid)
                    except TorrentClientError:
                        pass
                try:
                    self._call("aria2.removeDownloadResult", gid)
                except TorrentClientError:
                    pass
            if delete_files:
                for entry in mine:
                    if not self._is_metadata(entry):
                        _delete_inside(entry.get("dir") or "", self._content(entry))

    def files(self, info_hash: str) -> list[dict]:
        real, _meta = self._pick(info_hash)
        if real is None:
            return []
        folder = real.get("dir") or ""
        out = []
        for item in self._call("aria2.getFiles", real["gid"]) or []:
            path = str(item.get("path", ""))
            try:
                name = os.path.relpath(path, folder) if folder else path
            except ValueError:
                name = path
            out.append({"index": int(item.get("index") or 0), "name": name.replace(os.sep, "/"),
                        "size": int(item.get("length") or 0)})
        return out

    def skip(self, info_hash: str, indexes: list[int]) -> None:
        real, _meta = self._pick(info_hash)
        if real is None or not indexes:
            return
        unwanted = set(indexes)
        wanted = [str(f["index"]) for f in self.files(info_hash) if f["index"] not in unwanted]
        if wanted:
            self._call("aria2.changeOption", real["gid"], {"select-file": ",".join(wanted)})

    def start(self, info_hash: str) -> None:
        """Let a download go that is waiting for its files to be chosen."""
        real, _meta = self._pick(info_hash)
        if real is not None and real.get("status") == "paused":
            self._call("aria2.unpause", real["gid"])


def _delete_inside(folder: str, target: str) -> None:
    """Delete a download and aria2's note beside it, never outside ``folder``."""
    if not folder or not target:
        return
    root = os.path.realpath(folder)
    for path in (target, target + ".aria2"):
        real = os.path.realpath(path)
        if real == root or not real.startswith(root + os.sep):
            continue
        try:
            if os.path.isdir(real):
                import shutil
                shutil.rmtree(real, ignore_errors=True)
            elif os.path.exists(real):
                os.remove(real)
        except OSError:
            pass


def from_settings(config: dict | None):
    """The configured client, or None when magnets go to the system handler."""
    config = config or {}
    kind = str(config.get("kind") or "system")
    args = (str(config.get("url") or ""), str(config.get("username") or ""),
            str(config.get("password") or ""))
    if kind == "qbittorrent":
        return QBittorrent(*args)
    if kind == "transmission":
        return Transmission(*args)
    return None


# Where each client listens when nobody has changed it.
DEFAULTS = (("transmission", "http://127.0.0.1:9091"), ("qbittorrent", "http://127.0.0.1:8080"))


def detect(timeout: int = 3) -> dict | None:
    """A torrent client already running on this computer, found without asking.

    Returns ``{"kind", "url", "label", "version"}``, with ``needs_login``
    set when one answers but wants a password, or None when none answers.
    """
    locked = None
    for kind, url in DEFAULTS:
        client = from_settings({"kind": kind, "url": url})
        client.timeout = timeout
        try:
            version = client.version()
        except TorrentClientError as exc:
            if "username or password" in str(exc) and locked is None:
                locked = {"kind": kind, "url": url, "label": client.label, "version": "",
                          "needs_login": True}
            continue
        return {"kind": kind, "url": url, "label": client.label, "version": version}
    return locked
