"""A small local web server. Everything stays on 127.0.0.1."""

from __future__ import annotations

import json
import mimetypes
import os
import platform
import re
import secrets
import string
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import (autoinstall, catalog, download, embed, feeds, installers, library, pipeline,
               placement, resolve, roms, scanners, sgdb, steam, tools, torrentclient, torrents,
               uninstall, updater, winproc)

def _web_dir() -> Path:
    """Where index.html lives, running from source or from a built binary.

    A one-file build unpacks itself into a temporary directory and points
    sys._MEIPASS at it, so the path next to this module is not where the
    assets end up.
    """
    bundled = getattr(sys, "_MEIPASS", "")
    if bundled:
        candidate = Path(bundled) / "nsgm" / "web"
        if candidate.is_dir():
            return candidate
        return Path(bundled) / "web"
    return Path(__file__).parent / "web"


WEB_DIR = _web_dir()

STATE: dict = {"steam_root": None, "users": [], "games": [], "notes": []}

# Every API request must carry this. The page gets it when it loads; a web
# page on any other site does not, so it cannot drive the app through the
# browser - which matters now that the app can download and install by
# itself.
TOKEN = secrets.token_urlsafe(24)


def json_response(handler: BaseHTTPRequestHandler, payload, status: int = 200) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def refresh_users() -> list:
    root = steam.find_steam_root()
    STATE["steam_root"] = root
    STATE["users"] = list_users_safe(root)
    return STATE["users"]


def find_user(id3: str) -> steam.SteamUser:
    """Look up an account, re-reading Steam's folder if we have not yet."""
    users = STATE["users"] or refresh_users()
    for user in users:
        if user.id3 == str(id3):
            return user
    for user in refresh_users():          # account may have been added since
        if user.id3 == str(id3):
            return user
    if not users:
        raise ValueError("No Steam account was found on this computer.")
    raise ValueError("Pick a Steam account first.")


# --------------------------------------------------------------------------
# folder browsing, so nobody has to type a path by hand
# --------------------------------------------------------------------------

def list_drives() -> list[str]:
    if platform.system() != "Windows":
        return ["/"]
    return [f"{letter}:\\" for letter in string.ascii_uppercase if Path(f"{letter}:\\").exists()]


EXECUTABLE_SUFFIXES = {".exe", ".bat", ".cmd", ".sh", ".appimage", ".x86_64", ".bin", ""}


def browse(path: str, with_files: bool = False, all_files: bool = False) -> dict:
    """List folders, and optionally files, under one path.

    ``with_files`` adds runnable files, for picking an emulator;
    ``all_files`` adds everything with sizes, for looking inside a folder
    without leaving the app.
    """
    target = Path(path).expanduser() if path else Path.home()
    if not target.is_dir():
        # A path to a file means the caller wants that file's folder shown.
        parent = Path(path).expanduser().parent if path else Path.home()
        target = parent if parent.is_dir() else Path.home()
    try:
        target = target.resolve()
    except OSError:
        pass

    dirs: list[dict] = []
    files: list[dict] = []
    try:
        for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith("."):
                continue
            if child.is_dir():
                dirs.append({"name": child.name, "path": str(child)})
            elif all_files:
                try:
                    size = child.stat().st_size
                except OSError:
                    size = 0
                name = child.name.lower()
                kind = ("archive" if installers.is_archive(child)
                        else "installer" if name.endswith((".msi",)) or (
                            name.endswith(".exe") and any(h in name for h in installers.INSTALLER_HINTS))
                        else "program" if name.endswith((".exe", ".appimage", ".sh", ".x86_64"))
                        else "file")
                files.append({"name": child.name, "path": str(child), "size": size, "kind": kind})
            elif with_files and child.suffix.lower() in EXECUTABLE_SUFFIXES:
                if child.suffix or os.access(child, os.X_OK):
                    files.append({"name": child.name, "path": str(child)})
    except OSError as exc:
        return {"path": str(target), "parent": str(target.parent), "dirs": [], "files": [],
                "drives": list_drives(), "error": str(exc)}

    return {
        "path": str(target),
        "parent": str(target.parent) if target.parent != target else "",
        "dirs": dirs,
        "files": files,
        "drives": list_drives(),
    }


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------

def scan_job(job: library.Job, settings: dict) -> None:
    job.stage = "scanning"
    job.message = "Looking for installed games"
    games, notes = library.collect_games(settings)
    prefer_direct = bool(settings.get("prefer_direct"))
    for game in games:
        game["short_id"] = library.preview_short_id(game, prefer_direct)
    STATE["games"] = games
    STATE["notes"] = notes
    STATE["matches"] = {}
    library.save_session(games, {})
    job.total = job.done = len(games)
    job.result = {"count": len(games), "notes": notes}
    job.message = f"{len(games)} games found"


def match_job(job: library.Job, games: list[dict], settings: dict) -> None:
    """Look every game up on SteamGridDB and grab a cover to preview."""
    client = sgdb.SteamGridDB(settings.get("api_key", ""))
    job.total = len(games)
    job.stage = "matching"
    matches: dict[str, dict] = {}

    for index, game in enumerate(games, 1):
        job.done = index
        title = game.get("title", "")
        job.message = title
        try:
            found = client.match(game.get("sgdb_hint") or title)
            if not found:
                matches[game["uid"]] = {"sgdb_id": None, "sgdb_name": "", "thumb": ""}
                continue
            art = client.artwork(
                found["id"], "capsule",
                nsfw=bool(settings.get("allow_nsfw")),
                animated=bool(settings.get("allow_animated")),
                limit=1,
            )
            matches[game["uid"]] = {
                "sgdb_id": found["id"],
                "sgdb_name": found["name"],
                "score": found["score"],
                "thumb": art[0]["thumb"] if art else "",
                "url": art[0]["url"] if art else "",
            }
        except sgdb.SGDBError as exc:
            job.error = str(exc)
            job.log.append(str(exc))
            break

    STATE["matches"] = {**(STATE.get("matches") or {}), **matches}
    library.save_session(STATE.get("games") or [], STATE["matches"])
    job.result = {"matches": matches}
    job.message = f"{sum(1 for m in matches.values() if m.get('sgdb_id'))} of {len(games)} matched"


