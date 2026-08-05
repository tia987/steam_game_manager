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
import sys
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
        if name.endswith((".dmg", ".txt", ".sha256")):
            continue
        if not any(hint in name for hint in wanted):
            continue
        score = 20 if "setup" in name or "install" in name else 10
        ranked.append((score, asset))
    if not ranked:
        return None
    ranked.sort(key=lambda pair: -pair[0])
    return ranked[0][1]


def _plain_binary(assets: list) -> dict | None:
    """The bare executable for this system, for replacing in place."""
    system = platform.system()
    hint = {"Windows": "windows.exe", "Darwin": "macos", "Linux": "linux"}.get(system)
    if not hint:
        return None
    for asset in assets:
        name = str(asset.get("name", "")).lower()
        if name.endswith((".dmg", ".txt", ".sha256")) or "setup" in name:
            continue
        if hint in name:
            return asset
    return None


# --------------------------------------------------------------------------
# replacing the running program with a newer one
# --------------------------------------------------------------------------

SIGNATURES = (b"MZ", b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xcf")
OLD_SUFFIX = ".old-version"


def running_binary() -> "Path | None":
    """The file to replace, or None when running from source."""
    from pathlib import Path

    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve()


def can_replace_itself() -> tuple:
    """Whether an in-place update is possible, and why not if it is not."""
    from pathlib import Path

    target = running_binary()
    if target is None:
        return False, "Running from source; update with git instead."
    if not target.exists():
        return False, "Cannot find the running program on disk."
    folder = target.parent
    probe = folder / ".write-test"
    try:
        probe.write_bytes(b"x")
        probe.unlink()
    except OSError:
        return False, f"No permission to write to {folder}."
    return True, ""


def cleanup_previous() -> None:
    """Remove the copy left behind by the last update.

    The old executable cannot be deleted while it is the one running, so
    it is renamed aside during the update and cleared away next start.
    """
    target = running_binary()
    if target is None:
        return
    for stale in target.parent.glob(f"*{OLD_SUFFIX}"):
        try:
            stale.unlink()
        except OSError:
            pass


def download_replacement(url: str, target, progress=None, timeout: int = 120):
    """Fetch the new executable beside the current one."""
    from pathlib import Path

    target = Path(target)
    temp = target.with_name(target.name + ".new-version")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            with open(temp, "wb") as handle:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        try:
            temp.unlink()
        except OSError:
            pass
        raise UpdateError(f"Download failed: {exc}") from exc

    head = temp.read_bytes()[:4] if temp.stat().st_size else b""
    if not any(head.startswith(sig) for sig in SIGNATURES):
        temp.unlink()
        raise UpdateError("What downloaded is not a program. Nothing was changed.")
    if temp.stat().st_size < 1_000_000:
        temp.unlink()
        raise UpdateError("The download is too small to be the program.")
    return temp


def apply(url: str, progress=None) -> dict:
    """Replace the running program with the newer one.

    The trick that makes this work everywhere: a running executable cannot
    be *overwritten*, but on Windows, macOS and Linux alike it can be
    *renamed*. So the current one is moved aside, the new one takes its
    place, and the leftover is deleted on the next start. No helper script
    waiting on a process id, which is the part that usually goes wrong.
    """
    ok, why = can_replace_itself()
    if not ok:
        raise UpdateError(why)

    target = running_binary()
    temp = download_replacement(url, target, progress)
    aside = target.with_name(target.name + OLD_SUFFIX)

    try:
        if aside.exists():
            aside.unlink()
        target.rename(aside)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise UpdateError(f"Could not move the current version aside: {exc}") from exc

    try:
        temp.rename(target)
        if platform.system() != "Windows":
            target.chmod(0o755)
    except OSError as exc:
        # Put it back rather than leave nothing runnable behind.
        try:
            aside.rename(target)
        except OSError:
            pass
        raise UpdateError(f"Could not put the new version in place: {exc}") from exc

    return {"replaced": str(target), "restart": True}


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
        # The bare executable is what an in-place update swaps in; the
        # installer above is the fallback when that is not possible.
        "binary": (_plain_binary(data.get("assets") or []) or {}).get(
            "browser_download_url", "") if newer else "",
        "can_replace": can_replace_itself()[0],
        "why_not": can_replace_itself()[1],
        "note": "" if not newer or asset else
                "A newer version exists but has no download for this system.",
    }
