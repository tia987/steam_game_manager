"""The store: browse catalogues from the sofa, press Install, play.

This is the console-style front end over what the rest of the app
already does. Picking a game here runs the same steps a person would
otherwise click through by hand:

1. find the right file for this computer (a GitHub release, or a direct
   link the catalogue gives)
2. download it, resuming if a previous attempt was cut off
3. hand it to the install queue, quietly where the installer allows it
4. write the Steam shortcut and artwork

The one step that cannot always happen at once is the last. Steam keeps
``shortcuts.vdf`` in memory and overwrites it on exit, so a shortcut
written while Steam is open is lost. When someone is streaming over
Moonlight, Steam is open by definition. So finished games wait in a
*pending* list, and Steam is restarted - by a small helper process that
outlives this one - either when the person leaves the store or straight
away, as they choose in settings.

Catalogues
----------
The open-source catalogue in ``catalog.py`` is always there. Anyone can
add more: a catalogue is a JSON file, on the web or on disk, shaped like
this::

    {
      "name": "My catalogue",
      "games": [
        {"name": "SuperTuxKart", "repo": "supertuxkart/stk-code",
         "tags": ["Racing"], "blurb": "Kart racing."},
        {"name": "Some Game",
         "downloads": {"windows": "https://example.org/game-win.zip",
                       "linux": "https://example.org/game.AppImage"},
         "cover": "https://example.org/cover.png"}
      ]
    }

A bare list of games is accepted too. ``repo`` means "the latest GitHub
release, picking the file that suits this computer"; ``asset`` narrows
that pick with a regular expression when a project publishes several
builds. ``downloads`` (or a single ``download``) are direct links.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from . import (autoinstall, catalog, download, drives, installers, library, proc, scanners, sgdb,
               steam)

USER_AGENT = "NonSteamGameManager/1.0 (+store)"

BUILTIN_ID = "builtin"
BUILTIN_NAME = "Open-source games"
STORE_TAG = "Store"
SHORTCUT_NAME = "Game Store"

CATALOGUE_HOURS = 6              # how long a fetched catalogue is trusted
ART_RETRY_DAYS = 7               # how long a failed artwork lookup is remembered
MAX_CATALOGUE_BYTES = 4 * 1024 * 1024
MAX_GAMES = 2000

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PLATFORM_KEYS = {
    "Windows": ("windows", "win", "win64"),
    "Linux": ("linux", "appimage"),
    "Darwin": ("mac", "macos", "osx", "darwin"),
}

# Set by the entry point when the store runs as its own full-screen window.
# Only then does restarting Steam also close and reopen the store.
STORE_WINDOW = False


class StoreError(RuntimeError):
    pass


class Stopped(Exception):
    """Cancelled from the store while downloading."""


_LOCK = threading.RLock()
_ART_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# small file helpers
# --------------------------------------------------------------------------

def _read_json(path: Path, default):
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), "utf-8")
    tmp.replace(path)


def _state_path() -> Path:
    return library.config_dir() / "store.json"


def _catalogue_cache_path() -> Path:
    return library.config_dir() / "store-catalogues.json"


def _art_cache_path() -> Path:
    return library.config_dir() / "store-art.json"


def load_state() -> dict:
    state = _read_json(_state_path(), {})
    state.setdefault("installed", {})     # game id -> what was installed and where
    state.setdefault("pending", [])       # game records waiting for Steam to close
    state.setdefault("store_shortcut", 0)
    return state


def _change_state(change) -> dict:
    with _LOCK:
        state = load_state()
        change(state)
        try:
            _write_json(_state_path(), state)
        except OSError:
            pass
        return state


def game_id(catalogue_id: str, name: str) -> str:
    raw = f"{catalogue_id}|{name.strip().lower()}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


# --------------------------------------------------------------------------
# catalogues
# --------------------------------------------------------------------------

def _clean_url(value) -> str:
    text = str(value or "").strip()
    if len(text) < 2000 and re.match(r"^https?://\S+$", text, re.I):
        return text
    return ""


def _clean_text(value, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _clean_game(raw, catalogue_id: str) -> dict | None:
    """One catalogue entry, made safe to show and to act on, or None."""
    if not isinstance(raw, dict):
        return None
    name = _clean_text(raw.get("name") or raw.get("title"), 120)
    if not name:
        return None

    repo = str(raw.get("repo") or "").strip()
    found = re.match(r"^https?://github\.com/([^/\s]+/[^/#?\s]+)", repo, re.I)
    if found:
        repo = found.group(1)
    repo = repo[:-4] if repo.endswith(".git") else repo
    if repo and not REPO_RE.match(repo):
        repo = ""

    asset = str(raw.get("asset") or "")[:200]
    if asset:
        try:
            re.compile(asset)
        except re.error:
            asset = ""

    downloads: dict[str, str] = {}
    listed = raw.get("downloads")
    if isinstance(listed, dict):
        for key, value in listed.items():
            url = _clean_url(value)
            if url:
                downloads[str(key).strip().lower()[:12]] = url
    single = _clean_url(raw.get("download") or raw.get("url"))
    if single:
        downloads.setdefault("any", single)

    tags = raw.get("tags") if isinstance(raw.get("tags"), list) else []
    try:
        size = max(0, int(raw.get("size") or 0))
    except (TypeError, ValueError):
        size = 0

    game = {
        "id": game_id(catalogue_id, name),
        "catalogue": catalogue_id,
        "name": name,
        "repo": repo,
        "asset": asset,
        "downloads": downloads,
        "tags": [_clean_text(t, 24) for t in tags if _clean_text(t, 24)][:8],
        "blurb": _clean_text(raw.get("blurb") or raw.get("description"), 600),
        "licence": _clean_text(raw.get("licence") or raw.get("license"), 40),
        "site": _clean_url(raw.get("site")),
        "note": _clean_text(raw.get("note"), 300),
        "needs_data": _clean_text(raw.get("needs_data"), 300),
        "cover": _clean_url(raw.get("cover")),
        "hero": _clean_url(raw.get("hero")),
        "logo": _clean_url(raw.get("logo")),
        "wide": _clean_url(raw.get("wide")),
        "size": size,
    }
    if not repo and not downloads and not game["site"]:
        return None
    return game


def parse_catalogue(blob, catalogue_id: str) -> dict:
    """Read a catalogue file into clean game records."""
    try:
        data = json.loads(blob)
    except (ValueError, UnicodeDecodeError) as exc:
        raise StoreError(f"That is not valid JSON ({exc}).") from exc
    if isinstance(data, list):
        data = {"games": data}
    if not isinstance(data, dict) or not isinstance(data.get("games"), list):
        raise StoreError('A catalogue needs a "games" list.')
    games, seen, skipped = [], set(), 0
    for raw in data["games"][:MAX_GAMES]:
        game = _clean_game(raw, catalogue_id)
        if not game or game["id"] in seen:
            skipped += 1
            continue
        seen.add(game["id"])
        games.append(game)
    return {"name": _clean_text(data.get("name"), 60), "games": games, "skipped": skipped}


def _raw_github(url: str) -> str:
    """A github.com/.../blob/... page address, turned into the file itself."""
    found = re.match(r"^https://github\.com/([^/]+)/([^/]+)/blob/(.+)$", url, re.I)
    if found:
        return f"https://raw.githubusercontent.com/{found.group(1)}/{found.group(2)}/{found.group(3)}"
    return url


def fetch_source(source: str) -> bytes:
    """The bytes of a catalogue, from a web address or a file on disk."""
    source = source.strip()
    if re.match(r"^https?://", source, re.I):
        request = urllib.request.Request(_raw_github(source), headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                blob = response.read(MAX_CATALOGUE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise StoreError(f"The server answered HTTP {exc.code}.") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise StoreError(f"Could not reach it ({getattr(exc, 'reason', exc)}).") from exc
    else:
        path = Path(source).expanduser()
        if not path.is_file():
            raise StoreError("There is no file at that path.")
        try:
            with open(path, "rb") as handle:
                blob = handle.read(MAX_CATALOGUE_BYTES + 1)
        except OSError as exc:
            raise StoreError(f"Could not read it ({exc.strerror or exc}).") from exc
    if len(blob) > MAX_CATALOGUE_BYTES:
        raise StoreError("That catalogue is larger than 4 MB, which is more than any list of "
                         "games should need.")
    return blob


def test_source(source: str) -> dict:
    """Fetch and read a catalogue without saving it, so it can be checked first."""
    if not source.strip():
        raise StoreError("Type a web address or a file path first.")
    parsed = parse_catalogue(fetch_source(source), "test")
    if not parsed["games"]:
        raise StoreError("It was read, but it has no usable games in it.")
    return {
        "name": parsed["name"],
        "count": len(parsed["games"]),
        "skipped": parsed["skipped"],
        "sample": [g["name"] for g in parsed["games"][:5]],
    }


def builtin_games() -> list[dict]:
    return [g for g in (_clean_game(raw, BUILTIN_ID) for raw in catalog.GAMES) if g]


def clean_catalogue_list(items) -> list[dict]:
    """The user's catalogue list as saved in settings."""
    out, seen = [], set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source", "")).strip()[:2000]
        if not source or source.lower() in seen:
            continue
        seen.add(source.lower())
        cid = str(item.get("id") or "").strip()
        if not re.fullmatch(r"[a-f0-9]{8,16}", cid):
            cid = uuid.uuid4().hex[:10]
        out.append({"id": cid, "source": source, "name": _clean_text(item.get("name"), 60),
                    "enabled": item.get("enabled", True) is not False})
    return out


