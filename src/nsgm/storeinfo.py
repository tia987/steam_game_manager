"""How much room a game needs, and whether it needs a solid state drive.

An installer does not say how big the game will be once installed, and
nothing on disk says whether it streams assets fast enough to need an
SSD. The game's Steam store entry usually says both, in its system
requirements:

    Storage: 150 GB available space
    Additional Notes: SSD required

That holds for games bought elsewhere too, since nearly every commercial
PC game has a Steam page. The lookup is by title, so it is matched with
care, and results are cached because they do not change.

How the SSD line is read, from real store pages:

* the *minimum* requirements decide. "SSD required", "50GB SATA SSD" or
  an SSD named as the storage means required
* softened wording - "SSD recommended", "SSD (Preferred)", "HDD
  supported" - means recommended, even if the recommended spec then
  insists on one, because the game still runs from a hard disk
* an SSD mentioned only in the recommended spec means recommended
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import sgdb

SEARCH = "https://store.steampowered.com/api/storesearch/?term={term}&l=english&cc=US"
DETAILS = "https://store.steampowered.com/api/appdetails?appids={app}&l=english"
USER_AGENT = "NonSteamGameManager/1.0 (+install planning)"

FOUND_DAYS = 30
MISSING_DAYS = 7

TAGS = re.compile(r"<[^>]+>")
SPACE = re.compile(r"\s+")

STORAGE = re.compile(
    r"(?i)\b(?:storage|hard\s*(?:disk|drive)(?:\s*space)?|disk\s*space|hdd|free\s*space|space)"
    r"\s*:\s*(?:at\s+least\s+)?(\d+(?:[.,]\d+)?)\s*(tb|gb|mb)\b"
)
SSD = re.compile(r"(?i)\bssd\b|solid[\s-]*state")
SOFT = re.compile(
    r"(?i)recommend|prefer|optional|suggest|ideal|advis|benefit|for best|"
    r"hdd\s*(?:is\s*)?(?:supported|ok|works|compatible)|\bor\s+hdd\b|\bhdd\s+or\b"
)
NEGATED = re.compile(r"(?i)not\s+required|no\s+ssd|doesn.t\s+require|isn.t\s+required")


def plain(markup: str) -> str:
    """Requirement markup as text, one requirement per line."""
    text = html.unescape(TAGS.sub("\n", str(markup or "")))
    lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def _size_in(text: str) -> int:
    match = STORAGE.search(text)
    if not match:
        return 0
    number = float(match.group(1).replace(",", "."))
    unit = match.group(2).lower()
    scale = {"mb": 1024 ** 2, "gb": 1024 ** 3, "tb": 1024 ** 4}[unit]
    return int(number * scale)


def _ssd_clauses(text: str) -> list[str]:
    # Clauses rather than a window of characters: "SSD required. 16 GB RAM
    # recommended" must not read the RAM's softening as the SSD's.
    return [c.strip() for c in re.split(r"[.;,\n|]+", text) if SSD.search(c)]


def parse_requirements(minimum: str, recommended: str = "") -> dict:
    """Storage size and SSD need from the two requirement blocks."""
    low = plain(minimum)
    high = plain(recommended)
    size = _size_in(low) or _size_in(high)

    need, evidence = "", ""
    clauses = _ssd_clauses(low)
    hard = [c for c in clauses if not SOFT.search(c) and not NEGATED.search(c)]
    soft = [c for c in clauses if SOFT.search(c) and not NEGATED.search(c)]
    if hard:
        need, evidence = "required", hard[0]
    elif soft:
        need, evidence = "recommended", soft[0]
    else:
        later = [c for c in _ssd_clauses(high) if not NEGATED.search(c)]
        if later:
            need, evidence = "recommended", later[0]
    return {"size": size, "ssd": need, "evidence": evidence[:140]}


# --------------------------------------------------------------------------
# finding the game
# --------------------------------------------------------------------------

NOISE = re.compile(
    r"(?ix)"
    r"\b(setup|installer|install|repack|fitgirl|dodi|elamigos|kaos|gog|steamrip|"
    r"portable|multi\d*|x64|x86|64bit|32bit|win(?:64|32)?|windows|pc|"
    r"build\s*\d+|update\s*\d+|patch\s*\d+|v\s?\d+(?:[._]\d+)*[a-z]?|"
    r"\d+(?:\.\d+){1,4}|final|proper|incl\w*|dlcs?|all\s+dlcs?|bonus\s+content)\b"
)
BRACKETED = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")


def search_title(name: str) -> str:
    """Strip release clutter from a download's name for a store search."""
    text = re.sub(r"\.(exe|msi|zip|7z|rar|iso)$", "", str(name or ""), flags=re.I)
    text = BRACKETED.sub(" ", text)
    # Underscores go first so version numbers stand alone, but dots stay
    # until the versions are gone, or "1.0.0.1" would survive as "1 0 0 1".
    text = text.replace("_", " ")
    text = re.sub(r"(?i)^setup\s+", "", text)
    text = NOISE.sub(" ", text)
    text = text.replace(".", " ")
    text = NOISE.sub(" ", text)
    text = re.sub(r"\s*[-–]\s*$", "", text)
    text = SPACE.sub(" ", text).strip(" -–")
    return text


