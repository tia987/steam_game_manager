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

A catalogue can also be a website that publishes one blog post per
release (see ``sitecatalog``). Its address goes in the same box; each
post that offers downloads becomes a game, and installing one reads the
post again for its current mirrors and magnet links. A release split
over many files is fetched file by file, from whichever mirror answers,
and unpacked before it is installed.
"""

from __future__ import annotations

import contextlib
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
from pathlib import Path, PurePath

from . import (archive, autoinstall, catalog, download, drives, filecheck, hosters, installers,
               library,
               pipeline, placement, proc, scanners, sgdb, sitecatalog, sitesync, steam, tools,
               torrentclient, torrentengine, torrents, uninstall)

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

# Set when the store is its own app in Sunshine, streamed to Moonlight. It
# is not something Steam launched, so it can restart Steam without closing,
# and closing it - Leave - is what ends the stream.
STREAMING = False


def started_by_sunshine() -> bool:
    """Sunshine started this, as an app of its own rather than through Steam."""
    sunshine = any(os.environ.get(k) for k in ("SUNSHINE_APP_ID", "SUNSHINE_APP_NAME"))
    through_steam = any(os.environ.get(k) for k in ("SteamGameId", "SteamClientLaunch",
                                                    "SteamAppId"))
    return sunshine and not through_steam


def sunshine_command() -> str:
    """The command to give Sunshine for an app that opens just the store."""
    command, _cwd = self_command("--store", "--streaming")
    return subprocess.list2cmdline(command) if os.name == "nt" else " ".join(
        f'"{part}"' if " " in part else part for part in command)


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
    state.setdefault("pending_remove", [])    # {"short_id", "title"} to take out of Steam
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


def _clean_link(value) -> str:
    """A download link: a web address, or a magnet link for BitTorrent."""
    text = str(value or "").strip()
    if len(text) < 4000 and re.match(r"(?i)^magnet:\?\S*xt=urn:btih:[a-z0-9]{32,40}", text):
        return text
    return _clean_url(text)


def is_torrent(url: str) -> bool:
    lowered = url.lower()
    return lowered.startswith("magnet:") or urllib.parse.urlparse(lowered).path.endswith(".torrent")


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
            url = _clean_link(value)
            if url:
                downloads[str(key).strip().lower()[:12]] = url
    single = _clean_link(raw.get("download") or raw.get("url") or raw.get("torrent")
                         or raw.get("magnet"))
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
        # When it was added or released, YYYY-MM-DD, for "newest first".
        "date": (_clean_text(raw.get("date") or raw.get("added"), 10)
                 if re.match(r"^\d{4}-\d{2}-\d{2}", str(raw.get("date") or raw.get("added") or ""))
                 else ""),
    }
    if not repo and not downloads and not game["site"]:
        return None
    return game


def parse_catalogue(blob, catalogue_id: str) -> dict:
    """Read a catalogue file into clean game records."""
    if isinstance(blob, bytes):
        try:
            blob = blob.decode("utf-8-sig")     # Notepad may put a byte-order mark first
        except UnicodeDecodeError as exc:
            raise StoreError("That is not a text file, so it cannot be a catalogue.") from exc
    text = blob.lstrip("\ufeff").strip()
    if not text:
        raise StoreError("That file is empty.")
    if text.startswith("<"):
        # The usual mistake: the address of a page that shows the file,
        # not of the file itself.
        raise StoreError("That address gives a web page, not the catalogue file itself. On "
                         "GitHub, GitLab, Gist or Pastebin, open the file and copy the address "
                         "of its Raw button. A Dropbox or Google Drive share link works as it is.")
    try:
        data = json.loads(text)
    except ValueError as exc:
        where = f" (line {exc.lineno}, column {exc.colno})" if hasattr(exc, "lineno") else ""
        raise StoreError(f"That file is not valid JSON{where}: {getattr(exc, 'msg', exc)}. A "
                         "missing or extra comma is the usual cause; jsonlint.com shows where.") from exc
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


def direct_link(url: str) -> str:
    """The file itself, from the address of a page that only shows it.

    People copy whatever is in the address bar, and on most sites that is
    a page around the file rather than the file.
    """
    rules = (
        # GitHub: a file's page, and a gist's page
        (r"^https://github\.com/([^/]+)/([^/]+)/blob/(.+)$",
         r"https://raw.githubusercontent.com/\1/\2/\3"),
        (r"^https://gist\.github\.com/([^/]+)/([0-9a-f]+)/?$",
         r"https://gist.githubusercontent.com/\1/\2/raw"),
        # GitLab and Codeberg
        (r"^(https://gitlab\.com/.+?)/-/blob/(.+)$", r"\1/-/raw/\2"),
        (r"^(https://codeberg\.org/[^/]+/[^/]+)/src/(.+)$", r"\1/raw/\2"),
        # Pastebin
        (r"^https://pastebin\.com/(?!raw/)([A-Za-z0-9]+)/?$", r"https://pastebin.com/raw/\1"),
        # Google Drive
        (r"^https://drive\.google\.com/file/d/([^/?#]+).*$",
         r"https://drive.google.com/uc?export=download&id=\1"),
        (r"^https://drive\.google\.com/open\?id=([^&#]+).*$",
         r"https://drive.google.com/uc?export=download&id=\1"),
    )
    for pattern, replacement in rules:
        if re.match(pattern, url, re.I):
            return re.sub(pattern, replacement, url, flags=re.I)
    # Dropbox: the same address, asking for the file rather than a preview
    if re.match(r"^https://(www\.)?dropbox\.com/", url, re.I):
        url = re.sub(r"([?&])(dl|raw)=0", r"\1dl=1", url)
        return url if re.search(r"[?&](dl|raw)=1", url) else url + ("&" if "?" in url else "?") + "dl=1"
    return url


def clean_source(source: str) -> str:
    """A catalogue address or path as typed or pasted: quotes and file:// gone."""
    source = source.strip().strip('"').strip("'").strip()
    if source.lower().startswith("file:"):
        source = urllib.request.url2pathname(urllib.parse.urlparse(source).path)
    return source


def fetch_source(source: str) -> bytes:
    """The bytes of a catalogue, from a web address or a file on disk."""
    source = clean_source(source)
    if re.match(r"^https?://", source, re.I):
        request = urllib.request.Request(direct_link(source), headers={"User-Agent": USER_AGENT})
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