def load_catalogues(force: bool = False) -> list[dict]:
    """Every catalogue with its games; user ones are re-read when stale."""
    settings = library.load_settings()
    out = [{"id": BUILTIN_ID, "name": BUILTIN_NAME, "builtin": True, "enabled": True,
            "source": "", "error": "", "games": builtin_games()}]
    with _LOCK:
        cache = _read_json(_catalogue_cache_path(), {})
        changed = False
        for entry in clean_catalogue_list(settings.get("store_catalogues")):
            cid, source = entry["id"], entry["source"]
            cached = cache.get(cid) if isinstance(cache.get(cid), dict) else None
            stale = (not cached or cached.get("source") != source
                     or time.time() - cached.get("fetched", 0) > CATALOGUE_HOURS * 3600)
            if entry["enabled"] and (force or stale):
                try:
                    parsed = parse_catalogue(fetch_source(source), cid)
                    cached = {"fetched": time.time(), "source": source, "name": parsed["name"],
                              "games": parsed["games"], "error": ""}
                except StoreError as exc:
                    # Keep whatever was read last time: a site being down for
                    # an hour should not empty the shelf.
                    old = cached if cached and cached.get("source") == source else {}
                    cached = {"fetched": time.time(), "source": source,
                              "name": old.get("name", ""), "games": old.get("games", []),
                              "error": str(exc)}
                cache[cid] = cached
                changed = True
            cached = cached or {}
            out.append({
                "id": cid,
                "name": entry["name"] or cached.get("name") or source,
                "builtin": False,
                "enabled": entry["enabled"],
                "source": source,
                "error": cached.get("error", "") if entry["enabled"] else "",
                "games": cached.get("games", []) if entry["enabled"] else [],
            })
        if changed:
            try:
                _write_json(_catalogue_cache_path(), cache)
            except OSError:
                pass
    return out


