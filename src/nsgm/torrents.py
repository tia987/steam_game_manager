"""Read .torrent files and work out their magnet links.

Sites that publish over BitTorrent do not always print a magnet next to
the download. Very often there is only a ``.torrent`` file, and the
magnet is derivable from it: the info hash is the SHA-1 of the file's
``info`` dictionary, exactly as it appears on disk.

That is what this does, so a magnet can be offered anywhere a .torrent
is found rather than only where a site happened to spell one out.
"""

from __future__ import annotations

import hashlib
import urllib.parse


class TorrentError(ValueError):
    pass


# --------------------------------------------------------------------------
# bencode
# --------------------------------------------------------------------------

def _decode(data: bytes, i: int):
    """Return (value, next_index). Raises TorrentError on malformed input."""
    if i >= len(data):
        raise TorrentError("torrent data ended unexpectedly")
    marker = data[i : i + 1]

    if marker == b"i":                                   # i<int>e
        end = data.index(b"e", i)
        return int(data[i + 1 : end]), end + 1

    if marker == b"l":                                   # l<items>e
        i += 1
        items = []
        while data[i : i + 1] != b"e":
            value, i = _decode(data, i)
            items.append(value)
        return items, i + 1

    if marker == b"d":                                   # d<key><value>e
        i += 1
        out: dict = {}
        while data[i : i + 1] != b"e":
            key, i = _decode(data, i)
            value, i = _decode(data, i)
            out[key] = value
        return out, i + 1

    if marker.isdigit():                                 # <length>:<bytes>
        colon = data.index(b":", i)
        length = int(data[i:colon])
        start = colon + 1
        return data[start : start + length], start + length

    raise TorrentError(f"unexpected byte {marker!r} at {i}")


def decode(data: bytes):
    value, _ = _decode(data, 0)
    return value


def _info_span(data: bytes) -> tuple[int, int]:
    """Byte range of the top level ``info`` value.

    The hash has to be taken over the original bytes rather than anything
    re-encoded, or it will not match what trackers and peers expect.
    """
    if data[:1] != b"d":
        raise TorrentError("not a torrent file")
    i = 1
    while i < len(data) and data[i : i + 1] != b"e":
        key, i = _decode(data, i)
        start = i
        _value, i = _decode(data, i)
        if key == b"info":
            return start, i
    raise TorrentError("this torrent has no info section")


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


def read(data: bytes) -> dict:
    """Pull the useful parts out of a .torrent file."""
    if not data or data[:1] != b"d":
        raise TorrentError("that file is not a torrent")
    meta = decode(data)
    if not isinstance(meta, dict):
        raise TorrentError("that torrent could not be read")

    start, end = _info_span(data)
    info_hash = hashlib.sha1(data[start:end]).hexdigest()

    info = meta.get(b"info") or {}
    name = _text(info.get(b"name"))

    size = 0
    if isinstance(info.get(b"length"), int):
        size = info[b"length"]
    else:
        for entry in info.get(b"files") or []:
            if isinstance(entry, dict) and isinstance(entry.get(b"length"), int):
                size += entry[b"length"]

    trackers: list[str] = []
    announce = _text(meta.get(b"announce"))
    if announce:
        trackers.append(announce)
    for tier in meta.get(b"announce-list") or []:
        for item in tier if isinstance(tier, list) else [tier]:
            value = _text(item)
            if value and value not in trackers:
                trackers.append(value)

    return {"hash": info_hash, "name": name, "size": size, "trackers": trackers}


def magnet(data: bytes, limit_trackers: int = 6) -> str:
    """Build the magnet link for a .torrent file's contents.

    The ``xt`` value keeps its colons literal. Percent-encoding them is
    technically valid but some clients will not take it.
    """
    info = read(data)
    parts = [f"xt=urn:btih:{info['hash']}"]
    if info["name"]:
        parts.append("dn=" + urllib.parse.quote(info["name"], safe=""))
    if info["size"]:
        parts.append(f"xl={info['size']}")
    for tracker in info["trackers"][:limit_trackers]:
        parts.append("tr=" + urllib.parse.quote(tracker, safe=""))
    return "magnet:?" + "&".join(parts)