def owned_titles(user: steam.SteamUser | None) -> list[str]:
    """Normalised titles the person already has, for flagging feed items."""
    titles = {g.get("title", "") for g in (STATE.get("games") or [])}
    if user:
        try:
            titles |= {e.get("AppName", "") for e in steam.read_shortcuts(user)}
        except OSError:
            pass
    cleaned = set()
    for title in titles:
        normal = sgdb.normalise(title)
        if len(normal) >= 4:          # "Ori" style stubs would match everything
            cleaned.add(normal)
    return sorted(cleaned)


def flag_owned(items: list[dict], owned: list[str]) -> list[dict]:
    """Mark feed entries whose headline names something already installed."""
    if not owned:
        return items
    for item in items:
        headline = sgdb.normalise(item.get("title", ""))
        item["owned"] = any(name in headline for name in owned) if headline else False
    return items


def feed_job(job: library.Job, feed_list: list[dict], user: steam.SteamUser | None) -> None:
    job.stage = "fetching"
    job.total = sum(1 for f in feed_list if f.get("enabled", True))

    def progress(name: str) -> None:
        job.done += 1
        job.message = name

    items, notes = feeds.refresh(feed_list, library.config_dir(), progress)
    for note in notes:
        job.log.append(note)
    flag_owned(items, owned_titles(user))
    job.result = {"items": items[:400], "notes": notes, "fetched": time.time()}
    job.message = f"{len(items)} entries from {job.total} feeds"


def deep_resolve_job(job: library.Job, url: str, pages: int, token: str,
                     force: bool) -> None:
    """Follow a listing to its end, reporting as it goes.

    There is no page number to choose: it stops when the site stops
    offering another page. The ceiling is only a safety net.
    """
    job.stage = "reading"
    # Unknown, and honestly so. Every page read can reveal more pages, so
    # any denominator during a crawl is a guess that keeps being wrong.
    # Zero tells the client to show movement rather than a fraction.
    job.total = 0

    def progress(read: int, files: int, swarm: int, queued: int) -> None:
        job.done = read
        job.total = 0
        waiting = f", {queued} more queued" if queued else ""
        job.message = (
            f"{read} pages read{waiting} \u00b7 "
            f"{files} downloads, {swarm} magnets and torrents"
        )

    found = resolve.resolve(
        url, token=token, pages=pages, cache_dir=library.config_dir(),
        force=force, progress=progress,
    )
    job.result = found
    # Only at the end is the count real.
    job.done = job.total = max(1, found.get("pages_read", 1))
    for line in (found.get("page_list") or [])[:12]:
        job.log.append(line)
    if len(found.get("page_list") or []) > 12:
        job.log.append(f"...and {len(found['page_list']) - 12} more addresses.")
    if found.get("pages_read", 0) >= pages:
        job.log.append(
            f"Stopped at the {pages} page safety limit. There may be more; "
            "run it again to continue."
        )
    shown = found.get("shown") or {}
    job.message = (
        f"{found.get('pages_read', 0)} pages, "
        f"{shown.get('files_total', len(found.get('candidates', [])))} downloads, "
        f"{shown.get('swarm_total', 0)} magnets and torrents"
    )


def update_job(job: library.Job, url: str) -> None:
    """Swap in the newer program, then ask to be restarted."""
    job.stage = "downloading"
    job.total = 100

    def progress(done: int, total: int) -> None:
        job.done = int(done * 100 / total) if total else 0
        job.message = f"{done // 1048576} MB of {(total or 0) // 1048576} MB"

    result = updater.apply(url, progress)
    job.done = job.total
    job.result = {**result, "version_note": "Restart to finish."}
    job.message = "Installed. Restart to finish."


def index_job(job: library.Job, url: str, pages: int) -> None:
    """Walk a listing of programs and collect every entry it names."""
    job.stage = "crawling"
    job.total = pages

    def progress(page_number: int, found: int) -> None:
        job.done = min(page_number, pages)
        job.message = f"page {page_number}, {found} programs so far"

    result = resolve.crawl_index(url, max_pages=pages, progress=progress)
    result["requested_pages"] = pages
    job.result = result
    job.done = job.total
    job.message = (
        f"{len(result['programs'])} programs from {result['pages_read']} pages"
        + ("" if result["exhausted"] else ", more available")
    )


def watch_job(job: library.Job, watched: list, token: str) -> None:
    """Re-check every watched page and report which ones moved."""
    job.stage = "checking"
    job.total = sum(1 for w in watched if w.get("enabled", True))

    def progress(name: str) -> None:
        job.done += 1
        job.message = name

    updated, changed = resolve.check_watched(watched, token=token, progress=progress)
    library.save_settings({"watched": updated})
    for entry in changed:
        job.log.append(
            f"{entry.get('name')}: now {entry.get('best_name') or 'a different set of files'}"
        )
    job.result = {"watched": updated, "changed": changed}
    job.message = (
        f"{len(changed)} of {len(updated)} changed" if changed
        else f"nothing new across {len(updated)} pages"
    )