def find_game(gid: str) -> dict | None:
    for entry in load_catalogues():
        for game in entry["games"]:
            if game["id"] == gid:
                return game
    return None


def platform_download(game: dict, system: str = "") -> str:
    """The direct link in a catalogue entry that suits this computer."""
    system = system or platform.system()
    downloads = game.get("downloads") or {}
    for key in PLATFORM_KEYS.get(system, ()):
        if downloads.get(key):
            return downloads[key]
    return downloads.get("any", "")


def installable(game: dict) -> bool:
    return bool(game.get("repo") or platform_download(game))


# --------------------------------------------------------------------------
# artwork for the store's own shelves
# --------------------------------------------------------------------------

def _art_client() -> sgdb.SteamGridDB | None:
    key = str(library.load_settings().get("api_key", "")).strip()
    return sgdb.SteamGridDB(key) if key else None


def _lookup_art(game: dict, kinds: list[str], cache: dict, client) -> dict:
    """SteamGridDB art for one game, from the cache where possible."""
    key = game["name"].lower()
    entry = cache.get(key) if isinstance(cache.get(key), dict) else {}
    missing = [k for k in kinds if k not in entry]
    recent = time.time() - entry.get("fetched", 0) < ART_RETRY_DAYS * 86400
    if not missing or client is None or (entry.get("no_match") and recent):
        return entry
    try:
        sgdb_id = entry.get("sgdb_id")
        if not sgdb_id:
            found = client.match(game["name"])
            if not found:
                entry = {"no_match": True, "fetched": time.time()}
                cache[key] = entry
                return entry
            sgdb_id = found["id"]
        picks = client.best_artwork(int(sgdb_id), missing)
        entry = {**entry, "sgdb_id": sgdb_id, "fetched": time.time()}
        entry.pop("no_match", None)
        for kind in missing:
            entry[kind] = (picks.get(kind) or {}).get("url", "")
            if kind == "capsule" and picks.get(kind):
                entry["thumb"] = picks[kind].get("thumb") or entry[kind]
    except sgdb.SGDBError:
        return entry           # a network hiccup is not "no art exists"
    cache[key] = entry
    return entry