def _clean_size(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _clean_release(found) -> dict:
    """A post's mirrors and torrents, reduced to what installing needs."""
    found = found if isinstance(found, dict) else {}
    mirrors = []
    for mirror in found.get("mirrors") or []:
        files = [{"url": _clean_url(f.get("url")), "name": _clean_text(f.get("name"), 200),
                  "optional": bool(f.get("optional"))}
                 for f in mirror.get("files") or [] if isinstance(f, dict)]
        files = [f for f in files if f["url"] and f["name"]][:400]
        if files:
            mirrors.append({"host": _clean_text(mirror.get("host"), 80),
                            "label": _clean_text(mirror.get("label"), 60), "files": files})
    swarm = []
    for item in found.get("torrents") or []:
        link = _clean_link(item.get("url")) if isinstance(item, dict) else ""
        if link:
            swarm.append({"url": link, "label": _clean_text(item.get("label"), 60),
                          "name": _clean_text(item.get("name"), 200)})
    return {"mirrors": mirrors[:12], "torrents": swarm[:6]}


def site_game(post: dict, catalogue_id: str) -> dict | None:
    """One release post as a store game."""
    name = _clean_text(post.get("name"), 120)
    page = _clean_url(post.get("url"))
    if not name or not page:
        return None
    return {
        "id": game_id(catalogue_id, name),
        "catalogue": catalogue_id,
        "name": name,
        "repo": "", "asset": "", "downloads": {},
        "tags": [_clean_text(t, 24) for t in post.get("tags") or [] if _clean_text(t, 24)][:8],
        "blurb": _clean_text(post.get("blurb"), 600),
        "licence": "",
        "site": page,
        "page": page,
        "version": _clean_text(post.get("version"), 40),
        "extras": _clean_text(post.get("extras"), 120),
        "note": "",
        "needs_data": "",
        "company": _clean_text(post.get("company"), 80),
        "date": _clean_text(post.get("date"), 10),
        "art_name": _clean_text(post.get("art_name"), 120) or name,
        "cover": "", "hero": "", "logo": "", "wide": "",
        "site_cover": _clean_url(post.get("cover")),
        "size": _clean_size(post.get("size")),
        "download_size": _clean_size(post.get("download_size")),
        # The post is read again when installing, for links that are
        # current; keeping thousands of mirror lists here would only make
        # the shelf slow to load.
        "torrent": bool((post.get("downloads") or {}).get("torrents")),
    }


SITE_PAGES_DEFAULT = 10


def _site_pages(settings: dict | None = None) -> int:
    """How deep to read a release website; 0 means every page it has."""
    settings = settings or library.load_settings()
    value = settings.get("store_site_pages", SITE_PAGES_DEFAULT)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return SITE_PAGES_DEFAULT
    return sitesync.ALL_PAGES if value <= 0 else min(value, 100000)


def _site_catalogue(source: str, catalogue_id: str, text: str = "") -> dict:
    """A release website's front page, read now; the rest follows in the background."""
    settings = library.load_settings()
    try:
        final = source
        if not text:
            text, final = sitecatalog.fetch(source)
        listing = sitecatalog.read_listing(text, final,
                                           settings.get("store_language") or "english")
    except sitecatalog.SiteError as exc:
        raise StoreError(str(exc)) from exc
    games, seen, skipped = [], set(), 0
    for post in listing["posts"]:
        game = site_game(post, catalogue_id)
        if not game or game["id"] in seen:
            skipped += 1                 # the newest post of a game comes first and wins
            continue
        seen.add(game["id"])
        games.append(game)
    if not games:
        raise StoreError("That is a web page, but none of its posts offer downloads that could "
                         "be read. For a JSON catalogue, use the address of the raw file.")
    return {"name": listing["site"], "games": games, "skipped": skipped, "kind": "site",
            "pages": max(listing["last_page"], 1)}


def read_source(source: str, catalogue_id: str) -> dict:
    """A catalogue from a JSON file or a release website, whichever it is."""
    source = clean_source(source)
    web = bool(re.match(r"^https?://", source, re.I))
    try:
        blob = fetch_source(source)
    except StoreError:
        if not web:
            raise
        # Some sites turn away anything that does not look like a browser.
        return _site_catalogue(source, catalogue_id)
    if web and sitecatalog.looks_like_site(blob) and direct_link(source) == source:
        text = blob.decode("utf-8", "replace")
        return _site_catalogue(source, catalogue_id, text=text)
    parsed = parse_catalogue(blob, catalogue_id)
    parsed["kind"] = "json"
    return parsed


def test_source(source: str) -> dict:
    """Fetch and read a catalogue without saving it, so it can be checked first."""
    if not source.strip():
        raise StoreError("Type a web address or a file path first.")
    parsed = read_source(source, "test")
    if not parsed["games"]:
        raise StoreError("It was read, but it has no usable games in it.")
    return {
        "name": parsed["name"],
        "count": len(parsed["games"]),
        "skipped": parsed["skipped"],
        "kind": parsed.get("kind", "json"),
        "pages": parsed.get("pages", 0),
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
        source = clean_source(str(item.get("source", "")))[:2000]
        if not source or source.lower() in seen:
            continue
        seen.add(source.lower())
        cid = str(item.get("id") or "").strip()
        if not re.fullmatch(r"[a-f0-9]{8,16}", cid):
            cid = uuid.uuid4().hex[:10]
        out.append({"id": cid, "source": source, "name": _clean_text(item.get("name"), 60),
                    "enabled": item.get("enabled", True) is not False})
    return out


_CATALOGUE_MEMO: dict = {"stamp": None, "data": {}}


def _catalogue_cache() -> dict:
    """The saved catalogues, read from disk only when the file has changed.

    A release website read twenty pages deep makes a file of several
    megabytes, and every cover and every Install asks for it.
    """
    path = _catalogue_cache_path()
    try:
        info = path.stat()
        stamp = (info.st_mtime_ns, info.st_size)
    except OSError:
        return {}
    if _CATALOGUE_MEMO["stamp"] != stamp:
        _CATALOGUE_MEMO["data"] = _read_json(path, {})
        _CATALOGUE_MEMO["stamp"] = stamp
    return _CATALOGUE_MEMO["data"]


def load_catalogues(force: bool = False) -> list[dict]:
    """Every catalogue with its games; user ones are re-read when stale."""
    settings = library.load_settings()
    builtin_on = settings.get("store_builtin_enabled", True) is not False
    out = [{"id": BUILTIN_ID, "name": BUILTIN_NAME, "builtin": True, "enabled": builtin_on,
            "source": "", "error": "", "games": builtin_games() if builtin_on else []}]
    with _LOCK:
        cache = _catalogue_cache()
        changed = False
        language = settings.get("store_language") or "english"
        depth = _site_pages(settings)
        for entry in clean_catalogue_list(settings.get("store_catalogues")):
            cid, source = entry["id"], entry["source"]
            cached = cache.get(cid) if isinstance(cache.get(cid), dict) else None
            if (cached or {}).get("kind") == "site" and (cached or {}).get("source") == source \
                    and sitesync.has_state(cid):
                out.append(_site_entry(entry, depth, language, force))
                continue
            stale = (not cached or cached.get("source") != source
                     or cached.get("kind") == "site"          # its state file went missing
                     or time.time() - cached.get("fetched", 0) > CATALOGUE_HOURS * 3600)
            if entry["enabled"] and (force or stale):
                try:
                    parsed = read_source(source, cid)
                    if parsed.get("kind") == "site":
                        # From now on the site keeps itself up to date.
                        sitesync.seed(cid, source, parsed["name"], parsed["games"])
                        cache[cid] = {"fetched": time.time(), "source": source, "kind": "site",
                                      "name": parsed["name"], "games": [], "error": ""}
                        changed = True
                        out.append(_site_entry(entry, depth, language, True))
                        continue
                    cached = {"fetched": time.time(), "source": source, "name": parsed["name"],
                              "games": parsed["games"], "error": "", "kind": "json"}
                except StoreError as exc:
                    # Keep whatever was read last time: a site being down for
                    # an hour should not empty the shelf.
                    old = cached if cached and cached.get("source") == source else {}
                    cached = {"fetched": time.time(), "source": source,
                              "name": old.get("name", ""), "games": old.get("games", []),
                              "error": str(exc), "kind": "json"}
                cache[cid] = cached
                changed = True
            cached = cached or {}
            out.append({
                "id": cid,
                "name": entry["name"] or cached.get("name") or source,
                "builtin": False,
                "enabled": entry["enabled"],
                "source": source,
                "kind": "json",
                "error": cached.get("error", "") if entry["enabled"] else "",
                "games": cached.get("games", []) if entry["enabled"] else [],
            })
        if changed:
            try:
                _write_json(_catalogue_cache_path(), cache)
            except OSError:
                pass
    return out


def _site_entry(entry: dict, depth: int, language: str, force: bool) -> dict:
    """A release website's catalogue: what is on disk now, kept fresh behind the scenes."""
    cid, source = entry["id"], entry["source"]
    if entry["enabled"]:
        sitesync.ensure(cid, source, depth, lambda post, cid=cid: site_game(post, cid),
                        force=force, language=language)
    state = sitesync.load(cid, source)
    live = sitesync.progress(cid)
    return {
        "id": cid,
        "name": entry["name"] or state.get("name") or source,
        "builtin": False,
        "enabled": entry["enabled"],
        "source": source,
        "kind": "site",
        "error": state.get("error", "") if entry["enabled"] else "",
        "games": state.get("games", []) if entry["enabled"] else [],
        "sync": {
            "running": bool(live),
            "phase": live.get("phase", ""),
            "page": live.get("page", 0),
            "pages_read": state.get("pages_read", 0),
            "total_pages": live.get("of") or state.get("total_pages", 0),
            "complete": bool(state.get("complete")),
            "depth": depth,
            "checked": state.get("checked", 0),
        },
    }


def refresh_everything() -> dict:
    """Read every catalogue again from scratch: the button for when in doubt.

    JSON catalogues are fetched again. Release websites are read again
    from the front page, in the background, refreshing every release in
    place, and dropping the ones a site no longer lists once all of it has
    been read. Artwork that was not found before is looked for again.
    """
    settings = library.load_settings()
    for entry in clean_catalogue_list(settings.get("store_catalogues")):
        if entry["enabled"] and sitesync.has_state(entry["id"]):
            sitesync.rescan(entry["id"], entry["source"])
    with _ART_LOCK:
        cache = _read_json(_art_cache_path(), {})
        cache = {k: v for k, v in cache.items() if not (isinstance(v, dict) and v.get("no_match"))}
        try:
            _write_json(_art_cache_path(), cache)
        except OSError:
            pass
    return overview(force=True)


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
    return bool(game.get("repo") or platform_download(game) or game.get("page"))


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
            found = client.match(game.get("art_name") or game["name"])
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
                # A release blog's own picture is a small thumbnail, so it
                # only stands in when SteamGridDB has nothing better.
                out[game["id"]] = (entry.get("thumb") or entry.get("capsule")
                                   or game.get("site_cover") or "")
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
    art = {"cover": game.get("cover", ""), "hero": game.get("hero", ""), "logo": game.get("logo", ""),
           "blurb": game.get("blurb", "")}
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
    art["cover"] = art["cover"] or game.get("site_cover", "")
    return art


# --------------------------------------------------------------------------
# installing
# --------------------------------------------------------------------------

ACTIVE_PHASES = ("resolving", "downloading", "installing", "waiting", "adding", "uninstalling")


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
    target: str = ""              # a library folder chosen for this one game, or "" for automatic
    extras: bool = False          # let the installer run DirectX, Visual C++ and other extras
    verify: bool = False          # check the installed files against the release's checksums
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


def _library_paths(settings: dict, enabled_only: bool = True) -> list[str]:
    return [str(f.get("path")) for f in settings.get("library_folders") or []
            if isinstance(f, dict) and f.get("path")
            and (not enabled_only or f.get("enabled", True) is not False)]


def library_overview(settings: dict | None = None) -> dict:
    """The library folders and the drives that could get one, for picking with a controller."""
    settings = settings or library.load_settings()
    folders = []
    for target in placement.describe_all(settings.get("library_folders") or []):
        folders.append({key: target.get(key) for key in (
            "path", "kind", "free", "total", "connected", "enabled", "priority", "reserve_gb")})
    taken = {drives.volume_key(f["path"]) for f in folders}
    choices = [c for c in library_choices() if drives.volume_key(c["path"]) not in taken]
    return {"folders": folders, "choices": choices,
            "ask": bool(settings.get("store_ask_drive"))}


def change_library(action: str, path: str) -> dict:
    """Put a folder first, switch it off or on, take it out of the list, or add a drive.

    Taking a folder out of the list never deletes anything in it; games
    already there stay where they are.
    """
    if action == "add":
        set_library(path)
        return library_overview()
    settings = library.load_settings()
    folders = [dict(f) for f in settings.get("library_folders") or [] if isinstance(f, dict)]
    index = next((n for n, f in enumerate(folders)
                  if str(f.get("path", "")).lower() == path.lower()), None)
    if index is None:
        raise StoreError("That folder is not in the list any more.")
    if action == "first":
        folders.insert(0, folders.pop(index))
        folders[0]["enabled"] = True
    elif action == "toggle":
        folders[index]["enabled"] = folders[index].get("enabled", True) is False
    elif action == "remove":
        folders.pop(index)
    else:
        raise StoreError("Unknown change.")
    library.save_settings({"library_folders": folders})
    autoinstall.QUEUE.retry_waiting("folder")
    return library_overview()


def install(gid: str, target: str = "", extras: bool | None = None,
            verify: bool | None = None) -> StoreJob:
    """Download and install one game, in the background.

    ``target`` is one of the library folders, chosen for this game alone;
    without it the usual order and free-space rules choose.
    """
    settings = library.load_settings()
    if target:
        if target not in _library_paths(settings):
            raise StoreError("Pick one of your library folders.")
    # Off unless asked for: most PCs already have DirectX and Visual C++, and
    # their setups are windows a controller cannot click.
    extras = bool(settings.get("store_extras")) if extras is None else bool(extras)
    verify = bool(settings.get("store_verify")) if verify is None else bool(verify)
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
        previous = JOBS.get(gid)
        job = StoreJob(id=gid, name=game["name"], started=time.time(),
                       target=target or (previous.target if previous else ""),
                       extras=extras, verify=verify)
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
    if job:
        return install(gid, extras=job.extras, verify=job.verify)
    return install(gid)


def _pick_from_release(game: dict, release: dict) -> str:
    """The game's own file in one GitHub release, or ""."""
    assets = release.get("assets") or []
    if game.get("asset"):
        pattern = re.compile(game["asset"], re.I)
        assets = [a for a in assets if pattern.search(a.get("name", ""))]
        best = catalog.choose_asset(assets)
        if not best:
            # The catalogue's pattern is trusted over the platform guess,
            # but never over "this is a patch, not the game".
            allowed = [a for a in assets if not catalog.NOT_THE_GAME.search(a.get("name", ""))]
            allowed.sort(key=lambda a: -(a.get("size") or 0))
            best = {"url": allowed[0].get("browser_download_url", "")} if allowed else None
    else:
        best = catalog.choose_asset(assets)
    return (best or {}).get("url", "")


def _resolve_download(game: dict, settings: dict) -> tuple[str, str]:
    """(url, version) of the full game's file for this computer."""
    direct = platform_download(game)
    if direct and not game.get("repo"):
        return direct, ""
    token = str(settings.get("github_token", "")).strip()
    if not game.get("asset"):
        found = catalog.resolve(game, library.config_dir(), token=token)
        if found.get("asset") and found["asset"].get("url"):
            return found["asset"]["url"], found.get("version", "")
    # Either the catalogue narrows the choice, or the newest release has
    # nothing that is the game - a point release that ships only a patch
    # is common. Walk back through recent releases to the newest full one.
    releases, _remaining = catalog._request(
        f"{catalog.API}/repos/{game['repo']}/releases?per_page=10", token)
    for release in releases if isinstance(releases, list) else []:
        if release.get("draft") or release.get("prerelease"):
            continue
        url = _pick_from_release(game, release)
        if url:
            return url, release.get("tag_name") or ""
    if direct:
        return direct, ""
    raise StoreError("None of the recent releases has a full game for this computer, only "
                     "patches, add-ons or builds for other systems.")


def _not_the_game(name: str) -> str:
    """Why a downloaded file is not something to install as a game, or ""."""
    found = catalog.NOT_THE_GAME.search(name)
    if not found:
        return ""
    word = found.group(1).lower()
    return (f"The download is named “{name}”: it looks like a {word}, not the game "
            "itself, so it was not installed. The catalogue entry needs a link to the full game.")


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
        _release_reservation(job)
        if job.phase in ("cancelled", "failed", "done"):
            job.finished = job.finished or time.time()
            job.progress = -1.0 if job.phase != "done" else 1.0


def _install_steps(job: StoreJob, game: dict) -> None:
    settings = library.load_settings()
    folder = download_folder(settings)
    folder.mkdir(parents=True, exist_ok=True)
    if game.get("page"):
        result, shared = _fetch_release(job, game, settings, folder)
        _install_and_record(job, game, settings, folder, result, shared, game.get("version", ""))
        return

    url, version = _resolve_download(game, settings)
    if job.cancel:
        raise Stopped()
    job.phase, job.progress, job.message = "downloading", 0.0, "Starting the download"

    def progress(done: int, total: int) -> None:
        if job.cancel:
            raise Stopped()
        job.progress = done / total if total else -1.0
        mb = done / 1048576
        job.message = (f"{mb:,.0f} MB of {total / 1048576:,.0f} MB" if total
                       else f"{mb:,.0f} MB")

    shared = None                  # the TorrentResult, for a BitTorrent download
    if is_torrent(url):
        shared = _torrent_download(job, url, folder, settings)
        result = {"path": str(shared.path), "name": shared.path.name}
    else:
        # A direct link names its file before anything is fetched, and a
        # server may still rename it on the way; both are checked.
        problem = _not_the_game(Path(urllib.parse.urlparse(url).path).name)
        if problem:
            raise StoreError(problem)
        result = download.download(url, folder, progress=progress)
        if job.cancel:
            raise Stopped()
        problem = _not_the_game(result["name"])
        if problem:
            _tidy(Path(result["path"]), folder, {"store_keep_downloads": False})
            raise StoreError(problem)
    _install_and_record(job, game, settings, folder, result, shared, version)


def _install_and_record(job: StoreJob, game: dict, settings: dict, folder: Path,
                        result: dict, shared, version: str) -> None:
    """Queue what was downloaded, wait for it, add it to Steam, tidy up."""
    job.phase, job.progress, job.message = "installing", -1.0, "Waiting its turn"
    task = autoinstall.QUEUE.add(result["path"], name=game["name"], mode="quiet",
                                 origin="store", size=int(game.get("size") or 0),
                                 target_root=job.target,
                                 folder_name=placement.plain_folder_name(game["name"]),
                                 # The installer's own file checker always waits for
                                 # clicks; the store checks the files itself instead.
                                 skip_redists=not job.extras, skip_checkers=True)
    job.task = task.id
    task = _follow(job, task.id)
    checked = _check_files(job, task.target_dir) if job.verify else None

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
    if checked is not None:
        record["check"] = {k: checked[k] if isinstance(checked[k], int) else checked[k][:50]
                           for k in ("checked", "missing", "bad")}
    _change_state(lambda state: state["installed"].__setitem__(game["id"], record))
    if shared is not None and not shared.ours:
        pass                       # the person's own torrent: neither it nor its files are touched
    elif shared is not None and settings.get("store_keep_seeding") \
            and not getattr(shared.client, "leech_only", False):
        pass                       # left in the torrent client, still being shared
    else:
        if shared is not None:
            _forget_torrent(shared.client, shared.info_hash)
        _tidy(Path(result.get("tidy") or result["path"]), folder, settings)

    job.phase, job.finished = "done", time.time()
    job.message = {
        "added": "Installed and added to Steam.",
        "pending": "Installed. It appears in Steam after Steam restarts.",
        "none": "Installed, but no program to start was found in it.",
        "failed": f"Installed, but not added to Steam: {record.get('steam_error', '')}",
    }.get(record["steam"], "Installed.")
    if checked is not None:
        wrong = len(checked["missing"]) + len(checked["bad"])
        if not checked["checked"]:
            job.message += " It came with no list of files to check against."
        elif wrong:
            job.message += (f" {wrong} of {checked['checked']} files did not check out "
                            f"(first: {(checked['missing'] + checked['bad'])[0]}); reinstalling "
                            "it may help.")
        else:
            job.message += f" All {checked['checked']} files checked out."
    if record["steam"] == "pending" and settings.get("store_restart") == "now":
        threading.Thread(target=_restart_when_idle, daemon=True).start()


def _check_files(job: StoreJob, folder: str) -> dict | None:
    """The installed files against the release's checksum list, in the background job."""
    if not folder or not Path(folder).is_dir():
        return None
    job.phase, job.progress, job.message = "installing", 0.0, "Checking the installed files"

    def progress(number: int, total: int, name: str) -> None:
        job.progress = number / total if total else -1.0
        job.message = f"Checking files · {number:,} of {total:,}"

    try:
        return filecheck.check(folder, progress=progress, stop=lambda: job.cancel)
    except filecheck.Stopped:
        return None
    except OSError as exc:
        return {"checked": 0, "missing": [], "bad": [], "error": str(exc)}


# --------------------------------------------------------------------------
# releases published as blog posts
# --------------------------------------------------------------------------

UNPACKED = "unpacked"
UNPACKED_MARK = ".nsgm-unpacked"
PARTS_FILE = ".nsgm-parts.json"


def _folder_name(name: str) -> str:
    """The download folder for a game: plain ASCII and short, for old unpackers."""
    return placement.plain_folder_name(name)


def _game_home(folder: Path, name: str) -> Path:
    """A game's download folder, moving one made under the old naming into place.

    Older versions named it after the title as written, "–" and all; a
    download already there is carried over rather than fetched again.
    """
    home = folder / _folder_name(name)
    legacy = folder / (re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")[:80] or "Game")
    if legacy != home and legacy.is_dir() and not home.exists():
        try:
            legacy.rename(home)
        except OSError:
            return legacy                   # still in use: keep using it as it is
    return home


def _fetch_release(job: StoreJob, game: dict, settings: dict, folder: Path):
    """Download one post's release, by torrent or from its mirrors.

    Returns ``(result, shared)`` the way the ordinary path does: ``result``
    names what to install and what to delete afterwards, ``shared`` is the
    torrent to forget once installed, if it came that way.
    """
    job.phase, job.progress, job.message = "resolving", -1.0, "Reading the release page"
    release = game.get("release") or {}
    try:
        post = sitecatalog.read_one(game["page"],
                                    language=settings.get("store_language") or "english")
        if sitecatalog.has_downloads(post):
            release = _clean_release(post["downloads"])
    except sitecatalog.SiteError as exc:
        # Links read when the catalogue was refreshed are hours old at
        # most, which beats giving up while the site has a bad minute.
        if not (release.get("mirrors") or release.get("torrents")):
            raise StoreError(f"The release page could not be read: {exc}") from exc
    if job.cancel:
        raise Stopped()
    mirrors, swarm = release.get("mirrors") or [], release.get("torrents") or []
    if not mirrors and not swarm:
        raise StoreError("The release page has no download links on it any more.")

    method = settings.get("store_site_method", "auto")
    if method == "torrent":
        order = ["torrent"]
    elif method == "direct":
        order = ["direct"]
    else:
        if builtin_engine(settings):
            have_client = bool(swarm)
        else:
            try:
                have_client = bool(swarm) and torrent_client(settings) is not None
            except StoreError:
                have_client = False
        order = ["torrent", "direct"] if have_client else ["direct", "torrent"]
    order = [way for way in order if (mirrors if way == "direct" else swarm)]
    if not order:
        raise StoreError("This release is only offered over BitTorrent, and the store is set to "
                         "direct downloads only." if swarm else
                         "This release has no torrent, and the store is set to BitTorrent only.")

    home = _game_home(folder, game["name"])
    download_size = int(game.get("download_size") or 0)
    problems = []
    for number, way in enumerate(order):
        fallback = number < len(order) - 1
        try:
            if way == "torrent":
                _reserve(job, home, max(0, download_size - installers.folder_size(home / "torrent")))
                return _release_by_torrent(job, game, swarm, home, settings, fallback)
            with _direct_slot(job, settings):
                # Room for the download, and for unpacking it beside itself.
                have = installers.folder_size(home) if home.is_dir() else 0
                _reserve(job, home, max(0, download_size - have) + int(game.get("size") or 0))
                target = _download_release(job, game, mirrors, folder, settings)
            return {"path": str(_prepare_release(job, target, settings)),
                    "tidy": str(target)}, None
        except StoreError as exc:
            problems.append(str(exc))
    raise StoreError(" Then: ".join(problems))


def _release_by_torrent(job: StoreJob, game: dict, swarm: list, home: Path, settings: dict,
                        fallback: bool):
    """The release over BitTorrent, into the game's own folder.

    Optional files are left unchosen in the torrent client, exactly as the
    direct route leaves them undownloaded. When the mirrors are there to
    fall back on, a torrent nobody is sharing is given up on after a while
    instead of being waited on for ever.
    """
    job.phase, job.progress = "downloading", -1.0
    language = settings.get("store_language") or "english"
    wanted_extras = bool(settings.get("store_optional_files"))

    def skip(name: str) -> bool:
        return not wanted_extras and sitecatalog.is_optional(name, language)

    patience = Patience() if fallback else None
    problems = []
    for item in swarm[:3]:
        try:
            got = _torrent_download(job, item["url"], home / "torrent", settings,
                                    check_name=False, skip=skip, patience=patience)
        except StoreError as exc:
            problems.append(str(exc))
            continue
        path = got.path
        if getattr(got.client, "leech_only", False):
            # Finished and never shared: let the built-in downloader close
            # the files now, before the setup program tries to open them.
            _forget_torrent(got.client, got.info_hash)
        if path.is_dir() and installers.find_installer(path) is None and _archive_sets(
                [p for p in path.iterdir() if p.is_file()]):
            # Packed inside the torrent too. Unpacked beside it; the
            # archives stay while the torrent might still be shared.
            keep = dict(settings, store_keep_downloads=True)
            path = _prepare_release(job, path, keep, staging=home / UNPACKED)
        return {"path": str(path), "tidy": str(home)}, got
    raise StoreError(problems[0] if len(set(problems)) == 1 else " ".join(problems))


# --- many downloads at once ------------------------------------------------
#
# Pressing Install on thirty games is expected. Thirty threads pulling from
# the same three file hosts at once get throttled or banned, and thirty
# releases that each fit the disk on their own do not fit it together. So
# direct downloads take turns, a few at a time, and each job sets aside the
# space it will need before starting, counting what the others have set
# aside and not yet written. Torrents need no turns of their own: the
# torrent app has its own queue, which the store waits in and shows.

_SLOTS = threading.Condition()
_DOWNLOADING: set = set()
_RESERVED: dict = {}             # job id -> (disk key, folder, bytes)
SPACE_MARGIN = 2 * 1024 ** 3     # never fill a disk to the last byte


def parallel_downloads(settings: dict | None = None) -> int:
    settings = settings or library.load_settings()
    try:
        return max(1, min(6, int(settings.get("store_parallel_downloads") or 2)))
    except (TypeError, ValueError):
        return 2


@contextlib.contextmanager
def _direct_slot(job: StoreJob, settings: dict):
    with _SLOTS:
        while len(_DOWNLOADING) >= parallel_downloads(settings):
            if job.cancel:
                raise Stopped()
            job.phase, job.progress = "downloading", -1.0
            ahead = len(_DOWNLOADING)
            job.message = (f"Waiting its turn: {ahead} download{'s' if ahead != 1 else ''} "
                           "running")
            _SLOTS.wait(1)
        _DOWNLOADING.add(job.id)
    try:
        yield
    finally:
        with _SLOTS:
            _DOWNLOADING.discard(job.id)
            _SLOTS.notify_all()


def _reserve(job: StoreJob, folder: Path, need: int) -> None:
    """Set aside disk space for a download, or say why it will not fit."""
    with _SLOTS:
        _RESERVED.pop(job.id, None)
    if need <= 0:
        return
    room = drives.space(folder)
    if not room.get("ok"):
        return
    key = drives.volume_key(folder)
    with _SLOTS:
        others = list((jid, entry) for jid, entry in _RESERVED.items() if jid != job.id)
    # What another download has already written is already gone from the
    # free space, so only the rest of its share is still to come.
    pending = 0
    for _jid, (other_key, other_folder, amount) in others:
        if other_key == key:
            written = installers.folder_size(other_folder) if other_folder.is_dir() else 0
            pending += max(0, amount - written)
    if room["free"] - pending < need + SPACE_MARGIN:
        promised = f", with {placement.human(pending)} set aside for other downloads" if pending else ""
        raise StoreError(f"Not enough free space in {folder.parent}: this needs about "
                         f"{placement.human(need)} and {placement.human(room['free'])} is free"
                         f"{promised}. Free some space or choose another download folder.")
    with _SLOTS:
        _RESERVED[job.id] = (key, folder, need)


def _release_reservation(job: StoreJob) -> None:
    with _SLOTS:
        _RESERVED.pop(job.id, None)


def _release_files(mirrors: list, optional: bool) -> tuple[list, dict]:
    """Every file the release needs, and where each one can be had.

    The first mirror's list is the release; another mirror is only a
    second source for a file of the same name.
    """
    names, sources = [], {}
    for mirror in mirrors:
        for item in mirror.get("files") or []:
            if item.get("optional") and not optional:
                continue
            key = item["name"].lower()
            if key not in sources:
                sources[key] = []
                names.append(item["name"])
            sources[key].append((mirror, item["url"]))
    return names, sources


def _download_release(job: StoreJob, game: dict, mirrors: list, folder: Path,
                      settings: dict) -> Path:
    """Fetch every needed file into a folder of the game's own, resuming.

    Each file is tried on the mirror that last worked first, then on the
    others, so one host being down or slow costs one file's worth of
    retrying rather than the whole release.
    """
    target = _game_home(folder, game["name"])
    target.mkdir(parents=True, exist_ok=True)
    if (target / UNPACKED / UNPACKED_MARK).is_file():
        return target                        # unpacked on an earlier attempt
    names, sources = _release_files(mirrors, bool(settings.get("store_optional_files")))
    if not names:
        raise StoreError("Every file in this release is marked optional, so there is nothing "
                         "to download. Turn on optional files in Settings to fetch them.")

    manifest_path = target / PARTS_FILE
    finished = _read_json(manifest_path, {})
    expected = int(game.get("download_size") or 0)
    done_bytes = sum(int(finished.get(n.lower()) or 0) for n in names
                     if (target / download.clean_file_name(n)).is_file())
    good = None                       # the mirror that last delivered a file
    job.phase, job.progress = "downloading", 0.0

    for index, name in enumerate(names):
        key = name.lower()
        path = target / download.clean_file_name(name)
        if key in finished and path.is_file() and path.stat().st_size == finished[key]:
            continue
        count = f"file {index + 1} of {len(names)}" if len(names) > 1 else ""

        def progress(done: int, total: int, count=count, index=index) -> None:
            if job.cancel:
                raise Stopped()
            sofar = done_bytes + done
            if expected:
                job.progress = min(sofar / expected, 0.99)
            elif total:
                job.progress = (index + done / total) / len(names)
            amount = (f"{sofar / 1048576:,.0f} MB of {expected / 1048576:,.0f} MB" if expected
                      else f"{sofar / 1048576:,.0f} MB")
            job.message = " · ".join(part for part in (amount, count) if part)

        problems = []
        options = sorted(sources[key], key=lambda pair: pair[0] is not good)
        for mirror, url in options:
            if job.cancel:
                raise Stopped()
            label = mirror.get("label") or mirror.get("host") or "a mirror"
            job.message = " · ".join(part for part in (f"Asking {label}", count) if part)
            try:
                link = hosters.resolve(url, name)
                got = download.download(link["url"], target, progress=progress, name=name,
                                        headers=link["headers"])
            except (hosters.HosterError, download.DownloadError) as exc:
                problems.append(f"{label}: {exc}")
                continue
            finished[key] = got["size"]
            done_bytes += got["size"]
            good = mirror
            try:
                _write_json(manifest_path, finished)
            except OSError:
                pass
            break
        else:
            raise StoreError(f"“{name}” could not be downloaded from any mirror. "
                             + " ".join(problems[:4]))
    return target


_UNPACK_LOCK = threading.Lock()


def _unpack_all(job: StoreJob, firsts: list[Path], staging: Path, progress) -> None:
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)     # a half-finished earlier attempt
    try:
        for number, volume in enumerate(firsts):
            job.message = "Unpacking the download"
            pipeline.unpack(str(volume), into=str(staging), keep_archive=True, progress=progress,
                            flatten=(number == 0), stop=lambda: job.cancel)
    except pipeline.Stopped as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise Stopped() from exc
    except pipeline.PipelineError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise StoreError(f"The download would not unpack: {exc}") from exc


VOLUME_TAIL = re.compile(r"(?i)(\.part\d+\.rar|\.(7z|zip|rar)\.\d{3}|\.r\d{2}|\.rar)$")


def _archive_sets(files: list[Path]) -> list[Path]:
    """The first volume of each archive among the files, one per set."""
    sets: dict[str, list[Path]] = {}
    for item in files:
        if installers.is_archive(item) or VOLUME_TAIL.search(item.name):
            sets.setdefault(VOLUME_TAIL.sub("", item.name).lower(), []).append(item)
    out = []
    for members in sets.values():
        first = installers.first_volume(members)
        if first is not None:
            out.append(first)
    return out


def _prepare_release(job: StoreJob, target: Path, settings: dict,
                     staging: Path | None = None) -> Path:
    """What the install queue should be given for a downloaded release.

    A release packed as an archive usually holds a setup program, not the
    game. Handing the archive straight to the queue would unpack the setup
    program into the library and stop there, so it is unpacked here first
    and the queue gets the folder, which it then installs from properly.

    Files downloaded loose beside the archive - a language pack, optional
    extras - belong next to that setup program, which looks for them in
    its own folder, so they are moved there.
    """
    staging = staging or target / UNPACKED
    if (staging / UNPACKED_MARK).is_file():
        return staging
    files = [p for p in target.iterdir() if p.is_file() and not p.name.startswith(".")
             and not p.name.endswith(".part")]
    language = settings.get("store_language") or "english"
    firsts = _archive_sets(files)
    # The release itself first; optional extras packed as archives after.
    firsts.sort(key=lambda p: (sitecatalog.is_optional(p.name, language), p.name.lower()))
    if not firsts:
        return target                      # a setup program and its data, or the game itself

    native = (".zip", ".tar", ".gz", ".tgz", ".xz", ".bz2")
    if not pipeline._external_unpacker() and any(p.suffix.lower() not in native for p in firsts):
        if not tools.can_fetch():
            raise StoreError(f"{firsts[0].name} needs 7-Zip to unpack. Install p7zip (7-Zip) "
                             "with your package manager, then press Try again.")
        job.message = "Getting 7-Zip"
        try:
            tools.fetch_seven_zip()
        except tools.ToolError as exc:
            raise StoreError(f"7-Zip could not be fetched ({exc}).") from exc

    job.phase, job.progress = "installing", 0.0

    def progress(fraction: float, name: str = "") -> None:
        job.progress = max(0.0, min(float(fraction), 1.0))
        job.message = f"Unpacking {Path(name).name}" if name else "Unpacking the download"

    # Unpacking is all disk; several at once only makes each slower.
    while not _UNPACK_LOCK.acquire(timeout=1):
        if job.cancel:
            raise Stopped()
        job.message = "Waiting for another game to finish unpacking"
    try:
        _unpack_all(job, firsts, staging, progress)
    finally:
        _UNPACK_LOCK.release()

    packed = {p for p in files if installers.is_archive(p) or VOLUME_TAIL.search(p.name)}
    setup = installers.find_installer(staging)
    home = setup.parent if setup else staging
    keep = bool(settings.get("store_keep_downloads"))
    for loose in files:
        if loose in packed:
            continue
        try:
            (shutil.copy2 if keep else shutil.move)(str(loose), str(home / loose.name))
        except OSError as exc:
            raise StoreError(f"{loose.name} could not be put beside the setup program "
                             f"({exc.strerror or exc}).") from exc
    (staging / UNPACKED_MARK).write_text("", "utf-8")

    # The archives are spent; a big release would otherwise take its space
    # twice over until the install finished.
    if not keep:
        for item in packed:
            try:
                item.unlink()
            except OSError:
                pass
    return staging


# --------------------------------------------------------------------------
# BitTorrent
# --------------------------------------------------------------------------

def builtin_engine(settings: dict | None = None) -> bool:
    """Whether torrents go through the store's own downloader rather than an app."""
    settings = settings or library.load_settings()
    return (settings.get("store_torrent_engine", "builtin") != "app"
            and torrentengine.available())


def torrent_client(settings: dict | None = None, look: bool = True):
    """The torrent client to use: the built-in one, the app set up, or one found running.

    Finding an app saves it, so the person never has to fill in the
    settings for a client that is sitting there on its default port.
    """
    settings = settings or library.load_settings()
    if builtin_engine(settings):
        if not look:
            return None
        try:
            return torrentengine.ENGINE.get()
        except torrentengine.EngineError as exc:
            raise StoreError(str(exc)) from exc
    configured = settings.get("torrent_client") or {}
    client = torrentclient.from_settings(configured)
    if client is not None or not look:
        return client
    found = torrentclient.detect()
    if found and not found.get("needs_login"):
        library.save_settings({"torrent_client": {"kind": found["kind"], "url": found["url"],
                                                  "username": "", "password": ""}})
        return torrentclient.from_settings(found)
    if found:
        raise StoreError(f"{found['label']} is running but wants a password. Sign in to it "
                         "under Settings → BitTorrent, then press Try again.")
    raise StoreError("This game is shared over BitTorrent. Switch Settings → BitTorrent to the "
                     "built-in downloader, or start Transmission or qBittorrent with remote "
                     "access on, then press Try again.")


def torrent_info() -> dict:
    """The client the store would use, for Settings, without starting anything."""
    settings = library.load_settings()
    engine = "app" if settings.get("store_torrent_engine") == "app" else "builtin"
    base = {"engine": engine, "builtin_available": torrentengine.available(),
            "builtin_ready": bool(torrentengine.find())}
    if builtin_engine(settings):
        return {**base, "label": "Built-in downloader", "url": ""}
    client = torrentclient.from_settings(settings.get("torrent_client") or {})
    if client is None:
        return {**base, "label": "", "url": ""}
    configured = settings.get("torrent_client") or {}
    return {**base, "label": client.label,
            "url": getattr(client, "base", "") or getattr(client, "endpoint", ""),
            "username": str(configured.get("username") or ""),
            "signed_in": bool(configured.get("password"))}


def look_for_torrent_client() -> dict:
    found = torrentclient.detect()
    if found and not found.get("needs_login"):
        library.save_settings({"torrent_client": {"kind": found["kind"], "url": found["url"],
                                                  "username": "", "password": ""}})
        return {**torrent_info(), "message": f"Found {found['label']} {found['version']}".strip()}
    current = torrent_info()
    if found:
        library.save_settings({"torrent_client": {"kind": found["kind"], "url": found["url"],
                                                  "username": "", "password": ""}})
        return {**torrent_info(), "needs_login": True,
                "message": f"{found['label']} is running but wants its username and password. "
                           "Press Sign in."}
    if current["label"]:
        return {**current, "message": f"Nothing answered on the usual ports, so the "
                                      f"{current['label']} set up before stays in use."}
    return {**current, "message": "No torrent app is running with remote access on."}


def torrent_login(username: str, password: str) -> dict:
    """Save a torrent app's sign-in, typed with the controller, and try it."""
    settings = library.load_settings()
    configured = dict(settings.get("torrent_client") or {})
    if configured.get("kind") not in ("transmission", "qbittorrent"):
        found = torrentclient.detect()
        if not found:
            raise StoreError("No Transmission or qBittorrent is answering on this PC. Start it "
                             "with remote access on, or use the built-in downloader.")
        configured = {"kind": found["kind"], "url": found["url"]}
    configured.update(username=str(username or "")[:200], password=str(password or "")[:200])
    client = torrentclient.from_settings(configured)
    try:
        version = client.version()
    except torrentclient.TorrentClientError as exc:
        raise StoreError(str(exc)) from exc
    library.save_settings({"torrent_client": configured, "store_torrent_engine": "app"})
    return {**torrent_info(), "message": f"Signed in to {client.label} {version}".strip()}


def _speed(rate: int) -> str:
    return f"{rate / 1048576:.1f} MB/s" if rate >= 1048576 else f"{rate // 1024} KB/s"


def _eta(seconds: int) -> str:
    if seconds <= 0 or seconds > 30 * 86400:
        return ""
    if seconds < 90:
        return "under 2 minutes left"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes left"
    return f"{seconds / 3600:.1f} hours left"


TORRENT_TAG = "Game Store"
POLL_SECONDS = 3
CLIENT_GRACE = 180         # how long the torrent app may stop answering before giving up


@dataclass
class Patience:
    """When to give up on a torrent because something else could be tried.

    Only used when there is an alternative, such as the release's mirrors.
    With nothing else to try, waiting for sharers is the only way forward,
    so a torrent is waited on for as long as it takes.
    """
    details: int = 15 * 60      # seconds without the torrent's details while not queued
    stalled: int = 45 * 60      # seconds without a byte of progress while not queued


@dataclass
class TorrentResult:
    path: Path
    client: object
    info_hash: str
    ours: bool                  # added by the store, so the store may remove it


def _own_torrent(info_hash: str, gid: str = "", forget: bool = False) -> bool:
    """Remember, or forget, that the store added this torrent.

    Kept on disk because a download outlives the app: after a restart,
    pressing Install again finds the torrent already in the client, and
    it must still be known as the store's to tidy up - and a torrent the
    person added themselves must never be removed or have its files
    deleted by the store.
    """
    def change(state: dict) -> None:
        owned = state.setdefault("torrents", {})
        if forget:
            owned.pop(info_hash, None)
        else:
            owned[info_hash] = {"game": gid, "at": time.time()}
    if gid or forget:
        _change_state(change)
    return info_hash in (load_state().get("torrents") or {})


def _client_call(job: StoreJob, client, call, *args, **kwargs):
    """A call to the torrent app that tolerates it restarting.

    With many downloads running, the client being busy or restarted for a
    minute is normal and should not fail every one of them at once.
    """
    first_failure = 0.0
    while True:
        try:
            return call(*args, **kwargs)
        except torrentclient.TorrentClientError as exc:
            now = time.time()
            first_failure = first_failure or now
            if now - first_failure > CLIENT_GRACE or "password" in str(exc):
                raise StoreError(str(exc)) from exc
            job.message = f"{client.label} is not answering; waiting for it"
            if job.cancel:
                raise Stopped() from exc
            time.sleep(POLL_SECONDS)


def _choose_files(job: StoreJob, client, info_hash: str, skip) -> list[str]:
    """Leave out the files ``skip`` says are not needed. Returns their names.

    An older client that cannot choose files simply downloads them all;
    that costs disk space, not the game, so it is not an error.
    """
    unwanted: list = []
    try:
        files = client.files(info_hash)
        if not files:
            return []
        unwanted = [f for f in files if skip(PurePath(f["name"]).name)]
        if not unwanted or len(unwanted) == len(files):
            unwanted = []
            return []                       # never deselect everything
        client.skip(info_hash, [f["index"] for f in unwanted])
    except torrentclient.TorrentClientError:
        unwanted = []
    finally:
        _release_hold(client, info_hash)
    return [f["name"] for f in unwanted]


def _release_hold(client, info_hash: str) -> None:
    starter = getattr(client, "start", None)
    if starter is not None:
        try:
            starter(info_hash)
        except torrentclient.TorrentClientError:
            pass


def _torrent_download(job: StoreJob, url: str, folder: Path, settings: dict,
                      check_name: bool = True, skip=None,
                      patience: Patience | None = None) -> TorrentResult:
    """Have the torrent client fetch it into ``folder``; wait for it.

    ``skip(file_name)`` names files of the torrent not to download, such as
    a release's optional extras. ``patience`` gives up on a torrent that
    cannot get going, for when there is another way to get the game.
    """
    client = torrent_client(settings)
    magnet, blob, name = "", b"", ""
    if url.lower().startswith("magnet:"):
        magnet = url
        info_hash = torrentclient.info_hash(url)
        name = urllib.parse.unquote_plus(
            (urllib.parse.parse_qs(url.split("?", 1)[-1]).get("dn") or [""])[0])
    else:
        try:
            blob = archive.fetch_bytes(url)
            info = torrents.read(blob)
        except (OSError, torrents.TorrentError) as exc:
            raise StoreError(f"The .torrent file could not be read ({exc}).") from exc
        info_hash, name = info["hash"], info.get("name", "")
    if not info_hash:
        raise StoreError("That magnet link does not say which torrent it is.")
    info_hash = info_hash.lower()
    problem = _not_the_game(name) if name and check_name else ""
    if problem:
        raise StoreError(problem)

    job.message = f"Handing it to {client.label}"
    present = _client_call(job, client, client.status, [info_hash])
    ours = _own_torrent(info_hash)
    if not present:
        folder.mkdir(parents=True, exist_ok=True)
        try:
            client.add(magnet=magnet, torrent=blob, save_path=str(folder), tag=TORRENT_TAG)
        except torrentclient.TorrentClientError as exc:
            raise StoreError(str(exc)) from exc
        ours = _own_torrent(info_hash, gid=job.id)

    added_at = time.time()
    last_progress, last_change = -1.0, time.time()
    details_wait = 0.0
    skipped: list[str] = []
    # Never change file choices on someone else's torrent. The built-in
    # downloader holds every new torrent until its files are chosen, so it
    # always goes through the choosing step, even with nothing to leave out.
    holds = hasattr(client, "start")
    chosen = not ours or (skip is None and not holds)
    skip = skip or (lambda name: False)

    def give_up(reason: str) -> None:
        if ours:
            # Switching to another way of getting the game: what was
            # fetched so far would only take up the disk.
            _forget_torrent(client, info_hash, delete_files=True)
        raise StoreError(reason)

    while True:
        if job.cancel:
            if ours:
                _forget_torrent(client, info_hash, delete_files=True)
            raise Stopped()
        items = _client_call(job, client, client.status, [info_hash])
        now = time.time()
        if not items:
            if now - added_at > 90:
                if ours:
                    _own_torrent(info_hash, forget=True)
                raise StoreError(f"{client.label} no longer has the torrent; it may have "
                                 "been removed there.")
            time.sleep(POLL_SECONDS)
            continue
        item = items[0]
        state = item["state"]
        if state == "error":
            raise StoreError(f"{client.label}: {item['error'] or 'the torrent stopped with an error.'}")

        if not chosen and state != "fetching details":
            skipped = _choose_files(job, client, info_hash, skip)
            chosen = True
            continue                        # progress is now counted over what is left
        if holds and ours and state == "paused":
            _release_hold(client, info_hash)

        if item["progress"] >= 1 and state not in ("checking", "moving", "fetching details"):
            content = item.get("content") or os.path.join(item.get("path", ""), item.get("name", ""))
            path = Path(content)
            if not path.exists():
                raise StoreError(f"{client.label} finished, but the files are not at {path}.")
            _drop_skipped(path, item, skipped)
            return TorrentResult(path, client, info_hash, ours)

        # Time spent in the client's own queue, or paused by the person, is
        # not the torrent failing; only the rest counts against patience.
        idle = state in ("queued", "paused")
        if idle:
            last_change = now
        if item["progress"] > last_progress:
            last_progress, last_change = item["progress"], now
        if state == "fetching details" and not idle:
            details_wait += POLL_SECONDS
        if patience is not None and not idle:
            if details_wait > patience.details:
                give_up(f"Nobody shared the torrent's details in {patience.details // 60} "
                        "minutes.")
            if state != "fetching details" and now - last_change > patience.stalled:
                give_up(f"The torrent made no progress for {patience.stalled // 60} minutes.")

        if state == "fetching details":
            job.progress, job.message = -1.0, "Finding the torrent's details"
        elif state in ("stalled", "queued", "paused"):
            job.progress = item["progress"] if item["progress"] > 0 else -1.0
            job.message = {"stalled": "Waiting for someone sharing it",
                           "queued": f"Queued in {client.label} behind other downloads",
                           "paused": f"Paused in {client.label}"}[state]
        else:
            job.progress = item["progress"]
            parts = [f"{item['progress'] * 100:.0f}%"]
            if item.get("speed"):
                parts.append(_speed(item["speed"]))
            if _eta(item.get("eta", 0)):
                parts.append(_eta(item["eta"]))
            job.message = " · ".join(parts)
        time.sleep(POLL_SECONDS)


def _drop_skipped(content: Path, item: dict, skipped: list[str]) -> None:
    """Delete what a client left of files it was told not to download.

    A piece can straddle two files, so a client may write the start of a
    skipped file. A setup program that finds a half-written optional file
    beside it tries to install it and fails its checksum.
    """
    if not skipped:
        return
    base = Path(item.get("path") or "")
    if base == content or not base.is_dir():
        base = content.parent
    for name in skipped:
        target = base / name
        try:
            target.resolve().relative_to(content.resolve())
        except (OSError, ValueError):
            continue                       # only ever inside this torrent's own folder
        try:
            if target.is_file():
                target.unlink()
        except OSError:
            pass


def _forget_torrent(client, info_hash: str, delete_files: bool = False) -> None:
    """Take a finished or cancelled torrent out of the client's list."""
    try:
        client.remove([info_hash], delete_files=delete_files)
    except torrentclient.TorrentClientError:
        return                             # still remembered, so a later tidy can retry
    _own_torrent(info_hash, forget=True)


# --------------------------------------------------------------------------
# uninstalling
# --------------------------------------------------------------------------

def uninstall_game(gid: str) -> StoreJob:
    """Remove a store game: its uninstaller or folder, then its Steam entry."""
    with _LOCK:
        job = JOBS.get(gid)
        if job and job.phase in ACTIVE_PHASES:
            raise StoreError("Wait until it has finished what it is doing.")
        record = load_state()["installed"].get(gid)
        if not record:
            raise StoreError("That game was not installed from the store.")
        job = StoreJob(id=gid, name=record.get("name", "Game"), phase="uninstalling",
                       message="Uninstalling", started=time.time())
        JOBS[gid] = job
    threading.Thread(target=_run_uninstall, args=(job, gid, record), daemon=True).start()
    return job


def _norm(path) -> str:
    return os.path.normcase(os.path.normpath(str(path))).rstrip("\\/")


def _inside(child, parent) -> bool:
    child, parent = _norm(child), _norm(parent)
    return child == parent or child.startswith(parent + os.sep)


def registered_program(record: dict) -> dict | None:
    """The Programs and Features entry an installed game left, if any.

    Matched only by where it lives, never by name, so a library root or
    another game in the same folder can never be taken for it.
    """
    folder = record.get("dir")
    if not folder or not uninstall.IS_WINDOWS:
        return None
    from . import winproc
    for program in uninstall.list_programs():
        location = program.get("location")
        if location and _inside(location, folder):
            return program
        command = program.get("quiet") or program.get("uninstall") or ""
        try:
            exe = winproc.split_command(command)[0]
        except Exception:
            exe = ""
        if exe and _inside(exe, folder):
            return program
    return None


def deletable_folder(folder: Path, settings: dict) -> str:
    """Why a game folder may not be deleted, or "" when it may."""
    roots = [Path(f["path"]).expanduser() for f in settings.get("library_folders") or []
             if f.get("path")]
    try:
        resolved = folder.resolve()
    except OSError as exc:
        return f"{folder} cannot be read ({exc.strerror or exc})."
    if uninstall._is_reparse(folder):
        return f"{folder} is a link to somewhere else, so it is left alone."
    for root in roots:
        try:
            base = root.resolve()
        except OSError:
            continue
        if base in resolved.parents:
            return ""
    return (f"{folder} is not inside one of your library folders, so the store will not "
            "delete it. Remove it by hand if you want it gone.")


def _force_rmtree(folder: Path) -> None:
    import stat

    def unlock(function, path, _info):
        # Games ship read-only files; Windows will not delete those as they are.
        try:
            os.chmod(path, stat.S_IWRITE)
            function(path)
        except OSError:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(folder, onexc=unlock)
    else:
        shutil.rmtree(folder, onerror=unlock)


def _run_uninstall(job: StoreJob, gid: str, record: dict) -> None:
    try:
        settings = library.load_settings()
        folder = Path(record["dir"]) if record.get("dir") else None
        notes = []
        program = registered_program(record)
        if program:
            job.message = "Running its uninstaller"
            outcome = autoinstall.run_uninstaller(program, silent=bool(program.get("can_be_quiet")))
            if not outcome.get("ok"):
                raise StoreError(outcome.get("message") or "Its uninstaller did not finish.")
        if folder is not None and folder.exists():
            job.message = "Deleting its folder"
            problem = deletable_folder(folder, settings)
            if problem and not program:
                raise StoreError(problem)
            if problem:
                notes.append(problem)
            else:
                _force_rmtree(folder)
                if folder.exists():
                    notes.append(f"Some files in {folder} were in use and are still there.")
        job.message = "Taking it out of Steam"
        notes.insert(0, _remove_from_steam(record, settings))
        _change_state(lambda state: state["installed"].pop(gid, None))
        job.phase, job.finished, job.progress = "done", time.time(), 1.0
        job.message = " ".join(n for n in ["Uninstalled."] + notes if n)
        if load_state()["pending_remove"] and settings.get("store_restart") == "now":
            threading.Thread(target=_restart_when_idle, daemon=True).start()
    except Exception as exc:
        job.phase, job.finished, job.progress = "failed", time.time(), -1.0
        job.error = job.message = str(exc) or type(exc).__name__


def _remove_from_steam(record: dict, settings: dict) -> str:
    title = record.get("name", "")
    if record.get("steam") == "pending":
        # It never reached Steam: it just stops waiting to.
        _change_state(lambda state: state.__setitem__(
            "pending", [p for p in state["pending"] if p.get("title") != title]))
        return ""
    short_id = int(record.get("short_id") or 0)
    if record.get("steam") != "added" or not short_id:
        return ""
    if not steam.steam_is_running():
        user = _steam_user(settings)
        if user is None:
            return "No Steam account was found to take it out of."
        library.remove_games(library.Job(id=uuid.uuid4().hex[:12]), user, [short_id], True)
        return "It is out of your Steam library."

    def keep(state: dict) -> None:
        rest = [r for r in state["pending_remove"] if int(r.get("short_id", 0)) != short_id]
        state["pending_remove"] = rest + [{"short_id": short_id, "title": title}]
    _change_state(keep)
    return "It leaves your Steam library when Steam restarts."


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
    entry["sgdb_hint"] = game.get("art_name") or game["name"]
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
    removals = state["pending_remove"]
    if not pending and not removals:
        return 0
    settings = library.load_settings()
    user = _steam_user(settings)
    if user is None:
        raise StoreError("No Steam account was found on this computer.")
    if removals:
        gone = library.Job(id=uuid.uuid4().hex[:12])
        library.remove_games(gone, user, [int(r["short_id"]) for r in removals], True)
        log(f"Took {gone.result.get('removed', 0)} uninstalled game(s) out of Steam.")
        _change_state(lambda state: state.__setitem__("pending_remove", []))
    if not pending:
        return len(removals)
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
    return len(pending) + len(removals)


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
        state = load_state()
        if not state["pending"] and not state["pending_remove"]:
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
    if STREAMING:
        # Streamed as its own app: the store stays, so nothing needs
        # reopening, and Steam starts quietly behind it rather than taking
        # over the screen in the middle of the session.
        relaunch = False
    args = (["--steam-refresh"] + (["--relaunch-store"] if relaunch else [])
            + (["--steam-background"] if STREAMING else []))
    command, cwd = self_command(*args)
    _spawn_detached(command, cwd)
    leaving = STORE_WINDOW and not STREAMING
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


def run_refresh(relaunch: bool, background: bool = False) -> int:
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
    if background:
        log("Starting Steam in the background")
        opened = steam.open_silently()
    else:
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
    "store_glyphs": ("auto", "xbox", "playstation"),
    "store_site_method": ("auto", "direct", "torrent"),
    "store_torrent_engine": ("builtin", "app"),
    "store_sort": ("newest", "name"),
}


def _running_as_admin() -> bool:
    try:
        from . import winproc
        return bool(winproc.is_admin())
    except Exception:
        return False


def _glyphs(settings: dict) -> str:
    """Which button symbols to show: PlayStation unless someone picked otherwise.

    "auto" used to be the default, and over Moonlight it is misleading:
    Sunshine hands Windows a virtual Xbox pad whatever is in your hands, so
    a DualSense got Xbox letters. An "auto" nobody chose is read as PlayStation.
    """
    value = settings.get("store_glyphs", "playstation")
    if value == "auto" and not settings.get("store_glyphs_chosen"):
        return "playstation"
    return value if value in ("auto", "xbox", "playstation") else "playstation"


def store_settings(settings: dict | None = None) -> dict:
    settings = settings or library.load_settings()
    return {
        "restart": settings.get("store_restart", "exit"),
        "reopen": settings.get("store_reopen", "bigpicture"),
        "glyphs": _glyphs(settings),
        "sort": settings.get("store_sort", "newest"),
        "mute_installers": settings.get("mute_installers", True) is not False,
        "no_desktop_shortcuts": settings.get("no_desktop_shortcuts", True) is not False,
        "run_as_admin": bool(settings.get("run_as_admin")),
        "is_admin": _running_as_admin(),
        "clear_run_as_admin": settings.get("clear_run_as_admin", True) is not False,
        "extras": bool(settings.get("store_extras")),
        "verify": bool(settings.get("store_verify")),
        "keep_downloads": bool(settings.get("store_keep_downloads")),
        "keep_seeding": bool(settings.get("store_keep_seeding")),
        "builtin": settings.get("store_builtin_enabled", True) is not False,
        "site_method": settings.get("store_site_method", "auto"),
        "optional_files": bool(settings.get("store_optional_files")),
        "site_pages": _site_pages(settings),
        "parallel": parallel_downloads(settings),
        "torrent": torrent_info(),
        "catalogues": clean_catalogue_list(settings.get("store_catalogues")),
    }


def save_store_settings(payload: dict) -> dict:
    patch: dict = {}
    for key, allowed in SETTING_KEYS.items():
        short = key.replace("store_", "")
        if payload.get(short) in allowed:
            patch[key] = payload[short]
    if "store_glyphs" in patch:
        patch["store_glyphs_chosen"] = True
    if "extras" in payload:
        patch["store_extras"] = bool(payload["extras"])
    if "verify" in payload:
        patch["store_verify"] = bool(payload["verify"])
    if "run_as_admin" in payload:
        patch["run_as_admin"] = bool(payload["run_as_admin"])
    for flag in ("no_desktop_shortcuts", "clear_run_as_admin"):
        if flag in payload:
            patch[flag] = bool(payload[flag])
    if "mute_installers" in payload:
        patch["mute_installers"] = bool(payload["mute_installers"])
    if "ask_drive" in payload:
        patch["store_ask_drive"] = bool(payload["ask_drive"])
    if "keep_downloads" in payload:
        patch["store_keep_downloads"] = bool(payload["keep_downloads"])
    if "builtin" in payload:
        patch["store_builtin_enabled"] = bool(payload["builtin"])
    if "keep_seeding" in payload:
        patch["store_keep_seeding"] = bool(payload["keep_seeding"])
    if "catalogues" in payload:
        patch["store_catalogues"] = clean_catalogue_list(payload["catalogues"])
        before = {c["id"] for c in clean_catalogue_list(
            library.load_settings().get("store_catalogues"))}
        for gone in before - {c["id"] for c in patch["store_catalogues"]}:
            sitesync.forget(gone)
    if "optional_files" in payload:
        patch["store_optional_files"] = bool(payload["optional_files"])
    if "parallel" in payload:
        patch["store_parallel_downloads"] = parallel_downloads(
            {"store_parallel_downloads": payload["parallel"]})
    if "site_pages" in payload:
        patch["store_site_pages"] = _site_pages({"store_site_pages": payload["site_pages"]})
        # Deeper than before: the background read carries on from where it stopped.
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
            item.pop("release", None)       # mirror lists are for installing, not showing
            # A site can put thousands of games on the shelf; descriptions
            # come with a game's page instead, when it is opened.
            item.pop("blurb", None)
            item["catalogue_name"] = entry["name"]
            item["installable"] = installable(game)
            item["seq"] = len(games)        # the catalogue's own order: a site's newest first
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
        "library": library_overview(settings),
        "artwork_key": bool(str(settings.get("api_key", "")).strip()),
        "store_shortcut": bool(state.get("store_shortcut_added")),
        "store_window": STORE_WINDOW,
        "streaming": STREAMING,
        "sunshine_command": sunshine_command(),
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
        "pending_remove": [r.get("title", "") for r in state["pending_remove"]],
        "busy": busy(),
        "embed": embed.MANAGER.current(),
        "steam_running": _steam_running(),
        "sync": site_progress(),
    }


def site_progress() -> dict:
    """Release websites being read right now: catalogue id -> where it has got to."""
    out = {}
    for entry in clean_catalogue_list(library.load_settings().get("store_catalogues")):
        live = sitesync.progress(entry["id"])
        if live:
            out[entry["id"]] = live
    return out