def download_job(job: library.Job, url: str, folder: str) -> None:
    job.stage = "downloading"
    job.message = "Starting"

    def progress(done: int, total: int) -> None:
        job.done, job.total = done, total or done
        mb = done / (1024 * 1024)
        job.message = (f"{mb:.1f} MB of {total / (1024 * 1024):.1f} MB"
                       if total else f"{mb:.1f} MB")

    result = download.download(url, folder, progress=progress)
    job.result = result
    job.message = f"Saved {result['name']}"

    # With automatic installing on, a finished download goes straight into
    # the queue, exactly as one arriving in the downloads folder would.
    settings = library.load_settings()
    if settings.get("auto_install"):
        info = installers.inspect(result["path"])
        if info["kind"] not in ("unsupported",):
            task = autoinstall.QUEUE.add(result["path"], auto=True, origin="download")
            job.result["queued"] = task.id
            job.message = f"Saved {result['name']}; installing it now"


def uninstall_job(job: library.Job, program: dict, silent: bool, user) -> None:
    """Hand off to the program's uninstaller, then look for what it left."""
    job.total = 2
    job.stage = "uninstalling"
    if autoinstall.PROGRAM_LOCK.locked():
        job.message = "Waiting for the installer that is running to finish"
    else:
        job.message = (f"Removing {program.get('name')} quietly" if silent
                       else f"{program.get('name')} \u2014 answer its uninstaller")

    outcome = autoinstall.run_uninstaller(program, silent=silent)
    job.done = 1
    job.log.append(outcome.get("message", ""))

    job.stage = "sweeping"
    job.message = "Looking for leftover folders"
    leftovers = uninstall.find_leftovers(program) if outcome.get("ok") else []
    job.done = 2

    shortcut = None
    if user and outcome.get("ok"):
        target = sgdb.normalise(program.get("name", ""))
        for entry in library.managed_list(user):
            if target and target == sgdb.normalise(entry["title"]):
                shortcut = {"short_id": entry["short_id"], "title": entry["title"]}
                break

    job.result = {
        "uninstalled": bool(outcome.get("ok")),
        "message": outcome.get("message", ""),
        "leftovers": leftovers,
        "shortcut": shortcut,
    }
    job.message = outcome.get("message", "")


def seven_zip_job(job: library.Job) -> None:
    job.stage = "fetching"
    job.total = 3

    def progress(message: str) -> None:
        job.done = min(job.done + 1, 3)
        job.message = message
        job.log.append(message)

    path = tools.fetch_seven_zip(progress)
    job.done = job.total
    job.result = {"path": path}
    job.message = "7-Zip is ready"
    autoinstall.QUEUE.retry_waiting("sevenzip")


# --------------------------------------------------------------------------
# installs and everything around them
# --------------------------------------------------------------------------

def stage_games(games: list[dict]) -> None:
    """Put freshly installed games on the Add games shelf."""
    settings = library.load_settings()
    prefer_direct = bool(settings.get("prefer_direct"))
    if not STATE.get("games"):
        saved = library.load_session()
        STATE["games"] = saved["games"]
        STATE["matches"] = saved["matches"]
    merged = {g["uid"]: g for g in STATE.get("games") or []}
    for game in games:
        game.setdefault("uid", uuid.uuid4().hex[:12])
        game["short_id"] = library.preview_short_id(game, prefer_direct)
        merged[game["uid"]] = game
    STATE["games"] = sorted(merged.values(), key=lambda g: g["title"].lower())
    library.save_session(STATE["games"], STATE.get("matches") or {})


_STEAM_SEEN = {"at": 0.0, "running": False}
_TORRENTS_SEEN: dict = {"at": 0.0, "items": [], "error": "", "key": ""}
_CACHE_LOCK = threading.Lock()


def steam_running_cached() -> bool:
    """Steam's state, looked up at most every few seconds.

    Asking means running tasklist or pgrep, and the interface asks on
    every poll.
    """
    with _CACHE_LOCK:
        if time.time() - _STEAM_SEEN["at"] > 4:
            _STEAM_SEEN["running"] = steam.steam_is_running()
            _STEAM_SEEN["at"] = time.time()
        return _STEAM_SEEN["running"]


def torrent_status() -> dict:
    """Progress of the torrents this app handed to the client."""
    settings = library.load_settings()
    tracked = settings.get("torrents") or []
    client = torrentclient.from_settings(settings.get("torrent_client"))
    if not tracked or client is None:
        return {"items": [], "client": "", "error": ""}
    key = ",".join(sorted(t.get("hash", "") for t in tracked))
    with _CACHE_LOCK:
        fresh = time.time() - _TORRENTS_SEEN["at"] < 3 and _TORRENTS_SEEN["key"] == key
    if not fresh:
        try:
            found = {t["hash"]: t for t in client.status([t["hash"] for t in tracked
                                                          if t.get("hash")])}
            error = ""
        except torrentclient.TorrentClientError as exc:
            found, error = {}, str(exc)
        items = []
        for entry in tracked:
            live = found.get(entry.get("hash", ""))
            items.append({**entry, **(live or {}),
                          "missing": live is None and not error})
        with _CACHE_LOCK:
            _TORRENTS_SEEN.update(at=time.time(), items=items, error=error, key=key)
    return {"items": _TORRENTS_SEEN["items"], "client": client.label,
            "error": _TORRENTS_SEEN["error"]}


