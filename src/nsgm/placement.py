"""Decide which library folder a game goes into.

Library folders are kept in priority order. A game goes into the first
one with room for it, after leaving that folder's reserve free, so a big
slow disk can come first and a small fast one catch whatever does not
fit.

A game that needs a solid state drive is the exception, because it has a
reason to skip the order. What happens then is a setting:

``strict`` (the default)
    Only solid state folders are considered. If none has room the game
    waits, with the reason shown, rather than going onto a hard disk it
    will stutter on.
``prefer``
    Solid state folders are tried first, then the rest. Landing on a hard
    disk is allowed but flagged.
``ignore``
    The requirement is noted and the normal order applies.

A game that only *recommends* one follows the normal order unless
``prefer_recommended`` is on.
"""

from __future__ import annotations

import re

from . import drives

GB = 1024 ** 3

POLICIES = ("strict", "prefer", "ignore")


def human(size: float) -> str:
    """Bytes as a short readable figure."""
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit in ("B", "KB"):
                return f"{size:.0f} {unit}"
            return f"{size:.1f} {unit}" if size < 10 else f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.0f} TB"


KIND_LABEL = {"ssd": "SSD", "hdd": "HDD", "unknown": "drive type unknown"}

RESERVED_NAMES = re.compile(r"(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])$")


def safe_folder_name(title: str) -> str:
    """A folder name Windows, Linux and macOS will all accept."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(title or ""))
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "Game"
    if RESERVED_NAMES.match(name):
        name += " game"
    return name[:120]


def describe_all(folders: list) -> list[dict]:
    out = []
    for index, folder in enumerate(folders or []):
        if isinstance(folder, str):
            folder = {"path": folder}
        if not isinstance(folder, dict) or not folder.get("path"):
            continue
        described = drives.describe(folder)
        described["priority"] = index + 1
        out.append(described)
    return out


def _label(target: dict) -> str:
    return f"{target['path']} ({KIND_LABEL.get(target['kind'], target['kind'])})"


def choose(size: int, ssd_need: str, targets: list[dict], policy: str = "strict",
           prefer_recommended: bool = False, committed: dict | None = None) -> dict:
    """Pick a target for a game of ``size`` bytes.

    ``targets`` comes from :func:`describe_all`. ``committed`` maps a
    volume to bytes already promised to installs still in progress, so two
    queued games are not both promised the same free space.

    Returns ``{"target": dict | None, "notes": [...], "warning": str}``.
    Notes explain every folder that was passed over, so the choice can be
    read rather than trusted.
    """
    policy = policy if policy in POLICIES else "strict"
    committed = committed or {}
    notes: list[str] = []
    usable = []
    for target in targets:
        if not target.get("enabled", True):
            continue
        if not target.get("connected"):
            notes.append(f"{target['path']}: that drive is not connected.")
            continue
        usable.append(target)

    if not usable:
        return {"target": None, "warning": "",
                "notes": notes or ["No library folders are set up yet."]}

    ssds = [t for t in usable if t["kind"] == "ssd"]
    others = [t for t in usable if t["kind"] != "ssd"]
    warning = ""

    if ssd_need == "required" and policy == "strict":
        order = ssds
        for target in others:
            why = ("drive type unknown; mark it as SSD or HDD if it is solid state"
                   if target["kind"] == "unknown" else "this game needs an SSD")
            notes.append(f"{_label(target)}: skipped, {why}.")
    elif ssd_need == "required" and policy == "prefer":
        order = ssds + others
    elif ssd_need == "recommended" and prefer_recommended:
        order = ssds + others
    else:
        order = usable

    for target in order:
        reserve = int(target.get("reserve_gb", 0) * GB)
        free = int(target.get("free", 0)) - int(committed.get(target.get("volume", ""), 0))
        if size <= 0:
            # No size known at all. Still refuse a disk already into its
            # reserve, which is the one thing that can be checked.
            if free > reserve:
                if ssd_need == "required" and target["kind"] != "ssd" and policy == "prefer":
                    warning = "Installed on a hard disk although the game needs an SSD."
                notes.append(f"{_label(target)}: chosen. The game's size is not known, "
                             f"so only the reserve was checked ({human(free)} free).")
                return {"target": target, "notes": notes, "warning": warning}
            notes.append(f"{_label(target)}: skipped, only {human(max(free, 0))} free "
                         f"and {human(reserve)} is kept in reserve.")
            continue
        if free - size >= reserve:
            if ssd_need == "required" and target["kind"] != "ssd" and policy == "prefer":
                warning = ("No SSD had room, so this goes on a hard disk although the game "
                           "needs an SSD. Expect long loads or stutter.")
            notes.append(f"{_label(target)}: chosen, {human(free)} free for about "
                         f"{human(size)}.")
            return {"target": target, "notes": notes, "warning": warning}
        notes.append(f"{_label(target)}: skipped, {human(max(free, 0))} free but it needs "
                     f"about {human(size)} plus {human(reserve)} kept free.")

    if ssd_need == "required" and policy == "strict" and not ssds:
        notes.append("This game needs an SSD and none of your library folders is on one.")
    return {"target": None, "notes": notes, "warning": ""}
