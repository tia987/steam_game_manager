"""A catalogue of genuinely free and open-source games.

Every entry links to a public source repository under a recognised
free-software or free-culture licence, and downloads come from that
project's own release page. Nothing here is obscure or hard to find:
that is rather the point of open source.

Resolving a download means asking GitHub for a project's latest release
and picking the right file for this computer. GitHub allows only 60
unauthenticated API calls an hour per address, which a catalogue this
size would exhaust in one sweep, so:

* results are cached on disk for a day
* resolution happens for one game at a time, when it is asked for
* the remaining allowance is read from the response and reported
* a personal access token, if supplied, raises the ceiling to 5000

Some entries are engine re-implementations. They are open source, but
they replace only the program: you still need the original game's data
files, which are not free. Those carry a ``needs_data`` note and say so
plainly rather than leaving someone to discover it after downloading.
"""

from __future__ import annotations

import json
import platform
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

USER_AGENT = "NonSteamGameManager/1.0 (+open source catalogue)"
API = "https://api.github.com"
CACHE_HOURS = 24


class CatalogError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# the catalogue
# --------------------------------------------------------------------------

GAMES: list[dict] = [
    {"name": "0 A.D.", "repo": "0ad/0ad", "licence": "GPL-2.0",
     "site": "https://play0ad.com", "tags": ["Strategy"],
     "blurb": "Historical real-time strategy, in the Age of Empires mould."},
    {"name": "SuperTuxKart", "repo": "supertuxkart/stk-code", "licence": "GPL-3.0",
     "site": "https://supertuxkart.net", "tags": ["Racing"],
     "blurb": "Kart racing with power-ups, split screen and online play."},
    {"name": "OpenTTD", "repo": "OpenTTD/OpenTTD", "licence": "GPL-2.0",
     "site": "https://www.openttd.org", "tags": ["Simulation", "Strategy"],
     "blurb": "Build and run a transport empire. Decades of active development."},
    {"name": "Endless Sky", "repo": "endless-sky/endless-sky", "licence": "GPL-3.0",
     "site": "https://endless-sky.github.io", "tags": ["Space", "Trading"],
     "blurb": "Trade, fight and explore a large galaxy at your own pace."},
    {"name": "Mindustry", "repo": "Anuken/Mindustry", "licence": "GPL-3.0",
     "site": "https://mindustrygame.github.io", "tags": ["Strategy", "Factory"],
     "note": "Distributed as a .jar; needs Java installed.",
     "blurb": "Factory building crossed with tower defence."},
    {"name": "Cataclysm: Dark Days Ahead", "repo": "CleverRaven/Cataclysm-DDA",
     "licence": "CC-BY-SA-3.0", "site": "https://cataclysmdda.org",
     "tags": ["Roguelike", "Survival"],
     "blurb": "Enormously deep post-apocalyptic survival roguelike."},
    {"name": "The Battle for Wesnoth", "repo": "wesnoth/wesnoth", "licence": "GPL-2.0",
     "site": "https://www.wesnoth.org", "tags": ["Strategy"],
     "blurb": "Turn-based fantasy strategy with a huge library of campaigns."},
    {"name": "Widelands", "repo": "widelands/widelands", "licence": "GPL-2.0",
     "site": "https://www.widelands.org", "tags": ["Strategy"],
     "blurb": "Settlers-style economy and expansion strategy."},
    {"name": "OpenRA", "repo": "OpenRA/OpenRA", "licence": "GPL-3.0",
     "site": "https://www.openra.net", "tags": ["Strategy"],
     "blurb": "Modernised Command & Conquer engine. Downloads the freeware assets itself."},
    {"name": "Shattered Pixel Dungeon", "repo": "00-Evan/shattered-pixel-dungeon",
     "licence": "GPL-3.0", "site": "https://shatteredpixel.com",
     "tags": ["Roguelike"], "blurb": "Polished, endlessly replayable dungeon crawler."},
    {"name": "Luanti", "repo": "luanti-org/luanti", "licence": "LGPL-2.1",
     "site": "https://www.luanti.org", "tags": ["Sandbox"],
     "blurb": "Voxel sandbox engine, formerly Minetest, with thousands of mods."},
    {"name": "SuperTux", "repo": "SuperTux/supertux", "licence": "GPL-3.0",
     "site": "https://www.supertux.org", "tags": ["Platformer"],
     "blurb": "Classic side-scrolling platformer."},
    {"name": "HyperRogue", "repo": "zenorogue/hyperrogue", "licence": "GPL-2.0",
     "site": "https://roguetemple.com/z/hyper", "tags": ["Roguelike", "Puzzle"],
     "blurb": "A roguelike set in hyperbolic geometry. Genuinely unlike anything else."},
    {"name": "Freeciv", "repo": "freeciv/freeciv", "licence": "GPL-2.0",
     "site": "https://www.freeciv.org", "tags": ["Strategy"],
     "blurb": "Turn-based empire building along Civilization lines."},
    {"name": "Unciv", "repo": "yairm210/Unciv", "licence": "MPL-2.0",
     "site": "https://github.com/yairm210/Unciv", "tags": ["Strategy"],
     "blurb": "A light, fast reimplementation of Civilization V's design."},
    {"name": "DDNet", "repo": "ddnet/ddnet", "licence": "Custom (free)",
     "site": "https://ddnet.org", "tags": ["Platformer", "Multiplayer"],
     "blurb": "Cooperative multiplayer platformer built on Teeworlds."},
    {"name": "Teeworlds", "repo": "teeworlds/teeworlds", "licence": "Zlib",
     "site": "https://www.teeworlds.com", "tags": ["Shooter", "Multiplayer"],
     "blurb": "Fast 2D multiplayer shooter with a grappling hook."},
    {"name": "Naev", "repo": "naev/naev", "licence": "GPL-3.0",
     "site": "https://naev.org", "tags": ["Space", "Trading"],
     "blurb": "Open-ended space trading and combat, inspired by Escape Velocity."},
    {"name": "Unvanquished", "repo": "Unvanquished/Unvanquished", "licence": "GPL-3.0",
     "site": "https://unvanquished.net", "tags": ["Shooter", "Strategy"],
     "blurb": "Team shooter mixed with real-time strategy, aliens against humans."},
    {"name": "Xonotic", "repo": "", "licence": "GPL-3.0",
     "site": "https://xonotic.org", "tags": ["Shooter"],
     "blurb": "Very fast arena shooter. Distributed from the project's own site."},
    {"name": "OpenSpades", "repo": "yvt/openspades", "licence": "GPL-3.0",
     "site": "https://openspades.yvt.jp", "tags": ["Shooter"],
     "blurb": "Voxel shooter client, in the Ace of Spades style."},
    {"name": "Sonic Robo Blast 2", "repo": "STJr/SRB2", "licence": "GPL-2.0",
     "site": "https://www.srb2.org", "tags": ["Platformer"],
     "blurb": "3D fan platformer built on a heavily modified Doom engine."},
    {"name": "The Powder Toy", "repo": "The-Powder-Toy/The-Powder-Toy", "licence": "GPL-3.0",
     "site": "https://powdertoy.co.uk", "tags": ["Sandbox", "Simulation"],
     "blurb": "Physics sandbox for melting, freezing and exploding things."},
    {"name": "Veloren", "repo": "veloren/veloren", "licence": "GPL-3.0",
     "site": "https://veloren.net", "tags": ["RPG", "Sandbox"],
     "blurb": "Multiplayer voxel RPG. Development lives on GitLab; downloads on the site."},
    {"name": "Thrive", "repo": "Revolutionary-Games/Thrive", "licence": "GPL-3.0",
     "site": "https://revolutionarygamesstudio.com", "tags": ["Simulation"],
     "blurb": "Evolution simulator, from single cell upward."},
    {"name": "Pioneer", "repo": "pioneerspacesim/pioneer", "licence": "GPL-3.0",
     "site": "https://pioneerspacesim.net", "tags": ["Space"],
     "blurb": "Space adventure across a procedurally generated Milky Way."},
    {"name": "Terasology", "repo": "MovingBlocks/Terasology", "licence": "Apache-2.0",
     "site": "https://terasology.org", "tags": ["Sandbox"],
     "blurb": "Voxel world engine with a strong modular architecture."},
    {"name": "Rigs of Rods", "repo": "RigsOfRods/rigs-of-rods", "licence": "GPL-3.0",
     "site": "https://www.rigsofrods.org", "tags": ["Simulation"],
     "blurb": "Soft-body physics vehicle simulator."},
    {"name": "Space Station 14", "repo": "", "licence": "MIT",
     "site": "https://spacestation14.io", "tags": ["Multiplayer", "Simulation"],
     "blurb": "Chaotic multiplayer space station roleplay. Installed through the "
              "project's own launcher; its GitHub releases are server builds."},
    {"name": "Cortex Command Community Project",
     "repo": "cortex-command-community/Cortex-Command-Community-Project",
     "licence": "AGPL-3.0", "site": "https://cortex-command-community.github.io",
     "tags": ["Action"], "blurb": "Pixel-physics action game, open sourced by its creators."},
    {"name": "FreeCol", "repo": "FreeCol/freecol", "licence": "GPL-2.0",
     "site": "https://www.freecol.org", "tags": ["Strategy"],
     "blurb": "Turn-based colonisation strategy in the Colonization tradition."},

    # Engine re-implementations. Free software, but they need data files
    # from a game you have to own separately.
    {"name": "OpenMW", "repo": "OpenMW/openmw", "licence": "GPL-3.0",
     "site": "https://openmw.org", "tags": ["RPG", "Engine"],
     "needs_data": "You need your own copy of Morrowind for its data files.",
     "blurb": "A modern, open engine for Morrowind."},
    {"name": "OpenRCT2", "repo": "OpenRCT2/OpenRCT2", "licence": "GPL-3.0",
     "site": "https://openrct2.io", "tags": ["Simulation", "Engine"],
     "needs_data": "You need your own copy of RollerCoaster Tycoon 2.",
     "blurb": "RollerCoaster Tycoon 2, rebuilt and greatly extended."},
    {"name": "Julius", "repo": "bvschaik/julius", "licence": "AGPL-3.0",
     "site": "https://github.com/bvschaik/julius", "tags": ["Strategy", "Engine"],
     "needs_data": "You need your own copy of Caesar III.",
     "blurb": "Faithful Caesar III engine with modern resolutions."},
    {"name": "Augustus", "repo": "Keriew/augustus", "licence": "AGPL-3.0",
     "site": "https://github.com/Keriew/augustus", "tags": ["Strategy", "Engine"],
     "needs_data": "You need your own copy of Caesar III.",
     "blurb": "Caesar III engine with gameplay changes and quality-of-life work."},
    {"name": "CorsixTH", "repo": "CorsixTH/CorsixTH", "licence": "MIT",
     "site": "https://corsixth.com", "tags": ["Simulation", "Engine"],
     "needs_data": "You need your own copy of Theme Hospital.",
     "blurb": "Theme Hospital reimplementation."},
    {"name": "DevilutionX", "repo": "diasurgical/DevilutionX", "licence": "Unlicense",
     "site": "https://github.com/diasurgical/devilutionX", "tags": ["RPG", "Engine"],
     "needs_data": "You need MPQ data from your own copy of Diablo.",
     "blurb": "Diablo engine rebuilt for modern systems."},
    {"name": "OpenLoco", "repo": "OpenLoco/OpenLoco", "licence": "MIT",
     "site": "https://github.com/OpenLoco/OpenLoco", "tags": ["Simulation", "Engine"],
     "needs_data": "You need your own copy of Chris Sawyer's Locomotion.",
     "blurb": "Open reimplementation of Locomotion."},
]


