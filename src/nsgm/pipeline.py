"""Turn a folder of finished downloads into something Steam can hold.

A torrent client drops a folder or an archive somewhere and stops caring.
This picks up from there: unpack what is packed, work out what is
runnable, and say plainly which items still need a person.

The chain is deliberately not fully automatic at one point. Unpacking an
archive is deterministic and reversible, so it happens on request without
ceremony. Running an installer is neither: silent-install switches differ
between makers, an installer can do anything it likes to a machine, and a
download that turned out to be wrong becomes an installed mistake with no
step in between where anybody looked. Those are prepared and handed over
with a button, not run unattended.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

from .scanners import JUNK_DIR, pick_executable, prettify

ARCHIVES = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".zst"}
DISC_IMAGES = {".iso", ".img", ".bin", ".cue", ".chd", ".mdf", ".nrg"}
INSTALLER_HINTS = ("setup", "install", "installer")


class PipelineError(RuntimeError):
    pass


def _size_of(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def classify(path: Path) -> str:
    """What kind of thing has landed, and what has to happen to it."""
    if path.is_file():
        suffix = path.suffix.lower()
        if suffix in ARCHIVES:
            return "archive"
        if suffix in DISC_IMAGES:
            return "image"
        if suffix in (".exe", ".msi"):
            return "installer"
        return "unknown"

    if not path.is_dir():
        return "unknown"

    has_archive = False
    for child in path.rglob("*"):
        if child.is_file():
            suffix = child.suffix.lower()
            if suffix in ARCHIVES:
                has_archive = True

    found = pick_executable(path)
    if found:
        name = found.name.lower()
        if found.suffix.lower() in (".exe", ".msi") and any(
            hint in name for hint in INSTALLER_HINTS
        ):
            return "installer"
        return "ready"
    if has_archive:
        return "archive"
    return "unknown"


# --------------------------------------------------------------------------
# unpacking
# --------------------------------------------------------------------------

def _external_unpacker() -> str:
    for tool in ("7z", "7za", "7zz", "bsdtar"):
        if shutil.which(tool):
            return tool
    return ""


def _safe_extract_zip(archive: Path, target: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            # A crafted archive can name ../../ paths; refuse to write outside.
            destination = (target / member.filename).resolve()
            if not str(destination).startswith(str(target.resolve())):
                raise PipelineError(f"{archive.name} tries to write outside its folder.")
        bundle.extractall(target)


def _safe_extract_tar(archive: Path, target: Path) -> None:
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            destination = (target / member.name).resolve()
            if not str(destination).startswith(str(target.resolve())):
                raise PipelineError(f"{archive.name} tries to write outside its folder.")
        bundle.extractall(target)


def unpack(path: str, into: str = "", keep_archive: bool = True) -> dict:
    """Extract one archive beside itself. Returns where it landed."""
    archive = Path(path).expanduser()
    if not archive.is_file():
        raise PipelineError(f"No such file: {archive}")

    target = Path(into).expanduser() if into else archive.parent / archive.stem
    target.mkdir(parents=True, exist_ok=True)
    suffix = archive.suffix.lower()

    try:
        if suffix == ".zip":
            _safe_extract_zip(archive, target)
        elif suffix in (".tar", ".gz", ".tgz", ".bz2", ".xz") and tarfile.is_tarfile(archive):
            _safe_extract_tar(archive, target)
        else:
            tool = _external_unpacker()
            if not tool:
                raise PipelineError(
                    f"{suffix} archives need 7-Zip installed. Everything else "
                    "unpacks without it."
                )
            command = ([tool, "x", str(archive), f"-o{target}", "-y"]
                       if tool.startswith("7z")
                       else [tool, "-xf", str(archive), "-C", str(target)])
            result = subprocess.run(command, capture_output=True, text=True, timeout=1800)
            if result.returncode != 0:
                raise PipelineError(
                    f"{tool} could not unpack {archive.name}: "
                    f"{(result.stderr or '').strip()[:200]}"
                )
    except (zipfile.BadZipFile, tarfile.TarError) as exc:
        raise PipelineError(f"{archive.name} is damaged ({exc}).") from exc
    except subprocess.SubprocessError as exc:
        raise PipelineError(f"Unpacking {archive.name} failed ({exc}).") from exc

    if not keep_archive:
        try:
            archive.unlink()
        except OSError:
            pass

    # Archives often hold a single folder; use it rather than nesting.
    entries = [c for c in target.iterdir() if not c.name.startswith(".")]
    if len(entries) == 1 and entries[0].is_dir():
        target = entries[0]

    found = pick_executable(target)
    return {
        "path": str(target),
        "state": classify(target),
        "executable": str(found) if found else "",
    }


# --------------------------------------------------------------------------
# looking at what has arrived
# --------------------------------------------------------------------------

def survey(folder: str, ignore: list | None = None) -> list[dict]:
    """Everything sitting in a completed-downloads folder, and its state."""
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise PipelineError(f"No such folder: {folder}")

    skip = {str(Path(p)) for p in (ignore or [])}
    items: list[dict] = []

    for child in sorted(root.iterdir()):
        if child.name.startswith(".") or str(child) in skip:
            continue
        if child.is_dir() and JUNK_DIR.match(child.name):
            continue
        if child.is_file() and child.suffix.lower() in (".part", ".!qb", ".tmp"):
            continue        # still downloading

        state = classify(child)
        found = pick_executable(child) if child.is_dir() else None
        items.append(
            {
                "path": str(child),
                "name": prettify(child.name),
                "state": state,
                "size": _size_of(child),
                "executable": str(found) if found else "",
                "needs": {
                    "ready": "nothing, it can go into Steam",
                    "archive": "unpacking",
                    "installer": "you to run its installer",
                    "image": "mounting or an emulator",
                    "unknown": "a look, nothing runnable was found",
                }[state],
            }
        )
    return items


def to_game(item: dict) -> dict:
    """Shape a prepared item the way the Steam side expects."""
    return {
        "title": item.get("name") or Path(item["path"]).name,
        "source": "folder",
        "exe": item.get("executable", ""),
        "args": "",
        "start_dir": item["path"] if Path(item["path"]).is_dir() else "",
        "launcher_exe": "",
        "launcher_args": "",
        "icon": "",
        "tags": ["Downloaded"],
        "sgdb_hint": item.get("name", ""),
        "detail": item["path"],
    }


# --------------------------------------------------------------------------
# noticing that a download has finished
# --------------------------------------------------------------------------

PARTIAL = (".part", ".!qb", ".tmp", ".crdownload", ".aria2", ".bts", ".parts")


def is_settled(path: Path) -> bool:
    """No part-files left anywhere underneath."""
    if path.is_file():
        return not path.name.lower().endswith(PARTIAL)
    try:
        for child in path.rglob("*"):
            if child.is_file() and child.name.lower().endswith(PARTIAL):
                return False
    except OSError:
        return False
    return True


def sample(folder: str) -> dict:
    """Sizes of everything in the folder, for comparing against later.

    A torrent client gives no signal when it finishes, so completion is
    inferred: an item whose size has not moved between two looks, and
    which has no part-files left, has stopped being written to.
    """
    root = Path(folder).expanduser()
    out: dict = {}
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if child.name.startswith("."):
            continue
        out[str(child)] = {"size": _size_of(child), "settled": is_settled(child)}
    return out


def finished_since(previous: dict, current: dict) -> list[str]:
    """Items that have stopped being written to.

    Settled means two things at once: no part-files left underneath, and a
    size that has not moved since the previous look. Callers keep their own
    record of what they have already acted on, so this stays a plain
    question about the folder rather than a memory of past answers.
    """
    done = []
    for path, now in current.items():
        before = previous.get(path)
        if before is None:
            continue                      # first sighting; wait one round
        if now["settled"] and now["size"] > 0 and now["size"] == before["size"]:
            done.append(path)
    return done


# --------------------------------------------------------------------------
# handing an installer over, then noticing what it left behind
# --------------------------------------------------------------------------

def installer_in(path: str) -> str:
    """The setup program inside a downloaded item, if there is one."""
    target = Path(path).expanduser()
    if target.is_file() and target.suffix.lower() in (".exe", ".msi"):
        return str(target)
    if not target.is_dir():
        return ""
    best = ""
    for child in sorted(target.rglob("*")):
        if child.suffix.lower() not in (".exe", ".msi"):
            continue
        name = child.stem.lower()
        if any(hint in name for hint in INSTALLER_HINTS):
            return str(child)
        best = best or str(child)
    return best


def launch(path: str) -> dict:
    """Start an installer and let the person drive it.

    Deliberately not silent and not unattended: where it installs, onto
    which disk, and whether it should be installed at all are decisions
    worth keeping.
    """
    target = Path(path).expanduser()
    if not target.exists():
        raise PipelineError(f"No such file: {target}")

    try:
        if os.name == "nt":
            os.startfile(str(target))  # noqa: S606 - hands it to the shell
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(target)])
        else:
            subprocess.Popen([str(target)])
    except (OSError, subprocess.SubprocessError) as exc:
        raise PipelineError(f"Could not start {target.name}: {exc}") from exc
    return {"started": str(target)}


def installed_snapshot() -> dict:
    """What is installed right now, to compare against after a setup runs."""
    from . import scanners

    notes: list = []
    seen: dict = {}
    try:
        if os.name == "nt":
            found = scanners.scan_installed_programs(notes)
        else:
            found = scanners.scan_desktop_entries(notes)
    except Exception:
        found = []
    for game in found:
        seen[game["title"].strip().lower()] = game
    return seen


def newly_installed(before: dict) -> list[dict]:
    """Whatever appeared since the snapshot was taken."""
    after = installed_snapshot()
    fresh = [game for key, game in after.items() if key not in before]
    fresh.sort(key=lambda g: g["title"].lower())
    return fresh
