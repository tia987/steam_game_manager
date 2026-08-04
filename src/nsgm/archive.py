"""Read the Internet Archive through its own APIs rather than its HTML.

archive.org publishes two endpoints that make guesswork unnecessary:

``/services/search/v1/scrape``
    Walks a collection with a cursor. Every item, however many pages the
    website would have shown, without a page number in sight.

``/metadata/<identifier>``
    Everything about one item, including its full file list.

Items that are seeded also carry ``<identifier>_archive.torrent``. The
magnet for it is derived from that file, so a magnet is available for
every seeded item whether or not the page happens to print one.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from . import torrents

SCRAPE = "https://archive.org/services/search/v1/scrape"
METADATA = "https://archive.org/metadata/"
DOWNLOAD = "https://archive.org/download/"
DETAILS = "https://archive.org/details/"

USER_AGENT = "NonSteamGameManager/1.0 (+local download helper)"


class ArchiveError(RuntimeError):
    pass


def is_archive(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    return host in ("archive.org", "www.archive.org", "web.archive.org")


def identifier_of(url: str) -> str:
    """The item or collection name inside an archive.org address."""
    parts = urllib.parse.urlparse(url)
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) >= 2 and segments[0] in ("details", "download", "metadata"):
        return segments[1]
    return ""


def search_query(url: str) -> str:
    """A search address carries its query in the URL."""
    parts = urllib.parse.urlparse(url)
    if not parts.path.rstrip("/").endswith("search"):
        return ""
    fields = urllib.parse.parse_qs(parts.query)
    for key in ("query", "q", "and[]"):
        if fields.get(key):
            return fields[key][0]
    return ""


def _get(url: str, timeout: int = 25) -> dict:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise ArchiveError(f"archive.org answered HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ArchiveError(f"Could not reach archive.org ({exc}).") from exc
    except json.JSONDecodeError as exc:
        raise ArchiveError("archive.org sent something unreadable.") from exc


def fetch_bytes(url: str, timeout: int = 45) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


# --------------------------------------------------------------------------
# collections and searches
# --------------------------------------------------------------------------

def metadata(identifier: str, timeout: int = 25) -> dict:
    return _get(METADATA + urllib.parse.quote(identifier), timeout)


def is_collection(identifier: str, timeout: int = 25) -> bool:
    try:
        meta = (metadata(identifier, timeout).get("metadata") or {})
    except ArchiveError:
        return False
    return str(meta.get("mediatype", "")).lower() == "collection"


def scrape(query: str, want: int = 500, timeout: int = 25, progress=None) -> list[dict]:
    """Every item matching a query, walked by cursor.

    The cursor is the whole point: there is no page number to get wrong
    and no last page to miss. It keeps going until the Archive stops
    handing back a cursor, or until enough items have been collected.
    """
    fields = "identifier,title,mediatype,downloads,item_size,year"
    items: list[dict] = []
    cursor = ""

    while len(items) < want:
        params = {"q": query, "fields": fields, "count": 100}
        if cursor:
            params["cursor"] = cursor
        payload = _get(f"{SCRAPE}?{urllib.parse.urlencode(params)}", timeout)

        batch = payload.get("items") or []
        for item in batch:
            identifier = item.get("identifier")
            if not identifier:
                continue
            items.append(
                {
                    "name": item.get("title") or identifier,
                    "url": DETAILS + identifier,
                    "identifier": identifier,
                    "mediatype": item.get("mediatype", ""),
                    "year": item.get("year", ""),
                }
            )
        if progress:
            progress(len(items), payload.get("total", 0))

        cursor = payload.get("cursor") or ""
        if not cursor or not batch:
            break

    return items[:want]


def collect(url: str, want: int = 500, timeout: int = 25, progress=None) -> dict:
    """Treat an archive.org address as a listing and return its items."""
    query = search_query(url)
    identifier = identifier_of(url)
    if not query:
        if not identifier:
            raise ArchiveError("That archive.org address is not a collection or search.")
        query = f"collection:{identifier}"

    items = scrape(query, want, timeout, progress)
    return {
        "programs": items,
        "site": "archive.org",
        "query": query,
        "pages_read": max(1, (len(items) + 99) // 100),
        "exhausted": len(items) < want,
        "repeated_dropped": 0,
    }


# --------------------------------------------------------------------------
# one item
# --------------------------------------------------------------------------

SKIP_FORMATS = {
    "metadata", "item tile", "archive bittorrent", "web archive gzip",
    "json", "text", "png", "jpeg", "thumbnail", "columnar",
}


def item(url: str, timeout: int = 25, want_magnet: bool = True) -> dict:
    """One archive.org item: its files, and a magnet if it is seeded."""
    identifier = identifier_of(url)
    if not identifier:
        raise ArchiveError("That does not look like an archive.org item.")

    payload = metadata(identifier, timeout)
    meta = payload.get("metadata") or {}
    if not meta:
        raise ArchiveError(f"archive.org has no item called {identifier}.")

    title = meta.get("title") or identifier
    base = DOWNLOAD + urllib.parse.quote(identifier) + "/"
    candidates: list[dict] = []
    torrent_url = ""

    for entry in payload.get("files") or []:
        name = entry.get("name") or ""
        if not name:
            continue
        fmt = str(entry.get("format", "")).lower()
        try:
            size = int(entry.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        link = base + urllib.parse.quote(name)

        if name.endswith("_archive.torrent") or fmt == "archive bittorrent":
            torrent_url = link
            candidates.append(
                {"url": link, "name": name, "label": "torrent file", "size": size,
                 "score": 40, "usable": False, "kind": "torrent", "mirrors": 0}
            )
            continue
        if fmt in SKIP_FORMATS or name.startswith("__ia_thumb"):
            continue
        candidates.append(
            {"url": link, "name": name, "label": entry.get("format", ""), "size": size,
             "score": 50, "usable": True, "kind": "file", "mirrors": 0}
        )

    # The magnet is not published anywhere; it is derived from the torrent.
    if want_magnet and torrent_url:
        try:
            info = torrents.read(fetch_bytes(torrent_url, timeout))
            candidates.insert(
                0,
                {
                    "url": torrents.magnet(fetch_bytes(torrent_url, timeout)),
                    "name": info["name"] or title,
                    "label": "magnet, derived from the torrent",
                    "size": info["size"],
                    "score": 60,
                    "usable": False,
                    "kind": "magnet",
                    "mirrors": 0,
                    "magnet": {"hash": info["hash"], "name": info["name"],
                               "trackers": info["trackers"], "size": info["size"]},
                },
            )
        except (torrents.TorrentError, OSError, urllib.error.URLError):
            pass          # the item simply is not seeded

    candidates.sort(key=lambda c: (not c["usable"], -c["score"]))
    return {
        "method": "archive.org",
        "project": title,
        "version": str(meta.get("year") or ""),
        "released": str(meta.get("publicdate") or "")[:10],
        "page": DETAILS + identifier,
        "candidates": candidates,
        "checksums": [],
        "note": "",
        "pages_read": 1,
        "entry_count": 0,
        "mode": "files",
        "diagnostics": {},
    }
