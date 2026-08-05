"""Settings, the record of what this tool added, and the apply pipeline."""

from __future__ import annotations

import json
import os
import platform
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import scanners, sgdb, steam
from .feeds import DEFAULT_FEEDS
from .roms import run_parser

MANAGED_TAG = "Added by Game Manager"


def config_dir() -> Path:
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
        path = base / "NonSteamGameManager"
    elif system == "Darwin":
        path = Path.home() / "Library/Application Support/NonSteamGameManager"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        path = base / "nonsteam-game-manager"
    path.mkdir(parents=True, exist_ok=True)
    return path


DEFAULT_SETTINGS: dict = {
    "api_key": "",
    "sources": [s["key"] for s in scanners.available_sources() if s["default"]],
    "folders": [],           # [{"path": ..., "depth": 1}]
    "parsers": [],           # ROM parsers, see roms.new_parser
    "prefer_direct": False,  # launch through the storefront by default
    "art_kinds": ["capsule", "wide", "hero", "logo"],
    "overwrite_art": False,
    "allow_nsfw": False,
    "allow_animated": False,
    "add_tag": True,
    "feeds": [dict(f) for f in DEFAULT_FEEDS],
    "watched": [],   # pages watched for new downloads
    "hide_steam_games": True,
    "github_token": "",
}


def load_settings() -> dict:
    path = config_dir() / "settings.json"
    settings = dict(DEFAULT_SETTINGS)
    if path.exists():
        try:
            stored = json.loads(path.read_text("utf-8"))
            if isinstance(stored, dict):
                settings.update(stored)
        except (OSError, json.JSONDecodeError):
            pass
    return settings


def save_settings(settings: dict) -> None:
    path = config_dir() / "settings.json"
    merged = load_settings()
    merged.update(settings or {})
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, indent=2), "utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# record of shortcuts this tool created
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# the last scan, kept so a reload does not undo the work
# --------------------------------------------------------------------------

def session_path() -> Path:
    return config_dir() / "session.json"


def save_session(games: list, matches: dict) -> None:
    """Remember what was found and which covers were matched.

    Both cost real time - a scan walks the disk, matching asks
    SteamGridDB once per game - so losing them to a page refresh was
    paying that price twice for nothing.
    """
    try:
        payload = {"saved": time.time(), "games": games[:3000], "matches": matches}
        tmp = session_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), "utf-8")
        tmp.replace(session_path())
    except (OSError, TypeError, ValueError):
        pass


def load_session() -> dict:
    path = session_path()
    if not path.exists():
        return {"games": [], "matches": {}}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"games": [], "matches": {}}
    return {
        "games": data.get("games") or [],
        "matches": data.get("matches") or {},
        "saved": data.get("saved", 0),
    }


def manifest_path(id3: str) -> Path:
    return config_dir() / f"managed-{id3}.json"


def load_manifest(id3: str) -> dict[str, dict]:
    path = manifest_path(id3)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_manifest(id3: str, data: dict) -> None:
    path = manifest_path(id3)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), "utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------

def collect_games(settings: dict) -> tuple[list[dict], list[str]]:
    """Run every enabled source, folder and ROM parser, then de-duplicate."""
    notes: list[str] = []
    games, scan_notes = scanners.run_scanners(settings.get("sources") or [])
    notes.extend(scan_notes)

    for folder in settings.get("folders") or []:
        if isinstance(folder, str):
            folder = {"path": folder, "depth": 1}
        if not folder.get("path"):
            continue
        games.extend(scanners.scan_folder(folder["path"], int(folder.get("depth", 1)), notes))

    for parser in settings.get("parsers") or []:
        if parser.get("enabled", True):
            try:
                games.extend(run_parser(parser, notes))
            except Exception as exc:
                notes.append(f"{parser.get('name', 'Parser')}: {exc}")

    # Games Steam already has need no adding. Offering them back is noise
    # at best, and a second shortcut to something already in the library
    # at worst.
    owned: dict = {}
    if settings.get("hide_steam_games", True):
        root = steam.find_steam_root()
        if root:
            try:
                owned = steam.installed_apps(root)
            except OSError:
                owned = {}
    if owned:
        before = len(games)
        games = [g for g in games if g.get("title", "").strip().lower() not in owned]
        skipped = before - len(games)
        if skipped:
            notes.append(f"Skipped {skipped} titles Steam already has installed.")

    # Same title from two sources is common (a game in both GOG and a folder
    # scan). Keep the first, which follows source order, and drop the rest.
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for game in games:
        if not game.get("title"):
            continue
        key = (game["title"].strip().lower(), (game.get("exe") or "").lower())
        title_only = (game["title"].strip().lower(), "")
        if key in seen or (title_only in seen and game.get("source") in ("folder", "programs")):
            continue
        seen.add(key)
        seen.add(title_only)
        game["uid"] = uuid.uuid4().hex[:12]
        unique.append(game)

    unique.sort(key=lambda g: g["title"].lower())
    return unique, notes


