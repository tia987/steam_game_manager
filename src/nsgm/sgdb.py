"""SteamGridDB client: find a game, pick artwork, save it where Steam looks.

Steam reads shortcut artwork from ``userdata/<id3>/config/grid`` using the
32 bit shortcut id as the file name:

    <id>p.<ext>       portrait capsule, 600x900  - the library cover
    <id>.<ext>        wide capsule, 920x430      - shown in some views
    <id>_hero.<ext>   hero banner, 1920x620      - top of the game page
    <id>_logo.<ext>   transparent logo
    <id>_icon.<ext>   small icon, also referenced by the shortcut record

An API key is required and is free: https://www.steamgriddb.com/profile/preferences
"""

from __future__ import annotations

import difflib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_ROOT = "https://www.steamgriddb.com/api/v2"
USER_AGENT = "NonSteam-Game-Manager/1.0 (+local tool)"

# kind -> (endpoint, default dimensions, file suffix)
KINDS: dict[str, tuple[str, str, str]] = {
    "capsule": ("grids", "600x900", "p"),
    "wide": ("grids", "920x430,460x215", ""),
    "hero": ("heroes", "1920x620,3840x1240", "_hero"),
    "logo": ("logos", "", "_logo"),
    "icon": ("icons", "", "_icon"),
}

ART_ORDER = ["capsule", "wide", "hero", "logo", "icon"]


class SGDBError(RuntimeError):
    pass


