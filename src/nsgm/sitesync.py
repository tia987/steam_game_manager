"""Keep a release website's shelf up to date without reading it all again.

A site like this can run to hundreds of pages and thousands of releases.
Reading all of it every time the store opens would take many minutes and
lean on somebody else's server for nothing, so each site is kept on disk
and only what changed is read:

* **New releases.** Blogs list newest first. A check reads from the front
  page onward and stops at the first page with nothing new on it, which
  is usually the front page itself: one request.
* **Older releases.** The first time, the rest of the site is read in the
  background, a page at a time and gently, as deep as the store is set to
  go (or all of it). Progress is saved as it goes, so closing the app
  half way loses nothing; the next start carries on where it stopped.
  Raising the depth later carries on from there too.

Nothing here blocks the store opening. It shows what is already on disk
straight away, and the shelf grows as pages arrive.

Releases are recognised by their post's address. A post seen again with
a new title (a repack updated to a new version) has its entry refreshed;
a newer post for a game already on the shelf replaces the older one.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from pathlib import Path

from . import library, sitecatalog

CHECK_MINUTES = 30          # how often the front page is looked at for new releases
SAVE_EVERY = 10             # pages between saves during a long read
PAGE_DELAY = 0.6            # seconds between pages: be gentle with the site
RETRY_WAITS = (5, 20, 60)   # back off this long when the site answers badly
MAX_GAMES = 30000
ALL_PAGES = 0

_LOCK = threading.RLock()
_MEMO: dict = {}            # catalogue id -> (file stamp, state)
_RUNNING: dict = {}         # catalogue id -> live progress of a read in flight


def _path(cid: str) -> Path:
    return library.config_dir() / f"store-site-{cid}.json"


def _blank(source: str) -> dict:
    return {"source": source, "name": "", "games": [], "next": "", "pages_read": 0,
            "total_pages": 0, "complete": False, "checked": 0.0, "error": ""}


def has_state(cid: str) -> bool:
    return _path(cid).is_file()


def load(cid: str, source: str) -> dict:
    """What is known about one site, from disk, read again only if it changed."""
    path = _path(cid)
    with _LOCK:
        try:
            info = path.stat()
            stamp = (info.st_mtime_ns, info.st_size)
        except OSError:
            return _blank(source)
        cached = _MEMO.get(cid)
        if cached and cached[0] == stamp:
            state = cached[1]
        else:
            try:
                state = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                state = {}
            if not isinstance(state, dict):
                state = {}
            _MEMO[cid] = (stamp, state)
    if state.get("source") != source:
        return _blank(source)            # the address was changed: start over
    merged = _blank(source)
    merged.update(state)
    return merged


def save(cid: str, state: dict) -> None:
    path = _path(cid)
    with _LOCK:
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(state, separators=(",", ":")), "utf-8")
            tmp.replace(path)
            info = path.stat()
            _MEMO[cid] = ((info.st_mtime_ns, info.st_size), state)
        except OSError:
            pass


def forget(cid: str) -> None:
    with _LOCK:
        _MEMO.pop(cid, None)
        try:
            _path(cid).unlink()
        except OSError:
            pass


def progress(cid: str) -> dict:
    """What a read in flight is doing, for the interface; {} when idle."""
    with _LOCK:
        return dict(_RUNNING.get(cid) or {})


def seed(cid: str, source: str, name: str, games: list) -> None:
    """Start a site off with its front page, already read when it was checked."""
    state = _blank(source)
    state.update(name=name, games=list(games))
    save(cid, state)


def due(state: dict, limit: int) -> bool:
    if time.time() - float(state.get("checked") or 0) > CHECK_MINUTES * 60:
        return True
    deeper = limit == ALL_PAGES or int(state.get("pages_read") or 0) < limit
    return not state.get("complete") and deeper


def ensure(cid: str, source: str, limit: int, make_game, force: bool = False,
           language: str = "english") -> bool:
    """Bring a site up to date in the background, if it needs it. Never waits.

    ``make_game(post)`` turns a post into a store game record, or None.
    Returns whether a read was started.
    """
    with _LOCK:
        if cid in _RUNNING:
            return False
        state = load(cid, source)
        if not force and not due(state, limit):
            return False
        _RUNNING[cid] = {"phase": "new", "page": 0, "of": state.get("total_pages", 0),
                         "found": 0}
    thread = threading.Thread(target=_run, args=(cid, source, limit, make_game, language),
                              daemon=True, name=f"site-sync-{cid}")
    thread.start()
    return True


def _key(url: str) -> str:
    parts = urllib.parse.urlparse(url or "")
    return (parts.netloc.lower().removeprefix("www.") + parts.path.rstrip("/").lower())


def _fetch(url: str) -> tuple[str, str]:
    """One page, retried with growing pauses when the site is struggling."""
    last: Exception | None = None
    for wait in (0,) + RETRY_WAITS:
        if wait:
            time.sleep(wait)
        try:
            return sitecatalog.fetch(url)
        except sitecatalog.SiteError as exc:
            last = exc
            text = str(exc)
            # Gone or forbidden will not change by waiting.
            if "HTTP 404" in text or "HTTP 403" in text or "HTTP 410" in text:
                break
    raise last or sitecatalog.SiteError("The site could not be read.")


def _run(cid: str, source: str, limit: int, make_game, language: str) -> None:
    state = load(cid, source)
    try:
        _read_new(cid, source, state, limit, make_game, language)
        _read_older(cid, source, state, limit, make_game, language)
        state["error"] = ""
    except sitecatalog.SiteError as exc:
        state["error"] = str(exc)
    except Exception as exc:                  # a background read must never take the app down
        state["error"] = f"Reading the site stopped: {exc}"
    finally:
        state["checked"] = time.time()
        save(cid, state)
        with _LOCK:
            _RUNNING.pop(cid, None)


def _index(games: list) -> tuple[dict, dict]:
    by_page = {_key(g.get("page", "")): n for n, g in enumerate(games)}
    by_id = {g["id"]: n for n, g in enumerate(games)}
    return by_page, by_id


def _read_new(cid: str, source: str, state: dict, limit: int, make_game, language: str) -> None:
    """From the front page onward, until a page brings nothing new."""
    games = state["games"]
    if not games or not state.get("pages_read"):
        return                                 # the first full read is still to start
    by_page, by_id = _index(games)
    fresh: list = []
    replaced: set = set()
    url, page = source, 0
    while url:
        page += 1
        _status(cid, phase="new", page=page, of=state.get("total_pages", 0), found=len(fresh))
        text, final = _fetch(url)
        listing = sitecatalog.read_listing(text, final, language)
        if page == 1:
            state["total_pages"] = max(listing["last_page"], 1)
            state["name"] = state.get("name") or listing["site"]
        met_known = False
        for post in listing["posts"]:
            game = make_game(post)
            if not game:
                continue
            spot = by_page.get(_key(game["page"]))
            if spot is not None:
                games[spot] = game             # seen before: refresh title, version, size
                # Newest first: past the first release already on the
                # shelf, everything is. A pinned post proves nothing.
                met_known = met_known or not post.get("sticky")
                continue
            spot = by_id.get(game["id"])
            if spot is not None:
                older = (game.get("date") or "") < (games[spot].get("date") or "")
                if spot not in replaced and not older:
                    games[spot] = game         # a newer post of a game already shelved
                    replaced.add(spot)
                    by_page[_key(game["page"])] = spot
                continue
            if any(f["id"] == game["id"] for f in fresh):
                continue
            fresh.append(game)
        if met_known or not listing["next"] or (limit and page >= limit):
            break
        url = listing["next"]
        time.sleep(PAGE_DELAY)
    if fresh:
        state["games"] = (fresh + games)[:MAX_GAMES]


def _read_older(cid: str, source: str, state: dict, limit: int, make_game,
                language: str) -> None:
    """Carry on down the site from where the last read stopped."""
    if state.get("complete"):
        return
    url = state.get("next") or ("" if state.get("pages_read") else source)
    if not url and state.get("pages_read"):
        state["complete"] = True
        return
    by_page, by_id = _index(state["games"])
    since_save = 0
    while url and (limit == ALL_PAGES or state["pages_read"] < limit):
        if len(state["games"]) >= MAX_GAMES:
            break
        page = state["pages_read"] + 1
        _status(cid, phase="older", page=page, of=state.get("total_pages", 0),
                found=len(state["games"]))
        text, final = _fetch(url)
        listing = sitecatalog.read_listing(text, final, language)
        if page == 1:
            state["total_pages"] = max(listing["last_page"], 1)
            state["name"] = state.get("name") or listing["site"]
        for post in listing["posts"]:
            game = make_game(post)
            if not game:
                continue
            if _key(game["page"]) in by_page:
                continue
            if game["id"] in by_id:
                continue                       # an older post of a game already shelved
            by_page[_key(game["page"])] = by_id[game["id"]] = len(state["games"])
            state["games"].append(game)
        state["pages_read"] = page
        state["total_pages"] = max(state.get("total_pages", 0), listing["last_page"], page)
        following = listing["next"]
        if following and _key(following) == _key(url):
            following = ""
        state["next"] = following
        if not following:
            state["complete"] = True
        url = following
        since_save += 1
        if since_save >= SAVE_EVERY:
            state["checked"] = time.time()
            save(cid, state)
            since_save = 0
        if url:
            time.sleep(PAGE_DELAY)


def _status(cid: str, **values) -> None:
    with _LOCK:
        if cid in _RUNNING:
            _RUNNING[cid].update(values)