def resolve_launch(game: dict, prefer_direct: bool) -> tuple[str, str, str]:
    """Decide the exe, arguments and working directory for one game."""
    direct_exe = (game.get("exe") or "").strip()
    launcher_exe = (game.get("launcher_exe") or "").strip()
    launcher_args = (game.get("launcher_args") or "").strip()

    use_direct = prefer_direct or not launcher_exe
    if game.get("launch_mode") == "direct":
        use_direct = True
    elif game.get("launch_mode") == "launcher":
        use_direct = False

    if use_direct and direct_exe:
        return direct_exe, (game.get("args") or ""), (game.get("start_dir") or "")
    if launcher_exe:
        return launcher_exe, launcher_args, (game.get("start_dir") or "")
    return direct_exe, (game.get("args") or ""), (game.get("start_dir") or "")


def preview_short_id(game: dict, prefer_direct: bool) -> int:
    exe, _args, _cwd = resolve_launch(game, prefer_direct)
    return steam.shortcut_id(steam.quote_exe(exe), game.get("title", "")) if exe else 0


# --------------------------------------------------------------------------
# background jobs
# --------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    total: int = 0
    done: int = 0
    stage: str = "starting"
    message: str = ""
    finished: bool = False
    error: str = ""
    result: dict = field(default_factory=dict)
    log: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "id": self.id, "total": self.total, "done": self.done,
            "stage": self.stage, "message": self.message,
            "finished": self.finished, "error": self.error,
            "result": self.result, "log": self.log[-60:],
        }


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def start_job(target, *args) -> Job:
    job = Job(id=uuid.uuid4().hex[:12])
    with JOBS_LOCK:
        JOBS[job.id] = job

    def runner():
        try:
            target(job, *args)
        except Exception as exc:
            job.error = str(exc)
            job.log.append(f"Stopped: {exc}")
        finally:
            job.finished = True
            job.stage = "done" if not job.error else "failed"

    threading.Thread(target=runner, daemon=True).start()
    return job


# --------------------------------------------------------------------------
# applying to Steam
# --------------------------------------------------------------------------