def catalogue(tag: str = "") -> list[dict]:
    games = [dict(g) for g in GAMES]
    if tag:
        games = [g for g in games if tag in (g.get("tags") or [])]
    return sorted(games, key=lambda g: g["name"].lower())


def all_tags() -> list[str]:
    tags: set[str] = set()
    for game in GAMES:
        tags.update(game.get("tags") or [])
    return sorted(tags)


# --------------------------------------------------------------------------
# picking the right file
# --------------------------------------------------------------------------

NOT_A_PACKAGE = re.compile(
    r"(?i)\.(json|txt|md|sha\d*|sha\d*sum|asc|sig|sigstore|pem|blockmap|yml|yaml|"
    r"deb|rpm|snap|flatpak|apk|aab|xz\.sig)$"
)
SOURCE_LIKE = re.compile(r"(?i)(source|-src|_src|symbols|debug|dbg|pdb|sdk|headers)")
SERVER_LIKE = re.compile(r"(?i)(server|dedicated|headless)")

PLATFORM_HINTS = {
    "Windows": (re.compile(r"(?i)(windows|win64|win32|-win|_win|\bwin\b|\.msi$|\.exe$)"),
                re.compile(r"(?i)(linux|ubuntu|debian|macos|osx|darwin|android|\.dmg$|appimage)")),
    "Linux": (re.compile(r"(?i)(linux|appimage|ubuntu|debian|\.tar\.(gz|xz|bz2)$)"),
              re.compile(r"(?i)(windows|win64|win32|macos|osx|darwin|android|\.exe$|\.msi$|\.dmg$)")),
    "Darwin": (re.compile(r"(?i)(macos|osx|darwin|\.dmg$)"),
               re.compile(r"(?i)(windows|win64|win32|linux|android|\.exe$|\.msi$|appimage)")),
}