def covers(ids: list[str], budget: int = 12) -> dict:
    """Cover images for the games on screen.

    Looks up at most ``budget`` new ones per call. A game is only in the
    answer once it is settled, with a cover or known to have none, so the
    caller can tell "no art" from "not looked at yet" and ask again.
    """
    wanted = set(ids)
    games = [g for c in load_catalogues() for g in c["games"] if g["id"] in wanted]
    client = _art_client()
    out: dict[str, str] = {}

    def settled(entry: dict) -> bool:
        return "capsule" in entry or bool(entry.get("no_match"))

    with _ART_LOCK:
        cache = _read_json(_art_cache_path(), {})
        looked = 0
        for game in games:
            if game.get("cover"):
                out[game["id"]] = game["cover"]
                continue
            entry = cache.get(game["name"].lower()) or {}
            if not settled(entry) and client is not None and looked < budget:
                entry = _lookup_art(game, ["capsule"], cache, client)
                looked += 1
            if settled(entry) or client is None:
                out[game["id"]] = entry.get("thumb") or entry.get("capsule") or ""
        try:
            _write_json(_art_cache_path(), cache)
        except OSError:
            pass
    return {"covers": out, "complete": all(gid in out for gid in wanted),
            "artwork_key": client is not None}


def detail_art(gid: str) -> dict:
    """Hero banner and logo for a game's page."""
    game = find_game(gid)
    if not game:
        raise StoreError("That game is not in any catalogue.")
    art = {"cover": game.get("cover", ""), "hero": game.get("hero", ""), "logo": game.get("logo", "")}
    if art["hero"] and art["logo"]:
        return art
    client = _art_client()
    with _ART_LOCK:
        cache = _read_json(_art_cache_path(), {})
        entry = _lookup_art(game, ["capsule", "hero", "logo"], cache, client)
        try:
            _write_json(_art_cache_path(), cache)
        except OSError:
            pass
    for kind, source in (("cover", "capsule"), ("hero", "hero"), ("logo", "logo")):
        art[kind] = art[kind] or entry.get(source, "")
    return art


# --------------------------------------------------------------------------
# installing
# --------------------------------------------------------------------------

ACTIVE_PHASES = ("resolving", "downloading", "installing", "waiting", "adding")


@dataclass
class StoreJob:
    id: str                       # the game id
    name: str
    phase: str = "resolving"      # resolving downloading installing waiting adding done failed cancelled
    progress: float = -1.0
    message: str = "Finding the download"
    task: str = ""                # the install queue's task id
    action: str = ""              # what a waiting install needs
    error: str = ""
    started: float = 0.0
    finished: float = 0.0
    cancel: bool = False

    def to_json(self) -> dict:
        data = asdict(self)
        data.pop("cancel", None)
        return data


JOBS: dict[str, StoreJob] = {}


def download_folder(settings: dict | None = None) -> Path:
    settings = settings or library.load_settings()
    chosen = str(settings.get("store_download_folder") or "").strip()
    return Path(chosen).expanduser() if chosen else download.default_folder() / "Game Store downloads"


def library_ready(settings: dict | None = None) -> bool:
    settings = settings or library.load_settings()
    return any(f.get("enabled", True) is not False and f.get("path")
               for f in settings.get("library_folders") or [])


def library_choices() -> list[dict]:
    """Places a first library folder could go, for picking with a controller."""
    if platform.system() == "Windows":
        roots = [Path(f"{letter}:\\") for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ"
                 if Path(f"{letter}:\\").exists()]
        candidates = [root / "Games" for root in roots]
    else:
        candidates = [Path.home() / "Games"]
    out = []
    for path in candidates:
        room = drives.space(path)
        if room.get("ok"):
            out.append({"path": str(path), "free": room["free"], "total": room["total"]})
    return out


def set_library(path: str) -> None:
    if not any(c["path"] == path for c in library_choices()):
        raise StoreError("Pick one of the offered folders.")
    settings = library.load_settings()
    folders = list(settings.get("library_folders") or [])
    if not any(str(f.get("path", "")).lower() == path.lower() for f in folders):
        folders.append({"path": path, "kind": "auto", "reserve_gb": 10.0, "enabled": True})
    Path(path).mkdir(parents=True, exist_ok=True)
    library.save_settings({"library_folders": folders})
    autoinstall.QUEUE.retry_waiting("folder")


def install(gid: str) -> StoreJob:
    """Download and install one game, in the background."""
    with _LOCK:
        job = JOBS.get(gid)
        if job and job.phase in ACTIVE_PHASES:
            return job
        game = find_game(gid)
        if not game:
            raise StoreError("That game is not in any catalogue.")
        if not installable(game):
            raise StoreError("This one has no download for this computer. Its project site "
                             "has the details.")
        job = StoreJob(id=gid, name=game["name"], started=time.time())
        JOBS[gid] = job
    threading.Thread(target=_run_job, args=(job, game), daemon=True).start()
    return job


def cancel(gid: str) -> None:
    job = JOBS.get(gid)
    if not job or job.phase not in ACTIVE_PHASES:
        raise StoreError("Nothing is being installed for that game.")
    job.cancel = True
    job.message = "Stopping"
    if job.task:
        autoinstall.QUEUE.cancel(job.task)