def normalise(title: str) -> str:
    text = title.lower()
    text = re.sub(r"[™®©]", "", text)
    text = re.sub(r"\b(the|a|an)\b", " ", text)
    text = re.sub(r"\b(goty|game of the year|definitive|remastered|edition|"
                  r"deluxe|complete|enhanced|hd|directors cut)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def similarity(a: str, b: str) -> float:
    na, nb = normalise(a), normalise(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    score = difflib.SequenceMatcher(None, na, nb).ratio()
    if nb.startswith(na) or na.startswith(nb):
        score = max(score, 0.9)
    return score


class SteamGridDB:
    """Thin API wrapper. Caches within a session and throttles requests."""

    def __init__(self, api_key: str, min_interval: float = 0.12):
        self.api_key = (api_key or "").strip()
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._min_interval = min_interval
        self._cache: dict[str, object] = {}

    # -- plumbing ---------------------------------------------------------

    def _throttle(self) -> None:
        with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _get(self, path: str, params: dict | None = None, retries: int = 3):
        if not self.api_key:
            raise SGDBError("Add your SteamGridDB API key before fetching artwork.")
        url = f"{API_ROOT}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v not in (None, "")}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        if url in self._cache:
            return self._cache[url]

        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )
        for attempt in range(retries):
            self._throttle()
            try:
                with urllib.request.urlopen(request, timeout=25) as response:
                    payload = json.loads(response.read().decode("utf-8", "replace"))
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    raise SGDBError("SteamGridDB rejected the API key.") from exc
                if exc.code == 404:
                    return {"success": True, "data": []}
                if exc.code == 429 and attempt < retries - 1:
                    time.sleep(float(exc.headers.get("Retry-After", 2)) + attempt)
                    continue
                if attempt == retries - 1:
                    raise SGDBError(f"SteamGridDB returned HTTP {exc.code}.") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt == retries - 1:
                    raise SGDBError(f"Could not reach SteamGridDB: {exc}") from exc
                time.sleep(1 + attempt)
        else:
            raise SGDBError("Could not reach SteamGridDB.")

        if not payload.get("success", False):
            raise SGDBError(str(payload.get("errors") or "SteamGridDB request failed."))
        self._cache[url] = payload.get("data")
        return payload.get("data")

    # -- api --------------------------------------------------------------

    def search(self, term: str) -> list[dict]:
        term = term.strip()
        if not term:
            return []
        data = self._get(f"/search/autocomplete/{urllib.parse.quote(term)}") or []
        results = []
        for item in data:
            results.append(
                {
                    "id": item.get("id"),
                    "name": item.get("name", ""),
                    "verified": bool(item.get("verified")),
                    "release_date": item.get("release_date"),
                    "score": round(similarity(term, item.get("name", "")), 3),
                }
            )
        results.sort(key=lambda r: (-r["score"], not r["verified"]))
        return results

    def match(self, title: str, threshold: float = 0.62) -> dict | None:
        """Best automatic guess for a title, or None if nothing is close."""
        candidates = self.search(title)
        if not candidates:
            # Retry once without subtitle noise, which trips up autocomplete
            trimmed = re.split(r"\s[-–:]\s", title)[0]
            if trimmed != title and len(trimmed) > 3:
                candidates = self.search(trimmed)
        if not candidates:
            return None
        best = candidates[0]
        return best if best["score"] >= threshold else None

    def artwork(self, game_id: int, kind: str, nsfw: bool = False,
                animated: bool = False, limit: int = 30) -> list[dict]:
        endpoint, dimensions, _ = KINDS[kind]
        data = self._get(
            f"/{endpoint}/game/{game_id}",
            {
                "dimensions": dimensions or None,
                "types": "static,animated" if animated else "static",
                "nsfw": "false" if not nsfw else "any",
                "humor": "any",
                "limit": limit,
            },
        ) or []
        out = []
        for item in data:
            out.append(
                {
                    "id": item.get("id"),
                    "url": item.get("url"),
                    "thumb": item.get("thumb") or item.get("url"),
                    "width": item.get("width"),
                    "height": item.get("height"),
                    "style": item.get("style"),
                    "score": item.get("score", 0),
                    "author": (item.get("author") or {}).get("name", ""),
                }
            )
        return out

    def best_artwork(self, game_id: int, kinds: list[str], **options) -> dict[str, dict]:
        """Top rated art of each requested kind. Missing kinds are omitted."""
        picked: dict[str, dict] = {}
        for kind in kinds:
            try:
                items = self.artwork(game_id, kind, **options)
            except SGDBError:
                continue
            if items:
                picked[kind] = items[0]
        return picked


# --------------------------------------------------------------------------
# writing art into the Steam grid folder
# --------------------------------------------------------------------------

def _extension(url: str) -> str:
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return suffix if suffix in {".png", ".jpg", ".jpeg", ".webp", ".webm", ".ico"} else ".png"


IMAGE_SIGNATURES = (
    b"\x89PNG\r\n\x1a\n",   # png
    b"\xff\xd8\xff",        # jpeg
    b"GIF87a", b"GIF89a",   # gif
    b"\x00\x00\x01\x00",    # ico
    b"\x1a\x45\xdf\xa3",    # webm
)


def looks_like_image(blob: bytes) -> bool:
    """A signature check catches HTML error pages that a size check would pass."""
    if blob.startswith(b"RIFF") and blob[8:12] == b"WEBP":
        return True
    return any(blob.startswith(signature) for signature in IMAGE_SIGNATURES)


def download_art(url: str, grid_dir: Path, short_id: int, kind: str) -> Path:
    """Fetch one image and store it under the name Steam expects."""
    grid_dir.mkdir(parents=True, exist_ok=True)
    suffix = KINDS[kind][2]
    base = f"{short_id}{suffix}"

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=45) as response:
        blob = response.read()
    if not looks_like_image(blob):
        raise SGDBError(f"That download was not an image ({url}).")

    target = grid_dir / f"{base}{_extension(url)}"
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(blob)
    tmp.replace(target)

    # Steam picks whichever extension it finds first, so clear the others.
    for stale in grid_dir.glob(f"{base}.*"):
        if stale != target and stale.stem == base:
            try:
                stale.unlink()
            except OSError:
                pass
    return target


def existing_art(grid_dir: Path, short_id: int) -> dict[str, str]:
    """Which artwork kinds are already on disk for this shortcut."""
    found: dict[str, str] = {}
    if not grid_dir.is_dir():
        return found
    for kind in ART_ORDER:
        suffix = KINDS[kind][2]
        for candidate in grid_dir.glob(f"{short_id}{suffix}.*"):
            if candidate.stem == f"{short_id}{suffix}":
                found[kind] = str(candidate)
                break
    return found