ARCH_64 = re.compile(r"(?i)(x86[_-]?64|x64|amd64|64bit|win64)")
ARCH_ARM = re.compile(r"(?i)(arm64|aarch64|armhf|armv7|armeabi)")
ARCH_32 = re.compile(r"(?i)(i386|i686|win32|x86(?![_-]?64)|32bit)")


def host_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "arm"
    if machine in ("i386", "i686", "x86"):
        return "x86"
    return "x64"


def score_asset(name: str, system: str) -> int:
    """Rank one release file for this computer. Negative means unusable."""
    if NOT_A_PACKAGE.search(name):
        return -100
    wanted, unwanted = PLATFORM_HINTS.get(system, PLATFORM_HINTS["Windows"])
    if unwanted.search(name):
        return -100

    score = 0
    if wanted.search(name):
        score += 60
    else:
        # A bare .zip or .jar may still be the cross-platform build.
        if not re.search(r"(?i)\.(zip|7z|jar|tar\.gz)$", name):
            return -100
        score += 5

    if SOURCE_LIKE.search(name):
        return -100
    if SERVER_LIKE.search(name):
        # A dedicated server is never what someone browsing games wants,
        # and offering one as "the game" is worse than offering nothing.
        return -100

    arch = host_arch()
    is_arm, is_32 = bool(ARCH_ARM.search(name)), bool(ARCH_32.search(name))
    if arch == "x64":
        if is_arm:
            return -100          # will not run here at all
        if is_32:
            score -= 30          # runs, but the 64-bit build is better
        if ARCH_64.search(name):
            score += 25
    elif arch == "arm":
        if is_arm:
            score += 40
        elif is_32:
            score -= 30
    else:
        if is_arm:
            return -100
        if is_32:
            score += 25

    lowered = name.lower()
    if lowered.endswith((".exe", ".msi")):
        score += 20        # an installer is what most people want
    elif lowered.endswith(".zip"):
        score += 12        # portable, still fine
    elif lowered.endswith(".7z"):
        score += 6
    elif lowered.endswith(".jar"):
        score += 4
    return score