def apply_games(job: Job, user: steam.SteamUser, games: list[dict], settings: dict) -> None:
    """Write shortcuts and artwork for the selected games.

    Artwork is fetched first so an icon path can be baked into the
    shortcut record, then shortcuts.vdf is written once at the end. If
    Steam is running the whole thing stops before anything is touched.
    """
    if steam.steam_is_running():
        raise RuntimeError("Steam is running. Close it completely, then add the games.")

    job.total = len(games)
    job.stage = "artwork"

    client = sgdb.SteamGridDB(settings.get("api_key", ""))
    art_kinds = settings.get("art_kinds") or []
    prefer_direct = bool(settings.get("prefer_direct"))
    overwrite = bool(settings.get("overwrite_art"))
    grid_dir = user.grid_dir

    prepared: list[tuple[dict, dict]] = []   # (game, shortcut record)
    art_count = 0

    for index, game in enumerate(games, 1):
        title = game.get("title", "Untitled")
        job.message = title
        job.done = index

        exe, args, start_dir = resolve_launch(game, prefer_direct)
        if not exe:
            job.log.append(f"Skipped {title}: no executable found.")
            continue

        tags = list(game.get("tags") or [])
        if settings.get("add_tag") and MANAGED_TAG not in tags:
            tags.append(MANAGED_TAG)

        record = steam.build_shortcut(
            app_name=title, exe=exe, start_dir=start_dir,
            launch_options=args, tags=tags,
        )
        short_id = steam.entry_short_id(record)

        # artwork
        if art_kinds:
            have = sgdb.existing_art(grid_dir, short_id)
            wanted = [k for k in art_kinds if overwrite or k not in have]
            chosen = game.get("art") or {}
            if wanted:
                try:
                    game_id = game.get("sgdb_id")
                    if not game_id and not chosen:
                        found = client.match(game.get("sgdb_hint") or title)
                        game_id = found["id"] if found else None
                        if found:
                            job.log.append(f"{title}: matched \"{found['name']}\".")
                        else:
                            job.log.append(f"{title}: no artwork match found.")
                    picks = {}
                    if chosen:
                        picks = {k: {"url": v} for k, v in chosen.items() if k in wanted and v}
                    missing = [k for k in wanted if k not in picks]
                    if game_id and missing:
                        picks.update(
                            client.best_artwork(
                                int(game_id), missing,
                                nsfw=bool(settings.get("allow_nsfw")),
                                animated=bool(settings.get("allow_animated")),
                            )
                        )
                    for kind, item in picks.items():
                        url = item.get("url") if isinstance(item, dict) else item
                        if not url:
                            continue
                        saved = sgdb.download_art(url, grid_dir, short_id, kind)
                        art_count += 1
                        if kind == "icon":
                            record["icon"] = str(saved)
                except sgdb.SGDBError as exc:
                    job.log.append(f"{title}: {exc}")
                except OSError as exc:
                    job.log.append(f"{title}: could not save artwork ({exc}).")

            if not record.get("icon"):
                icon_file = sgdb.existing_art(grid_dir, short_id).get("icon")
                if icon_file:
                    record["icon"] = icon_file

        prepared.append((game, record))

    # merge into the existing file
    job.stage = "writing"
    job.message = "Updating shortcuts.vdf"
    existing = steam.read_shortcuts(user)
    by_id = {steam.entry_short_id(e): i for i, e in enumerate(existing)}

    added = updated = 0
    manifest = load_manifest(user.id3)
    for game, record in prepared:
        short_id = steam.entry_short_id(record)
        if short_id in by_id:
            index = by_id[short_id]
            record["LastPlayTime"] = existing[index].get("LastPlayTime", 0)
            existing[index] = record
            updated += 1
        else:
            existing.append(record)
            by_id[short_id] = len(existing) - 1
            added += 1
        manifest[str(short_id)] = {
            "title": record["AppName"],
            "source": game.get("source", ""),
            "exe": record["Exe"],
            "added": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    backup = steam.write_shortcuts(user, existing)
    save_manifest(user.id3, manifest)

    job.result = {
        "added": added,
        "updated": updated,
        "artwork": art_count,
        "backup": str(backup) if backup else "",
        "total_shortcuts": len(existing),
    }
    job.message = f"{added} added, {updated} updated, {art_count} images saved"


def remove_games(job: Job, user: steam.SteamUser, short_ids: list[int], delete_art: bool) -> None:
    """Take shortcuts back out and optionally clean up their artwork."""
    if steam.steam_is_running():
        raise RuntimeError("Steam is running. Close it completely, then remove the games.")

    targets = {int(s) for s in short_ids}
    job.total = len(targets)
    job.stage = "writing"

    existing = steam.read_shortcuts(user)
    kept = [e for e in existing if steam.entry_short_id(e) not in targets]
    removed = len(existing) - len(kept)
    steam.write_shortcuts(user, kept)

    if delete_art:
        job.stage = "artwork"
        for short_id in targets:
            for path in user.grid_dir.glob(f"{short_id}*"):
                if path.is_file():
                    try:
                        path.unlink()
                    except OSError:
                        pass

    manifest = load_manifest(user.id3)
    for short_id in targets:
        manifest.pop(str(short_id), None)
    save_manifest(user.id3, manifest)

    job.done = job.total
    job.result = {"removed": removed, "total_shortcuts": len(kept)}
    job.message = f"{removed} removed"


def managed_list(user: steam.SteamUser) -> list[dict]:
    """Shortcuts this tool added that are still present in Steam."""
    manifest = load_manifest(user.id3)
    present = {steam.entry_short_id(e): e for e in steam.read_shortcuts(user)}
    out = []
    for key, info in manifest.items():
        short_id = int(key)
        if short_id not in present:
            continue
        art = sgdb.existing_art(user.grid_dir, short_id)
        out.append(
            {
                "short_id": short_id,
                "title": present[short_id].get("AppName", info.get("title", "")),
                "source": info.get("source", ""),
                "added": info.get("added", ""),
                "exe": present[short_id].get("Exe", ""),
                "art": {k: f"/art/{user.id3}/{Path(v).name}" for k, v in art.items()},
            }
        )
    out.sort(key=lambda r: r["title"].lower())
    return out
