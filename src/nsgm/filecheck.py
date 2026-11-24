"""Check an installed game against the checksum lists it came with.

Repacks ship a list of every installed file's checksum (an ``.md5`` file,
sometimes ``.sfv``) and a small program, often QuickSFV, that the
installer opens at the end to check them. That program is a window full
of buttons, so the store stops it and, when asked, does the same check
itself, quietly, reporting only what is wrong.
"""

from __future__ import annotations

import hashlib
import os
import re
import zlib
from pathlib import Path

MD5_LINE = re.compile(r"^([0-9a-fA-F]{32})\s+\*?(.+?)\s*$")
SFV_LINE = re.compile(r"^(.+?)\s+([0-9a-fA-F]{8})\s*$")
CHUNK = 1024 * 1024


class Stopped(Exception):
    pass


def find_lists(folder: Path, depth: int = 3) -> list[Path]:
    """The checksum lists inside an installed game, nearest first."""
    found: list[Path] = []
    root = Path(folder)
    for current, dirs, files in os.walk(root):
        level = len(Path(current).relative_to(root).parts)
        if level >= depth:
            dirs[:] = []
        for name in files:
            if name.lower().endswith((".md5", ".sfv")):
                found.append(Path(current) / name)
    return sorted(found, key=lambda p: (len(p.parts), p.name.lower()))


def read_list(path: Path) -> list[tuple[str, str, str]]:
    """``(kind, checksum, relative path)`` for every line of a list."""
    try:
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return []
    entries = []
    kind = "sfv" if path.suffix.lower() == ".sfv" else "md5"
    for raw in text.splitlines():
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith((";", "#")):
            continue
        match = MD5_LINE.match(line) if kind == "md5" else SFV_LINE.match(line)
        if not match:
            continue
        if kind == "md5":
            entries.append(("md5", match.group(1).lower(), match.group(2)))
        else:
            entries.append(("sfv", match.group(2).lower(), match.group(1)))
    return entries


def _locate(listing: Path, relative: str, root: Path) -> Path | None:
    """Where a listed file is: beside the list, or above it inside the game."""
    relative = relative.replace("\\", "/").lstrip("/")
    for base in (listing.parent, listing.parent.parent, root):
        candidate = (base / relative)
        try:
            candidate.resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            continue                       # never look outside the game's folder
        if candidate.is_file():
            return candidate
    return None


def _checksum(path: Path, kind: str, stop) -> str:
    if kind == "md5":
        digest = hashlib.md5()
        with open(path, "rb") as handle:
            while True:
                if stop and stop():
                    raise Stopped()
                block = handle.read(CHUNK)
                if not block:
                    return digest.hexdigest()
                digest.update(block)
    crc = 0
    with open(path, "rb") as handle:
        while True:
            if stop and stop():
                raise Stopped()
            block = handle.read(CHUNK)
            if not block:
                return f"{crc & 0xFFFFFFFF:08x}"
            crc = zlib.crc32(block, crc)


def check(folder, progress=None, stop=None) -> dict:
    """Check every listed file. ``{"lists", "checked", "missing", "bad"}``."""
    root = Path(folder)
    lists = find_lists(root)
    entries = [(listing, *entry) for listing in lists for entry in read_list(listing)]
    missing, bad = [], []
    for number, (listing, kind, expected, relative) in enumerate(entries, 1):
        if progress:
            progress(number, len(entries), relative)
        found = _locate(listing, relative, root)
        if found is None:
            missing.append(relative)
            continue
        if _checksum(found, kind, stop) != expected:
            bad.append(relative)
    return {"lists": [str(p) for p in lists], "checked": len(entries),
            "missing": missing, "bad": bad}