def activity(since: int) -> dict:
    """Everything the interface polls for: events, installs, the held window."""
    seq, events = autoinstall.QUEUE.events_since(since)
    return {
        "seq": seq,
        "events": events,
        "tasks": autoinstall.QUEUE.snapshot(),
        "busy": autoinstall.QUEUE.busy(),
        "embed": embed.MANAGER.current(),
        "steam_running": steam_running_cached(),
        "torrents": torrent_status(),
    }


def library_state() -> dict:
    settings = library.load_settings()
    return {
        "folders": placement.describe_all(settings.get("library_folders") or []),
        "auto_install": bool(settings.get("auto_install")),
        "ssd_policy": settings.get("ssd_policy", "strict"),
        "prefer_ssd_recommended": bool(settings.get("prefer_ssd_recommended")),
        "install_mode": settings.get("install_mode", "classic"),
        "delete_after_install": bool(settings.get("delete_after_install")),
        "auto_add_to_steam": bool(settings.get("auto_add_to_steam")),
        "watch_incoming": bool(settings.get("watch_incoming")),
        "incoming_folder": settings.get("incoming_folder", ""),
        "backlog": len(autoinstall.WATCHER.backlog()) if settings.get("incoming_folder") else 0,
    }


LIBRARY_KEYS = {"auto_install": bool, "prefer_ssd_recommended": bool,
                "delete_after_install": bool, "auto_add_to_steam": bool,
                "watch_incoming": bool}


def save_library(payload: dict) -> dict:
    """Store library settings, cleaned, and react to switches being flipped."""
    before = library.load_settings()
    patch: dict = {}
    if "library_folders" in payload:
        folders = []
        seen = set()
        for item in payload.get("library_folders") or []:
            path = str((item or {}).get("path", "")).strip()
            if not path or path.lower() in seen:
                continue
            seen.add(path.lower())
            kind = str(item.get("kind", "auto")).lower()
            try:
                reserve = max(0.0, min(float(item.get("reserve_gb", 10)), 100000.0))
            except (TypeError, ValueError):
                reserve = 10.0
            folders.append({"path": path, "kind": kind if kind in ("auto", "ssd", "hdd") else "auto",
                            "reserve_gb": reserve, "enabled": item.get("enabled", True) is not False})
        patch["library_folders"] = folders
    for key, kind in LIBRARY_KEYS.items():
        if key in payload:
            patch[key] = kind(payload[key])
    if payload.get("ssd_policy") in placement.POLICIES:
        patch["ssd_policy"] = payload["ssd_policy"]
    if payload.get("install_mode") in ("classic", "quiet"):
        patch["install_mode"] = payload["install_mode"]
    library.save_settings(patch)

    # Switching watching on means "from now on": what is already in the
    # folder becomes the starting point instead of a pile to install.
    was_watching = before.get("auto_install") or before.get("watch_incoming")
    now = library.load_settings()
    if (now.get("auto_install") or now.get("watch_incoming")) and not was_watching:
        autoinstall.WATCHER.rebaseline()
    autoinstall.WATCHER.poke.set()
    return library_state()