EDITION_WORDS = {
    "ultimate", "gold", "complete", "deluxe", "goty", "definitive", "enhanced",
    "remastered", "directors", "cut", "anniversary", "collection", "bundle",
    "premium", "standard", "digital", "special", "legendary", "edition",
}


def match_score(term: str, candidate: str) -> float:
    """How surely a store title is the game being installed.

    Stricter than the cover matcher on purpose: a sequel shares most of
    its name with the original, and "Hades" quietly picking up the
    requirements of "Hades II" would be worse than finding nothing.
    """
    import difflib

    a, b = sgdb.normalise(term), sgdb.normalise(candidate)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # "Cyberpunk 2077 Ultimate Edition" is still Cyberpunk 2077.
    for longer, shorter in ((a, b), (b, a)):
        if longer.startswith(shorter + " "):
            extra = set(longer[len(shorter):].split())
            if extra and extra <= EDITION_WORDS:
                return 0.95
    return difflib.SequenceMatcher(None, a, b).ratio()


def _get(url: str, timeout: int = 12) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _cache_file(config_dir: Path) -> Path:
    return Path(config_dir) / "storeinfo.json"


def _load(config_dir: Path) -> dict:
    try:
        data = json.loads(_cache_file(config_dir).read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(config_dir: Path, cache: dict) -> None:
    try:
        path = _cache_file(config_dir)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache), "utf-8")
        tmp.replace(path)
    except OSError:
        pass


def lookup(name: str, config_dir: Path, timeout: int = 12) -> dict:
    """What the Steam store says this game needs. Never raises.

    ``{"found", "app", "name", "size", "ssd", "evidence"}``; ``found`` is
    False when there is no confident match or the store could not be
    reached, and the caller falls back to its own estimate.
    """
    term = search_title(name)
    key = sgdb.normalise(term)
    empty = {"found": False, "app": 0, "name": "", "size": 0, "ssd": "", "evidence": "",
             "term": term}
    if len(key) < 2:
        return empty

    cache = _load(config_dir)
    hit = cache.get(key)
    if hit:
        days = FOUND_DAYS if hit.get("found") else MISSING_DAYS
        if time.time() - hit.get("t", 0) < days * 86400:
            return {**empty, **hit}

    try:
        found = _get(SEARCH.format(term=urllib.parse.quote(term)), timeout)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return empty                    # offline: do not cache the miss

    best, best_score = None, 0.0
    for item in (found.get("items") or [])[:8]:
        if item.get("type") not in (None, "app"):
            continue
        score = match_score(term, str(item.get("name", "")))
        if score > best_score:
            best, best_score = item, score

    result = dict(empty)
    if best and best_score >= 0.9:
        try:
            details = _get(DETAILS.format(app=int(best["id"])), timeout)
            data = (details.get(str(best["id"])) or {}).get("data") or {}
        except (urllib.error.URLError, TimeoutError, ValueError, OSError, KeyError):
            return empty
        requirements = data.get("pc_requirements") or {}
        if isinstance(requirements, list):     # the API sends [] for "none"
            requirements = {}
        parsed = parse_requirements(requirements.get("minimum", ""),
                                    requirements.get("recommended", ""))
        result.update({"found": True, "app": int(best["id"]),
                       "name": str(data.get("name") or best.get("name", "")), **parsed})

    cache[key] = {**{k: v for k, v in result.items() if k != "term"}, "t": time.time()}
    _save(config_dir, cache)
    return result