def choose_asset(assets: list[dict], system: str = "") -> dict | None:
    system = system or platform.system()
    ranked = []
    for asset in assets:
        name = asset.get("name", "")
        value = score_asset(name, system)
        if value > 0:
            ranked.append((value, asset))
    if not ranked:
        return None
    ranked.sort(key=lambda pair: -pair[0])
    best = ranked[0][1]
    return {
        "name": best.get("name", ""),
        "url": best.get("browser_download_url", ""),
        "size": best.get("size", 0),
        "score": ranked[0][0],
    }


# --------------------------------------------------------------------------
# talking to GitHub
# --------------------------------------------------------------------------

class RateLimit(CatalogError):
    """GitHub's hourly allowance is spent."""


def _request(url: str, token: str = "", timeout: int = 20) -> tuple[dict, int]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
            remaining = int(response.headers.get("X-RateLimit-Remaining", "-1"))
            return payload, remaining
    except urllib.error.HTTPError as exc:
        remaining = int(exc.headers.get("X-RateLimit-Remaining", "-1") or -1)
        if exc.code in (403, 429) and remaining == 0:
            reset = exc.headers.get("X-RateLimit-Reset", "")
            when = ""
            if reset.isdigit():
                minutes = max(0, int((int(reset) - time.time()) / 60))
                when = f" It resets in about {minutes} minute{'s' if minutes != 1 else ''}."
            raise RateLimit(
                "GitHub's hourly limit for anonymous requests is used up." + when +
                " Adding a GitHub token in Settings raises it from 60 to 5000 an hour."
            ) from exc
        if exc.code == 404:
            raise CatalogError("That project has no published release on GitHub.") from exc
        raise CatalogError(f"GitHub answered HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise CatalogError(f"Could not reach GitHub ({exc}).") from exc


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------

def _cache_path(config_dir: Path) -> Path:
    return config_dir / "catalog-cache.json"


def load_cache(config_dir: Path) -> dict:
    path = _cache_path(config_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(config_dir: Path, cache: dict) -> None:
    path = _cache_path(config_dir)
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache), "utf-8")
        tmp.replace(path)
    except OSError:
        pass


def resolve(game: dict, config_dir: Path, token: str = "",
            system: str = "", force: bool = False) -> dict:
    """Find the current download for one game, using the cache when it can."""
    repo = (game.get("repo") or "").strip()
    if not repo:
        return {"status": "site", "site": game.get("site", ""),
                "message": "This project publishes downloads on its own site."}

    cache = load_cache(config_dir)
    entry = cache.get(repo)
    fresh = entry and (time.time() - entry.get("fetched", 0)) < CACHE_HOURS * 3600
    if entry and fresh and not force:
        return {**entry["data"], "cached": True}

    payload, remaining = _request(f"{API}/repos/{repo}/releases/latest", token)
    assets = payload.get("assets") or []
    best = choose_asset(assets, system)

    data = {
        "status": "ok" if best else "no-asset",
        "version": payload.get("tag_name") or payload.get("name") or "",
        "published": (payload.get("published_at") or "")[:10],
        "release_page": payload.get("html_url", ""),
        "asset": best,
        "asset_count": len(assets),
        "remaining": remaining,
        "site": game.get("site", ""),
        "message": "" if best else
                   "No file in the latest release matches this computer. "
                   "The release page lists everything that is there.",
    }
    cache[repo] = {"fetched": time.time(), "data": data}
    save_cache(config_dir, cache)
    return {**data, "cached": False}