def retry(gid: str) -> StoreJob:
    """Try a waiting install again, after its reason has been dealt with."""
    job = JOBS.get(gid)
    if job and job.phase == "waiting" and job.task:
        autoinstall.QUEUE.retry(job.task)
        return job
    return install(gid)


def _resolve_download(game: dict, settings: dict) -> tuple[str, str]:
    """(url, version) for the file that suits this computer."""
    direct = platform_download(game)
    if direct and not game.get("repo"):
        return direct, ""
    token = str(settings.get("github_token", "")).strip()
    if game.get("asset"):
        # A pattern from the catalogue: pick among the files it allows.
        payload, _remaining = catalog._request(
            f"{catalog.API}/repos/{game['repo']}/releases/latest", token)
        pattern = re.compile(game["asset"], re.I)
        assets = [a for a in payload.get("assets") or [] if pattern.search(a.get("name", ""))]
        best = catalog.choose_asset(assets)
        if not best and assets:
            best = {"url": assets[0].get("browser_download_url", "")}
        if best and best.get("url"):
            return best["url"], payload.get("tag_name") or ""
    else:
        found = catalog.resolve(game, library.config_dir(), token=token)
        if found.get("asset") and found["asset"].get("url"):
            return found["asset"]["url"], found.get("version", "")
    if direct:
        return direct, ""
    raise StoreError("The latest release has no file for this computer.")


def _run_job(job: StoreJob, game: dict) -> None:
    try:
        _install_steps(job, game)
    except Stopped:
        job.phase, job.message = "cancelled", "Cancelled. Installing again picks up the download."
    except catalog.RateLimit as exc:
        job.phase, job.error = "failed", str(exc)
        job.message = "GitHub's hourly limit is used up. A GitHub token in Settings raises it."
    except Exception as exc:                     # anything at all must end the job cleanly
        job.phase, job.error = "failed", str(exc) or type(exc).__name__
        job.message = job.error
    finally:
        if job.phase in ("cancelled", "failed", "done"):
            job.finished = job.finished or time.time()
            job.progress = -1.0 if job.phase != "done" else 1.0


def _install_steps(job: StoreJob, game: dict) -> None:
    settings = library.load_settings()
    url, version = _resolve_download(game, settings)
    if job.cancel:
        raise Stopped()

    folder = download_folder(settings)
    folder.mkdir(parents=True, exist_ok=True)
    job.phase, job.progress, job.message = "downloading", 0.0, "Starting the download"

    def progress(done: int, total: int) -> None:
        if job.cancel:
            raise Stopped()
        job.progress = done / total if total else -1.0
        mb = done / 1048576
        job.message = (f"{mb:,.0f} MB of {total / 1048576:,.0f} MB" if total
                       else f"{mb:,.0f} MB")

    result = download.download(url, folder, progress=progress)
    if job.cancel:
        raise Stopped()

    job.phase, job.progress, job.message = "installing", -1.0, "Waiting its turn"
    task = autoinstall.QUEUE.add(result["path"], name=game["name"], mode="quiet",
                                 origin="store", size=int(game.get("size") or 0))
    job.task = task.id
    task = _follow(job, task.id)

    games = task.result.get("games") or []
    record = {"name": game["name"], "catalogue": game["catalogue"], "version": version,
              "dir": task.target_dir, "exe": "", "short_id": 0, "steam": "none",
              "at": time.time()}
    if games:
        entry = _steam_record(game, games[0])
        prefer_direct = bool(settings.get("prefer_direct"))
        record["exe"] = entry.get("exe", "")
        record["short_id"] = library.preview_short_id(entry, prefer_direct)
        job.phase, job.message = "adding", "Adding to Steam"
        try:
            record["steam"] = add_or_queue([entry], settings)
        except Exception as exc:          # installed is installed, whatever Steam says
            record["steam"], record["steam_error"] = "failed", str(exc)
    _change_state(lambda state: state["installed"].__setitem__(game["id"], record))
    _tidy(Path(result["path"]), folder, settings)

    job.phase, job.finished = "done", time.time()
    job.message = {
        "added": "Installed and added to Steam.",
        "pending": "Installed. It appears in Steam after Steam restarts.",
        "none": "Installed, but no program to start was found in it.",
        "failed": f"Installed, but not added to Steam: {record.get('steam_error', '')}",
    }.get(record["steam"], "Installed.")
    if record["steam"] == "pending" and settings.get("store_restart") == "now":
        threading.Thread(target=_restart_when_idle, daemon=True).start()


