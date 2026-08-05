"""Notice when a newer release exists, and fetch its installer.

The app does not overwrite itself. A running program cannot replace its
own file on Windows, and the workarounds - a helper that waits for the
process to die, then swaps the file and restarts it - fail in exactly the
situations where failing is worst. Downloading the installer and letting
it do the job is what it is for, and it keeps the uninstall entry and
shortcuts correct on the way through.
"""

from __future__ import annotations

import json
import platform
import re
import urllib.error
import urllib.parse
import urllib.request

REPO = "tia987/steam_game_manager"
API = "https://api.github.com/repos/{repo}/releases/latest"
USER_AGENT = "NonSteamGameManager (+update check)"


class UpdateError(RuntimeError):
    pass


def parse_version(text: str) -> tuple:
    """Turn 'v1.2.3' into (1, 2, 3) for comparison. Unknown parts sort low."""
    numbers = re.findall(r"\d+", str(text or ""))
    return tuple(int(n) for n in numbers[:4]) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def _asset_for_this_system(assets: list) -> dict | None:
    """Pick the download that suits the machine asking."""
    system = platform.system()
    wanted = {
        "Windows": ("setup.exe", "windows.exe", "windows"),
        "Darwin": ("macos", "darwin"),
        "Linux": ("linux",),
    }.get(system, ())
    if not wanted:
        return None

    # An installer is preferred over a bare binary: it puts things where
    # they belong and leaves an uninstall entry behind.
    ranked = []
    for asset in assets:
        name = str(asset.get("name", "")).lower()
        if not any(hint in name for hint in wanted):
            continue
        score = 20 if "setup" in name or "install" in name else 10
        ranked.append((score, asset))
    if not ranked:
        return None
    ranked.sort(key=lambda pair: -pair[0])
    return ranked[0][1]


def check(current_version: str, repo: str = REPO, timeout: int = 15) -> dict:
    """Ask GitHub what the newest release is.

    Never raises for the ordinary failures - no network, rate limited, no
    releases yet - because a background check that interrupts with an
    error box is worse than one that stays quiet.
    """
    request = urllib.request.Request(
        API.format(repo=repo),
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"checked": True, "available": False,
                    "note": "That repository has published no releases yet."}
        if exc.code in (403, 429):
            return {"checked": True, "available": False,
                    "note": "GitHub's hourly limit is spent; try again later."}
        return {"checked": False, "available": False,
                "note": f"GitHub answered HTTP {exc.code}."}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"checked": False, "available": False,
                "note": f"Could not reach GitHub ({exc})."}

    tag = str(data.get("tag_name", ""))
    newer = is_newer(tag, current_version)
    asset = _asset_for_this_system(data.get("assets") or []) if newer else None
    return {
        "checked": True,
        "available": newer,
        "version": tag,
        "current": current_version,
        "published": str(data.get("published_at", ""))[:10],
        "notes": str(data.get("body", ""))[:2000],
        "page": data.get("html_url", ""),
        "download": (asset or {}).get("browser_download_url", ""),
        "filename": (asset or {}).get("name", ""),
        "size": (asset or {}).get("size", 0),
        "note": "" if not newer or asset else
                "A newer version exists but has no download for this system.",
    }