# --------------------------------------------------------------------------
# request handling
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "NonSteamGameManager"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # -- helpers ----------------------------------------------------------

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def allowed(self, need_token: bool) -> bool:
        """Refuse anything not from this app's own page.

        The Host header defeats DNS rebinding, where a hostile site points
        its own name at 127.0.0.1; the token defeats any other site simply
        posting to this port from someone's browser.
        """
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            json_response(self, {"error": "Forbidden"}, 403)
            return False
        if need_token and not secrets.compare_digest(
                self.headers.get("X-NSGM-Token") or "", TOKEN):
            json_response(self, {"error": "Forbidden"}, 403)
            return False
        return True

    def send_index(self) -> None:
        page = (WEB_DIR / "index.html").read_text("utf-8")
        page = page.replace("</head>", f'<meta name="nsgm-token" content="{TOKEN}">\n</head>', 1)
        blob = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def send_file(self, path: Path) -> None:
        if not path.is_file():
            json_response(self, {"error": "Not found"}, 404)
            return
        kind = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        blob = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    # -- routes -----------------------------------------------------------

    def do_GET(self):
        route = urlparse(self.path)
        path = route.path
        query = parse_qs(route.query)

        if not self.allowed(need_token=path.startswith("/api/")):
            return None
        if path in ("/", "/index.html"):
            return self.send_index()
        if path.startswith("/static/"):
            name = unquote(path[len("/static/"):])
            candidate = (WEB_DIR / name).resolve()
            if WEB_DIR.resolve() in candidate.parents:
                return self.send_file(candidate)
            return json_response(self, {"error": "Not found"}, 404)

        if path.startswith("/art/"):
            parts = path.split("/", 3)
            if len(parts) == 4:
                try:
                    user = find_user(parts[2])
                except ValueError:
                    return json_response(self, {"error": "Unknown account"}, 404)
                name = unquote(parts[3])
                if re.fullmatch(r"[\w.\-]+", name):
                    return self.send_file(user.grid_dir / name)
            return json_response(self, {"error": "Not found"}, 404)

        if path == "/api/bootstrap":
            return json_response(self, self.bootstrap())

        if path == "/api/job":
            job = library.JOBS.get((query.get("id") or [""])[0])
            if not job:
                return json_response(self, {"error": "Unknown job"}, 404)
            return json_response(self, job.to_json())

        if path == "/api/managed":
            try:
                user = find_user((query.get("user") or [""])[0])
            except ValueError as exc:
                return json_response(self, {"error": str(exc)}, 400)
            return json_response(self, {"games": library.managed_list(user)})

        if path == "/api/browse":
            files = (query.get("files") or ["0"])[0]
            return json_response(
                self,
                browse((query.get("path") or [""])[0], files == "1", files == "all"),
            )

        if path == "/api/activity":
            try:
                since = int((query.get("since") or ["0"])[0])
            except ValueError:
                since = 0
            return json_response(self, activity(since))

        if path == "/api/library":
            return json_response(self, library_state())

        if path == "/api/install/backlog":
            return json_response(self, {"items": autoinstall.WATCHER.backlog()})

        if path == "/api/feeds":
            settings = library.load_settings()
            feed_list = settings.get("feeds") or []
            user = None
            wanted = (query.get("user") or [""])[0]
            if wanted:
                try:
                    user = find_user(wanted)
                except ValueError:
                    user = None
            items = feeds.cached_items(feed_list, library.config_dir())
            flag_owned(items, owned_titles(user))
            return json_response(self, {"feeds": feed_list, "items": items[:400]})

        if path == "/api/catalog":
            return json_response(self, {
                "games": catalog.catalogue(),
                "tags": catalog.all_tags(),
                "system": platform.system(),
                "arch": catalog.host_arch(),
            })

        if path == "/api/download/folder":
            return json_response(self, {"folder": str(download.default_folder())})

        if path == "/api/programs":
            return json_response(self, {
                "programs": uninstall.list_programs(),
                "supported": uninstall.IS_WINDOWS,
            })

        if path == "/api/update":
            from . import __version__
            settings = library.load_settings()
            return json_response(
                self,
                updater.check(__version__, settings.get("update_repo") or updater.REPO),
            )

        if path == "/api/presets":
            return json_response(self, {"presets": roms.PRESETS})

        return json_response(self, {"error": "Not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.allowed(need_token=True):
            return None
        payload = self.read_json()

        try:
            if path == "/api/update/apply":
                url = str(payload.get("url", "")).strip()
                if not url:
                    return json_response(self, {"error": "Nothing to install."}, 400)
                job = library.start_job(update_job, url)
                return json_response(self, {"job": job.id})

            if path == "/api/quit":
                # Browser mode has no window to close, so there has to be a
                # way out that is not Task Manager.
                def stop() -> None:
                    time.sleep(0.4)
                    os._exit(0)
                threading.Thread(target=stop, daemon=True).start()
                return json_response(self, {"stopping": True})

            if path == "/api/settings":
                incoming = dict(payload.get("settings") or {})
                client = incoming.get("torrent_client")
                if client is not None:
                    kind = str((client or {}).get("kind", "system"))
                    incoming["torrent_client"] = {
                        "kind": kind if kind in torrentclient.KINDS else "system",
                        "url": str(client.get("url", "")).strip()[:200],
                        "username": str(client.get("username", ""))[:120],
                        "password": str(client.get("password", ""))[:200],
                    }
                library.save_settings(incoming)
                return json_response(self, {"settings": library.load_settings()})

            if path == "/api/scan":
                settings = library.load_settings()
                settings.update(payload.get("settings") or {})
                job = library.start_job(scan_job, settings)
                return json_response(self, {"job": job.id})

            if path == "/api/games":
                # A page reload should not throw away a scan and a round of
                # cover matching; both are restored from the last session.
                if not STATE.get("games"):
                    saved = library.load_session()
                    STATE["games"] = saved["games"]
                    STATE["matches"] = saved["matches"]
                return json_response(self, {
                    "games": STATE["games"],
                    "notes": STATE["notes"],
                    "matches": STATE.get("matches") or {},
                })

            if path == "/api/match":
                settings = library.load_settings()
                uids = set(payload.get("uids") or [])
                targets = [g for g in STATE["games"] if not uids or g["uid"] in uids]
                job = library.start_job(match_job, targets, settings)
                return json_response(self, {"job": job.id})

            if path == "/api/sgdb/search":
                client = sgdb.SteamGridDB(library.load_settings().get("api_key", ""))
                return json_response(self, {"results": client.search(payload.get("term", ""))})

            if path == "/api/sgdb/art":
                settings = library.load_settings()
                client = sgdb.SteamGridDB(settings.get("api_key", ""))
                items = client.artwork(
                    int(payload.get("game_id")), payload.get("kind", "capsule"),
                    nsfw=bool(settings.get("allow_nsfw")),
                    animated=bool(settings.get("allow_animated")),
                )
                return json_response(self, {"art": items})

            if path == "/api/apply":
                user = find_user(payload.get("user"))
                library.save_settings({"account": user.id3})
                settings = library.load_settings()
                settings.update(payload.get("settings") or {})
                chosen = payload.get("games") or []
                index = {g["uid"]: g for g in STATE["games"]}
                games = []
                for item in chosen:
                    base = dict(index.get(item.get("uid"), {}))
                    if not base:
                        continue
                    base.update({k: v for k, v in item.items() if v not in (None, "")})
                    games.append(base)
                if not games:
                    return json_response(self, {"error": "Select at least one game."}, 400)
                job = library.start_job(library.apply_games, user, games, settings)
                return json_response(self, {"job": job.id})

            if path == "/api/remove":
                user = find_user(payload.get("user"))
                job = library.start_job(
                    library.remove_games, user,
                    [int(i) for i in payload.get("short_ids") or []],
                    bool(payload.get("delete_art", True)),
                )
                return json_response(self, {"job": job.id})

            if path == "/api/feeds/refresh":
                settings = library.load_settings()
                user = None
                if payload.get("user"):
                    try:
                        user = find_user(payload["user"])
                    except ValueError:
                        user = None
                job = library.start_job(feed_job, settings.get("feeds") or [], user)
                return json_response(self, {"job": job.id})

            if path == "/api/feeds/discover":
                try:
                    found = feeds.discover(str(payload.get("url", "")))
                except feeds.FeedError as exc:
                    return json_response(self, {"error": str(exc)}, 400)
                return json_response(self, {"candidates": found})

            if path == "/api/feeds/save":
                if payload.get("restore"):
                    restored = [dict(f) for f in feeds.DEFAULT_FEEDS]
                    library.save_settings({"feeds": restored})
                    return json_response(self, {"feeds": restored})
                incoming = payload.get("feeds")
                if not isinstance(incoming, list):
                    return json_response(self, {"error": "Expected a list of feeds."}, 400)
                cleaned = []
                for item in incoming:
                    url = str(item.get("url", "")).strip()
                    if not url:
                        continue
                    if not re.match(r"^https?://", url, re.I):
                        url = "https://" + url
                    cleaned.append({
                        "name": str(item.get("name") or url)[:80],
                        "url": url,
                        "enabled": bool(item.get("enabled", True)),
                        "tag": str(item.get("tag", ""))[:20],
                    })
                library.save_settings({"feeds": cleaned})
                return json_response(self, {"feeds": cleaned})

            if path == "/api/catalog/resolve":
                name = str(payload.get("name", ""))
                game = next((g for g in catalog.GAMES if g["name"] == name), None)
                if not game:
                    return json_response(self, {"error": "Unknown game."}, 400)
                settings = library.load_settings()
                try:
                    found = catalog.resolve(
                        game, library.config_dir(),
                        token=str(settings.get("github_token", "")).strip(),
                        force=bool(payload.get("force")),
                    )
                except catalog.RateLimit as exc:
                    return json_response(self, {"error": str(exc), "rate_limited": True}, 429)
                except catalog.CatalogError as exc:
                    return json_response(self, {"error": str(exc)}, 502)
                return json_response(self, {"release": found})

            if path == "/api/resolve":
                url = str(payload.get("url", "")).strip()
                if not url:
                    return json_response(self, {"error": "Paste a page address first."}, 400)
                settings = library.load_settings()
                try:
                    found = resolve.resolve(
                        url,
                        token=str(settings.get("github_token", "")).strip(),
                        tag=str(payload.get("tag", "")).strip(),
                        pages=max(1, min(int(payload.get("pages") or 25), 200)),
                        cache_dir=library.config_dir(),
                        force=bool(payload.get("force")),
                    )
                except catalog.RateLimit as exc:
                    return json_response(self, {"error": str(exc), "rate_limited": True}, 429)
                except (resolve.ResolveError, catalog.CatalogError) as exc:
                    return json_response(self, {"error": str(exc)}, 502)
                return json_response(self, found)

            if path == "/api/resolve/search":
                settings = library.load_settings()
                try:
                    results = resolve.search_projects(
                        str(payload.get("term", "")),
                        token=str(settings.get("github_token", "")).strip(),
                    )
                except catalog.RateLimit as exc:
                    return json_response(self, {"error": str(exc), "rate_limited": True}, 429)
                except (resolve.ResolveError, catalog.CatalogError) as exc:
                    return json_response(self, {"error": str(exc)}, 502)
                return json_response(self, {"results": results})

            if path == "/api/resolve/releases":
                settings = library.load_settings()
                try:
                    history = resolve.releases(
                        str(payload.get("url", "")),
                        token=str(settings.get("github_token", "")).strip(),
                    )
                except catalog.RateLimit as exc:
                    return json_response(self, {"error": str(exc), "rate_limited": True}, 429)
                except (resolve.ResolveError, catalog.CatalogError) as exc:
                    return json_response(self, {"error": str(exc)}, 502)
                return json_response(self, history)

            if path == "/api/resolve/deep":
                url = str(payload.get("url", "")).strip()
                if not url:
                    return json_response(self, {"error": "Paste a page address first."}, 400)
                settings = library.load_settings()
                job = library.start_job(
                    deep_resolve_job, url,
                    # A ceiling only exists so a broken site cannot spin
                    # forever. Real listings end long before this, and when
                    # one does not, the result says so instead of stopping
                    # quietly at a number nobody chose.
                    max(1, min(int(payload.get("pages") or 20000), 20000)),
                    str(settings.get("github_token", "")).strip(),
                    bool(payload.get("force")),
                )
                return json_response(self, {"job": job.id})

            if path == "/api/incoming":
                folder = str(payload.get("folder", "")).strip()
                if not folder:
                    return json_response(self, {"error": "Choose your downloads folder."}, 400)
                try:
                    items = pipeline.survey(folder)
                except pipeline.PipelineError as exc:
                    return json_response(self, {"error": str(exc)}, 400)
                library.save_settings({"incoming_folder": folder})
                return json_response(self, {"items": items, "folder": folder})

            if path == "/api/incoming/unpack":
                try:
                    return json_response(self, pipeline.unpack(
                        str(payload.get("path", "")),
                        keep_archive=bool(payload.get("keep", True)),
                    ))
                except pipeline.PipelineError as exc:
                    return json_response(self, {"error": str(exc)}, 400)

            if path == "/api/incoming/stage":
                # Staged items join the normal scan results, so adding them to
                # Steam runs through exactly the same path as anything else.
                settings = library.load_settings()
                staged = []
                for item in payload.get("items") or []:
                    game = pipeline.to_game(item)
                    game["uid"] = uuid.uuid4().hex[:12]
                    game["short_id"] = library.preview_short_id(
                        game, bool(settings.get("prefer_direct"))
                    )
                    staged.append(game)
                merged = {g["uid"]: g for g in STATE.get("games") or []}
                for game in staged:
                    merged[game["uid"]] = game
                STATE["games"] = sorted(merged.values(), key=lambda g: g["title"].lower())
                return json_response(self, {"staged": len(staged), "games": staged})

            if path == "/api/index":
                url = str(payload.get("url", "")).strip()
                if not url:
                    return json_response(self, {"error": "Paste a site address first."}, 400)
                pages = max(1, min(int(payload.get("pages") or 25), 200))
                job = library.start_job(index_job, url, pages)
                return json_response(self, {"job": job.id})

            if path == "/api/watch/check":
                settings = library.load_settings()
                job = library.start_job(
                    watch_job,
                    settings.get("watched") or [],
                    str(settings.get("github_token", "")).strip(),
                )
                return json_response(self, {"job": job.id})

            if path in ("/api/magnet/open", "/api/torrent/add"):
                return json_response(self, *add_torrent(str(payload.get("url", "")),
                                                        str(payload.get("name", ""))))

            if path == "/api/torrent/test":
                client = torrentclient.from_settings(payload.get("config") or {})
                if client is None:
                    return json_response(self, {"ok": True, "message":
                                                "Magnets will open in whatever app the system uses."})
                try:
                    version = client.version()
                except torrentclient.TorrentClientError as exc:
                    return json_response(self, {"ok": False, "message": str(exc)})
                return json_response(self, {"ok": True,
                                            "message": f"Connected to {client.label} {version}."})

            if path == "/api/torrent/forget":
                wanted = str(payload.get("hash", "")).lower()
                kept = [t for t in library.load_settings().get("torrents") or []
                        if t.get("hash") != wanted]
                library.save_settings({"torrents": kept})
                return json_response(self, {"torrents": kept})

            if path == "/api/install/analyse":
                target = str(payload.get("path", "")).strip()
                if not target:
                    return json_response(self, {"error": "Choose something to install."}, 400)
                found = autoinstall.QUEUE.analyse(target)
                if found.get("error"):
                    return json_response(self, found, 400)
                return json_response(self, found)

            if path == "/api/install/start":
                target = str(payload.get("path", "")).strip()
                if not target or not Path(target).expanduser().exists():
                    return json_response(self, {"error": "That download is no longer there."}, 400)
                try:
                    size = int(float(payload.get("size_gb") or 0) * placement.GB)
                except (TypeError, ValueError):
                    size = 0
                task = autoinstall.QUEUE.add(
                    target, name=str(payload.get("name", "")),
                    target_root=str(payload.get("target_root", "")),
                    folder_name=str(payload.get("folder_name", "")),
                    mode=str(payload.get("mode", "")), ssd=str(payload.get("ssd", "")),
                    size=size, auto=bool(payload.get("auto")),
                    origin=str(payload.get("origin", "manual")),
                    installer_asks=bool(payload.get("installer_asks")))
                return json_response(self, {"task": task.to_json()})

            if path == "/api/install/cancel":
                problem = autoinstall.QUEUE.cancel(str(payload.get("id", "")))
                if problem:
                    return json_response(self, {"error": problem}, 400)
                return json_response(self, {"ok": True})

            if path == "/api/install/retry":
                task = autoinstall.QUEUE.retry(str(payload.get("id", "")),
                                               target_root=str(payload.get("target_root", "")),
                                               mode=str(payload.get("mode", "")))
                if not task:
                    return json_response(self, {"error": "That install is not in the list."}, 400)
                return json_response(self, {"task": task.to_json()})

            if path == "/api/install/remove":
                autoinstall.QUEUE.remove(str(payload.get("id", "")))
                return json_response(self, {"ok": True})

            if path == "/api/install/clear":
                autoinstall.QUEUE.clear_finished()
                return json_response(self, {"ok": True})

            if path == "/api/install/backlog":
                wanted = set(payload.get("paths") or [])
                queued = 0
                for item in autoinstall.WATCHER.backlog():
                    if wanted and item["path"] not in wanted:
                        continue
                    autoinstall.QUEUE.add(item["path"], auto=True, origin="incoming")
                    queued += 1
                return json_response(self, {"queued": queued})

            if path == "/api/library/save":
                return json_response(self, save_library(payload))

            if path == "/api/incoming/watch":
                return json_response(self, save_library({"watch_incoming": bool(payload.get("on"))}))

            if path == "/api/embed/place":
                embed.MANAGER.place(bool(payload.get("visible")), payload.get("rect"))
                return json_response(self, {"ok": True})

            if path == "/api/embed/popout":
                embed.MANAGER.pop_out()
                return json_response(self, {"embed": embed.MANAGER.current()})

            if path == "/api/embed/bringback":
                embed.MANAGER.bring_back()
                return json_response(self, {"embed": embed.MANAGER.current()})

            if path == "/api/embed/front":
                embed.MANAGER.front()
                return json_response(self, {"ok": True})

            if path == "/api/steam/close":
                outcome = steam.close_steam()
                _STEAM_SEEN["at"] = 0.0
                return json_response(self, {**outcome, "running": steam_running_cached()})

            if path == "/api/steam/start":
                return json_response(self, steam.open_steam())

            if path == "/api/tools/7zip":
                job = library.start_job(seven_zip_job)
                return json_response(self, {"job": job.id})

            if path == "/api/reveal":
                return json_response(self, download.reveal(payload.get("path", "")))

            if path == "/api/restart-elevated":
                if autoinstall.QUEUE.busy():
                    return json_response(self, {"error": "Wait until the current install "
                                                         "finishes, then restart."}, 400)
                library.save_settings({"run_as_admin": True})
                if not winproc.relaunch_elevated(["--no-elevate"]):
                    return json_response(self, {"error": "The permission prompt was declined, "
                                                         "so the app keeps running as it is."}, 400)

                def leave() -> None:
                    time.sleep(0.6)
                    embed.MANAGER.release_all()
                    os._exit(0)
                threading.Thread(target=leave, daemon=True).start()
                return json_response(self, {"restarting": True})

            if path == "/api/download":
                url = str(payload.get("url", "")).strip()
                folder = str(payload.get("folder", "")).strip() or str(download.default_folder())
                if not url:
                    return json_response(self, {"error": "Paste a link first."}, 400)
                job = library.start_job(download_job, url, folder)
                return json_response(self, {"job": job.id})

            if path == "/api/download/reveal":
                return json_response(self, download.reveal(payload.get("path", "")))

            if path == "/api/uninstall":
                program = payload.get("program") or {}
                if not program.get("uninstall") and not program.get("quiet"):
                    return json_response(self, {"error": "That program has no uninstaller."}, 400)
                user = None
                if payload.get("user"):
                    try:
                        user = find_user(payload["user"])
                    except ValueError:
                        user = None
                job = library.start_job(uninstall_job, program,
                                        bool(payload.get("silent")), user)
                return json_response(self, {"job": job.id})

            if path == "/api/leftovers":
                return json_response(
                    self, {"leftovers": uninstall.find_leftovers(payload.get("program") or {})}
                )

            if path == "/api/leftovers/remove":
                paths = [str(p) for p in (payload.get("paths") or [])]
                results = uninstall.remove_leftovers(paths)
                freed = sum(1 for r in results if r["removed"])
                return json_response(self, {"results": results, "removed": freed})

            if path == "/api/parser/preset":
                preset = payload.get("preset") or {}
                return json_response(
                    self,
                    {"parser": roms.preset_parser(preset, payload.get("retroarch", ""))},
                )

            if path == "/api/parser/blank":
                return json_response(self, {"parser": roms.new_parser()})

        except ValueError as exc:
            return json_response(self, {"error": str(exc)}, 400)
        except sgdb.SGDBError as exc:
            return json_response(self, {"error": str(exc)}, 502)
        except Exception as exc:
            return json_response(self, {"error": f"{type(exc).__name__}: {exc}"}, 500)

        return json_response(self, {"error": "Not found"}, 404)

    # -- state ------------------------------------------------------------

    def bootstrap(self) -> dict:
        refresh_users()
        root = STATE["steam_root"]
        settings = library.load_settings()
        return {
            "steam_root": str(root) if root else "",
            "users": [u.to_json() for u in STATE["users"]],
            "sources": scanners.available_sources(),
            "settings": settings,
            "steam_running": steam.steam_is_running(),
            "platform": platform.system(),
            "art_kinds": sgdb.ART_ORDER,
            "config_dir": str(library.config_dir()),
            "version": __import__("nsgm").__version__,

            "windowed": bool(STATE.get("windowed")),
            "embed_available": embed.available(),
            "admin": winproc.is_admin(),
            "seven_zip": tools.seven_zip(),
            "can_fetch_7zip": tools.can_fetch(),
            "torrent_kinds": list(torrentclient.KINDS),
        }


def list_users_safe(root) -> list:
    if not root:
        return []
    try:
        return steam.list_users(root)
    except OSError:
        return []


def add_torrent(url: str, name: str = "") -> tuple[dict, int]:
    """Give a magnet or .torrent link to the configured client, or the system."""
    settings = library.load_settings()
    client = torrentclient.from_settings(settings.get("torrent_client"))
    if client is None:
        if not url.lower().startswith("magnet:"):
            return {"error": "Set up a torrent client in Settings to send .torrent files to it."}, 400
        try:
            return resolve.open_magnet(url), 200
        except resolve.ResolveError as exc:
            return {"error": str(exc)}, 400

    torrent = b""
    magnet = ""
    if url.lower().startswith("magnet:"):
        magnet = url
        info_hash = torrentclient.info_hash(url)
        label = name or resolve.parse_magnet(url).get("name") or "Torrent"
    elif url.lower().startswith(("http://", "https://")):
        try:
            from . import archive
            torrent = archive.fetch_bytes(url)
            info = torrents.read(torrent)
        except (OSError, torrents.TorrentError) as exc:
            return {"error": f"That .torrent could not be read ({exc})."}, 400
        info_hash = info["hash"]
        label = name or info["name"] or "Torrent"
    else:
        return {"error": "That is not a magnet or a .torrent link."}, 400

    save_path = str(settings.get("incoming_folder") or "")
    try:
        client.add(magnet=magnet, torrent=torrent, save_path=save_path)
    except torrentclient.TorrentClientError as exc:
        return {"error": str(exc)}, 502
    tracked = [t for t in settings.get("torrents") or [] if t.get("hash") != info_hash]
    tracked.append({"hash": info_hash, "name": label[:160], "added": time.time()})
    library.save_settings({"torrents": tracked[-60:]})
    with _CACHE_LOCK:
        _TORRENTS_SEEN["at"] = 0.0
    where = f" into {save_path}" if save_path else ""
    return {"added": True, "client": client.label, "hash": info_hash,
            "message": f"Downloading with {client.label}{where}."}, 200


def serve(port: int = 8731, open_browser: bool = True) -> None:
    autoinstall.QUEUE.on_staged = stage_games
    autoinstall.QUEUE.load()
    autoinstall.QUEUE.start()
    autoinstall.WATCHER.start()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"  Game manager ready at {url}")
    print("  Leave this window open. Press Ctrl+C to stop.\n")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        httpd.server_close()