def _follow(job: StoreJob, task_id: str) -> autoinstall.Task:
    """Mirror an install queue task into the store job until it finishes."""
    while True:
        task = autoinstall.QUEUE.find(task_id)
        if task is None:
            raise StoreError("The install disappeared from the queue.")
        if job.cancel and task.status in ("queued", "waiting", "checking", "installing"):
            autoinstall.QUEUE.cancel(task_id)
        if task.status == "done":
            return task
        if task.status == "cancelled":
            raise Stopped()
        if task.status == "failed":
            raise StoreError(task.message or "The install failed.")
        if task.status == "waiting":
            job.phase, job.action, job.message = "waiting", task.action, task.message
        else:
            job.phase, job.action = "installing", ""
            job.progress = task.progress
            job.message = task.message or "Installing"
        time.sleep(0.5)


def _steam_record(game: dict, found: dict) -> dict:
    """The installed game, named and dressed the way the catalogue has it."""
    entry = dict(found)
    entry["title"] = game["name"]
    entry["sgdb_hint"] = game["name"]
    entry["source"] = "store"
    entry["tags"] = [t for t in entry.get("tags") or [] if t != "Installed"] + [STORE_TAG]
    art = {kind: game[key] for kind, key in
           (("capsule", "cover"), ("hero", "hero"), ("logo", "logo"), ("wide", "wide"))
           if game.get(key)}
    if art:
        entry["art"] = art
    return entry


def _tidy(path: Path, folder: Path, settings: dict) -> None:
    """Delete a store download once installed, unless asked to keep them."""
    if settings.get("store_keep_downloads"):
        return
    try:
        if folder.resolve() not in path.resolve().parents:
            return
    except OSError:
        return
    for part in installers.parts_of(path) if path.is_file() else [path]:
        try:
            if part.is_dir():
                shutil.rmtree(part)
            else:
                part.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------
# Steam
# --------------------------------------------------------------------------

def _steam_user(settings: dict):
    root = steam.find_steam_root()
    users = steam.list_users(root) if root else []
    wanted = str(settings.get("account") or "")
    return next((u for u in users if u.id3 == wanted), users[0] if users else None)


def _same(a: dict, b: dict) -> bool:
    return a.get("title") == b.get("title") and a.get("exe") == b.get("exe")


def add_or_queue(entries: list[dict], settings: dict) -> str:
    """Write shortcuts now if Steam is closed, otherwise keep them for later.

    Returns ``added`` or ``pending``.
    """
    if not steam.steam_is_running():
        user = _steam_user(settings)
        if user is None:
            raise StoreError("No Steam account was found on this computer.")
        job = library.Job(id=uuid.uuid4().hex[:12])
        library.apply_games(job, user, entries, settings)
        return "added"

    def keep(state: dict) -> None:
        pending = [p for p in state["pending"] if not any(_same(p, e) for e in entries)]
        state["pending"] = pending + entries
    _change_state(keep)
    return "pending"


def apply_pending(log=print) -> int:
    """Write every waiting shortcut. Steam must be closed."""
    state = load_state()
    pending = state["pending"]
    if not pending:
        return 0
    settings = library.load_settings()
    user = _steam_user(settings)
    if user is None:
        raise StoreError("No Steam account was found on this computer.")
    job = library.Job(id=uuid.uuid4().hex[:12])
    library.apply_games(job, user, pending, settings)
    for line in job.log:
        log(line)
    log(job.message)

    def done(state: dict) -> None:
        added = pending
        state["pending"] = [p for p in state["pending"] if not any(_same(p, a) for a in pending)]
        for record in state["installed"].values():
            if record.get("steam") == "pending" and any(
                    a.get("title") == record.get("name") for a in added):
                record["steam"] = "added"
        if any(a.get("source") == "store-self" for a in pending):
            state["store_shortcut_added"] = True
    _change_state(done)
    return len(pending)


def busy() -> bool:
    """Whether leaving now would cut something off."""
    return (any(j.phase in ACTIVE_PHASES for j in JOBS.values())
            or autoinstall.QUEUE.busy())


def restart_when_idle(relaunch: bool | None = None) -> dict:
    """Restart Steam once nothing is installing any more."""
    threading.Thread(target=_restart_when_idle, args=(relaunch,), daemon=True).start()
    return {"started": False, "message": "Steam restarts once the installs finish."}


def _restart_when_idle(relaunch: bool | None = None) -> None:
    """The 'restart straight away' setting: wait for every install to end first."""
    deadline = time.time() + 6 * 3600
    while time.time() < deadline:
        time.sleep(3)
        if busy():
            continue
        if not load_state()["pending"]:
            return
        refresh_steam(relaunch=STORE_WINDOW if relaunch is None else relaunch)
        return


