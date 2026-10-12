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

    def add(self, magnet: str = "", torrent: bytes = b"", save_path: str = "") -> None:
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
                "error": "",
            })
        return out


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

    def add(self, magnet: str = "", torrent: bytes = b"", save_path: str = "") -> None:
        arguments: dict = {}
        if torrent:
            arguments["metainfo"] = base64.b64encode(torrent).decode()
        else:
            arguments["filename"] = magnet
        if save_path:
            arguments["download-dir"] = save_path
        self._call("torrent-add", arguments)

    STATES = {0: "paused", 1: "checking", 2: "checking", 3: "queued", 4: "downloading",
              5: "done", 6: "done"}

    def status(self, hashes: list[str]) -> list[dict]:
        if not hashes:
            return []
        fields = ["hashString", "name", "percentDone", "status", "rateDownload", "eta",
                  "downloadDir", "totalSize", "error", "errorString"]
        found = self._call("torrent-get", {"fields": fields, "ids": hashes})
        out = []
        for item in found.get("torrents") or []:
            done = float(item.get("percentDone") or 0)
            state = self.STATES.get(int(item.get("status") or 0), "")
            if state == "paused" and done >= 1:
                state = "done"
            out.append({
                "hash": str(item.get("hashString", "")).lower(),
                "name": item.get("name", ""),
                "progress": done,
                "state": "error" if item.get("error") else state,
                "speed": int(item.get("rateDownload") or 0),
                "eta": int(item.get("eta") or 0),
                "size": int(item.get("totalSize") or 0),
                "path": item.get("downloadDir", ""),
                "error": item.get("errorString", ""),
            })
        return out


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