def self_command(*args: str) -> tuple[list[str], str]:
    """How to start this program again: (command, working folder)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args], str(Path(sys.executable).parent)
    package_parent = str(Path(__file__).resolve().parent.parent)
    return [sys.executable, "-m", "nsgm", *args], package_parent


def _spawn_detached(command: list[str], cwd: str) -> None:
    """Start a process that carries on after this one, and after Steam, exit."""
    kwargs: dict = {"cwd": cwd, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        detached, group, breakaway = 0x00000008, 0x00000200, 0x01000000
        try:
            # Steam runs what it launches inside a job object, and closing
            # Steam can take everything in that job with it. Breaking away
            # keeps the helper alive.
            subprocess.Popen(command, creationflags=detached | group | breakaway, **kwargs)
            return
        except OSError:
            pass
        if _spawn_via_wmi(command, cwd):
            # The job does not allow breaking away. A process started by
            # Windows' own process service belongs to no job at all.
            return
        subprocess.Popen(command, creationflags=detached | group, **kwargs)
        return
    subprocess.Popen(command, start_new_session=True, **kwargs)


def _spawn_via_wmi(command: list[str], cwd: str) -> bool:
    """Start a process through Win32_Process.Create, outside any job object."""
    line = subprocess.list2cmdline(command).replace("'", "''")
    folder = cwd.replace("'", "''")
    script = ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
              f"-Arguments @{{CommandLine='{line}'; CurrentDirectory='{folder}'}}; "
              "exit $r.ReturnValue")
    try:
        result = proc.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


_REFRESHING = {"at": 0.0}


def refresh_steam(relaunch: bool) -> dict:
    """Hand the restart to a helper process, then leave if this is the store window.

    The store is often itself something Steam launched, so it cannot be the
    one to close Steam and keep going: it goes first, and the helper does
    the rest.
    """
    if time.time() - _REFRESHING["at"] < 30:
        return {"started": False, "message": "Steam is already being restarted."}
    _REFRESHING["at"] = time.time()
    args = ["--steam-refresh"] + (["--relaunch-store"] if relaunch else [])
    command, cwd = self_command(*args)
    _spawn_detached(command, cwd)
    leaving = STORE_WINDOW
    if leaving:
        def leave() -> None:
            time.sleep(0.8)
            try:
                from . import embed
                embed.MANAGER.release_all()
            finally:
                os._exit(0)
        threading.Thread(target=leave, daemon=True).start()
    return {"started": True, "leaving": leaving,
            "message": "Restarting Steam. The new games appear when it is back."}


def run_refresh(relaunch: bool) -> int:
    """The helper: close Steam, write pending shortcuts, open Steam again."""
    log_path = library.config_dir() / "store-refresh.log"
    lines: list[str] = []

    def log(text: str) -> None:
        lines.append(f"{time.strftime('%H:%M:%S')}  {text}")
        try:
            log_path.write_text("\n".join(lines[-200:]) + "\n", "utf-8")
        except OSError:
            pass

    time.sleep(2.0)              # let the store window finish closing
    log("Closing Steam")
    closed = steam.close_steam(timeout=90)
    log(closed["message"])
    code = 0
    if closed["ok"]:
        time.sleep(2.0)          # Steam writes its own files on the way out
        try:
            count = apply_pending(log)
            log(f"{count} shortcut(s) written.")
        except Exception as exc:
            log(f"Could not write the shortcuts: {exc}")
            code = 1
    else:
        code = 1

    settings = library.load_settings()
    big_picture = settings.get("store_reopen", "bigpicture") != "library"
    log("Starting Steam" + (" in Big Picture" if big_picture else ""))
    opened = steam.open_big_picture() if big_picture else steam.open_steam()
    log(opened["message"])

    if relaunch:
        deadline = time.time() + 90
        while time.time() < deadline and not steam.steam_is_running():
            time.sleep(1)
        time.sleep(10)           # Steam is running before it is ready for links
        state = load_state()
        short_id = int(state.get("store_shortcut") or 0)
        if short_id and state.get("store_shortcut_added"):
            log("Opening the store again through Steam")
            steam.open_uri(f"steam://rungameid/{steam.run_game_id(short_id)}")
        else:
            log("Opening the store again")
            command, cwd = self_command("--store")
            _spawn_detached(command, cwd)
    return code


def play(gid: str) -> dict:
    record = load_state()["installed"].get(gid)
    if not record:
        raise StoreError("That game is not installed from the store.")
    if record.get("steam") == "added" and record.get("short_id"):
        steam.open_uri(f"steam://rungameid/{steam.run_game_id(int(record['short_id']))}")
        return {"message": f"Starting {record['name']} through Steam."}
    if record.get("steam") == "pending":
        raise StoreError("Restart Steam first: it does not know about this game yet.")
    raise StoreError("No program to start was found in this game's folder.")


# --------------------------------------------------------------------------
# the store as a Steam shortcut of its own
# --------------------------------------------------------------------------

def _artwork_file(name: str) -> str:
    from .server import WEB_DIR
    path = WEB_DIR / "store-art" / name
    return path.as_uri() if path.is_file() else ""


def shortcut_entry() -> dict:
    """A Steam shortcut that opens this program straight into the store."""
    if getattr(sys, "frozen", False):
        exe, args, start_dir = sys.executable, "--store", str(Path(sys.executable).parent)
    else:
        exe = sys.executable
        if os.name == "nt":
            windowless = Path(sys.executable).with_name("pythonw.exe")
            if windowless.is_file():
                exe = str(windowless)       # no console window behind the store
        args = "-m nsgm --store"
        start_dir = str(Path(__file__).resolve().parent.parent)
    entry = scanners.make_game(SHORTCUT_NAME, "store-self", exe=exe, args=args,
                               start_dir=start_dir, tags=[STORE_TAG])
    entry["launch_mode"] = "direct"
    art = {kind: _artwork_file(f"{kind}.png") for kind in ("capsule", "wide", "hero", "logo", "icon")}
    entry["art"] = {k: v for k, v in art.items() if v}
    return entry


def add_store_to_steam() -> dict:
    settings = library.load_settings()
    entry = shortcut_entry()
    short_id = library.preview_short_id(entry, True)
    _change_state(lambda state: state.__setitem__("store_shortcut", short_id))
    outcome = add_or_queue([entry], settings)
    if outcome == "added":
        _change_state(lambda state: state.__setitem__("store_shortcut_added", True))
        return {"outcome": outcome, "message": f"“{SHORTCUT_NAME}” is in your Steam library."}
    return {"outcome": outcome,
            "message": f"“{SHORTCUT_NAME}” appears in Steam after Steam restarts."}


# --------------------------------------------------------------------------
# what the interface reads
# --------------------------------------------------------------------------

SETTING_KEYS = {
    "store_restart": ("exit", "now"),
    "store_reopen": ("bigpicture", "library"),
}


def store_settings(settings: dict | None = None) -> dict:
    settings = settings or library.load_settings()
    return {
        "restart": settings.get("store_restart", "exit"),
        "reopen": settings.get("store_reopen", "bigpicture"),
        "keep_downloads": bool(settings.get("store_keep_downloads")),
        "catalogues": clean_catalogue_list(settings.get("store_catalogues")),
    }


def save_store_settings(payload: dict) -> dict:
    patch: dict = {}
    for key, allowed in SETTING_KEYS.items():
        short = key.replace("store_", "")
        if payload.get(short) in allowed:
            patch[key] = payload[short]
    if "keep_downloads" in payload:
        patch["store_keep_downloads"] = bool(payload["keep_downloads"])
    if "catalogues" in payload:
        patch["store_catalogues"] = clean_catalogue_list(payload["catalogues"])
    if patch:
        library.save_settings(patch)
    return store_settings()


def overview(force: bool = False) -> dict:
    """Everything the store shows when it opens."""
    settings = library.load_settings()
    catalogues = load_catalogues(force=force)
    games, tags = [], set()
    for entry in catalogues:
        for game in entry["games"]:
            item = dict(game)
            item["catalogue_name"] = entry["name"]
            item["installable"] = installable(game)
            games.append(item)
            tags.update(game.get("tags") or [])
    games.sort(key=lambda g: g["name"].lower())
    state = load_state()
    return {
        "catalogues": [{k: v for k, v in c.items() if k != "games"} | {"count": len(c["games"])}
                       for c in catalogues],
        "games": games,
        "tags": sorted(tags),
        "settings": store_settings(settings),
        "library_ready": library_ready(settings),
        "library_choices": [] if library_ready(settings) else library_choices(),
        "artwork_key": bool(str(settings.get("api_key", "")).strip()),
        "store_shortcut": bool(state.get("store_shortcut_added")),
        "store_window": STORE_WINDOW,
        "platform": platform.system(),
        **status(),
    }


def _steam_running() -> bool:
    from .server import steam_running_cached     # asked at most every few seconds
    return steam_running_cached()


def status() -> dict:
    """What changes while the store is open; polled."""
    from . import embed
    state = load_state()
    return {
        "jobs": {gid: job.to_json() for gid, job in JOBS.items()},
        "installed": state["installed"],
        "pending": [p.get("title", "") for p in state["pending"]],
        "busy": busy(),
        "embed": embed.MANAGER.current(),
        "steam_running": _steam_running(),
    }
