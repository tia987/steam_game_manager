"""Turn a folder of finished downloads into something Steam can hold.

A torrent client drops a folder or an archive somewhere and stops caring.
This picks up from there: unpack what is packed, work out what is
runnable, and say plainly which items still need a person.

Installing what arrives is ``autoinstall``'s job. By default it waits
for a person to press Install, because an installer can do anything it
likes to a machine; installing unattended is an opt-in setting.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

from .scanners import JUNK_DIR, pick_executable, prettify
from . import installers, proc, tools

ARCHIVES = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".zst"}
DISC_IMAGES = {".iso", ".img", ".bin", ".cue", ".chd", ".mdf", ".nrg"}
INSTALLER_HINTS = ("setup", "install", "installer")


class PipelineError(RuntimeError):
    pass


class NeedsSevenZip(PipelineError):
    """Raised when an archive needs 7-Zip and none is available."""


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
    # A folder holding only "setup.exe" and its data used to read as
    # unclear, because the game-executable picker skips setup programs.
    if installers.find_installer(path):
        return "installer"
    if has_archive:
        return "archive"
    return "unknown"


# --------------------------------------------------------------------------
# unpacking
# --------------------------------------------------------------------------

def _external_unpacker() -> str:
    return tools.seven_zip() or tools.bsdtar()


def _inside(target: Path, name: str) -> bool:
    destination = (target / name).resolve()
    root = target.resolve()
    return destination == root or root in destination.parents


class Stopped(PipelineError):
    """Raised when unpacking is stopped part way through."""


def _check(stop) -> None:
    if stop is not None and stop():
        raise Stopped("Stopped.")


def _safe_extract_zip(archive: Path, target: Path, progress=None, stop=None) -> None:
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        for member in members:
            # A crafted archive can name ../../ paths; refuse to write outside.
            if not _inside(target, member.filename):
                raise PipelineError(f"{archive.name} tries to write outside its folder.")
        total = sum(m.file_size for m in members) or 1
        done = 0
        for member in members:
            _check(stop)
            bundle.extract(member, target)
            done += member.file_size
            if progress:
                progress(done / total, member.filename)


def _safe_extract_tar(archive: Path, target: Path, progress=None, stop=None) -> None:
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        for member in members:
            if not _inside(target, member.name):
                raise PipelineError(f"{archive.name} tries to write outside its folder.")
            if member.issym() or member.islnk():
                link = Path(member.name).parent / member.linkname
                if Path(member.linkname).is_absolute() or not _inside(target, str(link)):
                    raise PipelineError(f"{archive.name} holds a link pointing outside its folder.")
        total = sum(m.size for m in members) or 1
        done = 0
        for member in members:
            _check(stop)
            bundle.extract(member, target)
            done += member.size
            if progress:
                progress(done / total, member.name)


PERCENT = re.compile(r"(\d{1,3})%")


def _run_external(tool: str, archive: Path, target: Path, progress=None, stop=None) -> None:
    if Path(tool).name.lower().startswith("7z"):
        # -bsp1 sends progress to stdout; stdin is closed so a password
        # prompt for an encrypted archive fails instead of waiting forever.
        command = [tool, "x", str(archive), f"-o{target}", "-y", "-bsp1", "-bso0"]
    else:
        command = [tool, "-xf", str(archive), "-C", str(target)]
    process = proc.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    tail = b""
    while True:
        if stop is not None and stop():
            process.kill()
            process.wait(timeout=30)
            raise Stopped("Stopped.")
        chunk = process.stdout.read(256)
        if not chunk:
            break
        tail = (tail + chunk)[-512:]
        found = PERCENT.findall(chunk.decode("ascii", "replace"))
        if found and progress:
            progress(min(int(found[-1]), 100) / 100, "")
    error = process.stderr.read().decode("utf-8", "replace")
    if process.wait(timeout=3600) != 0:
        message = error.strip() or tail.decode("utf-8", "replace").strip()
        if "password" in message.lower() or "encrypted" in message.lower():
            raise PipelineError(f"{archive.name} is password protected.")
        raise PipelineError(f"{Path(tool).name} could not unpack {archive.name}: {message[:200]}")


def hoist(target: Path) -> Path:
    """Lift a lone top folder's contents up a level.

    Archives very often hold one folder holding everything. Installed into
    a library that would mean "D:\\Games\\Hades\\Hades v1.2\\..."; lifting it
    gives the folder the name it was meant to have.
    """
    try:
        entries = [c for c in target.iterdir() if not c.name.startswith(".")]
    except OSError:
        return target
    if len(entries) != 1 or not entries[0].is_dir():
        return target
    inner = entries[0]
    temporary = target / (".hoist-" + inner.name)
    inner.rename(temporary)
    for child in temporary.iterdir():
        if (target / child.name).exists():
            continue
        child.rename(target / child.name)
    try:
        temporary.rmdir()
    except OSError:
        pass
    return target


def unpack(path: str, into: str = "", keep_archive: bool = True, progress=None,
           flatten: bool = False, stop=None) -> dict:
    """Extract one archive, beside itself or into ``into``. Returns where it landed.

    ``progress(fraction, current_file)`` is called as it goes, where the
    format allows knowing. ``flatten`` lifts a lone top-level folder.
    """
    archive = Path(path).expanduser()
    if not archive.is_file():
        raise PipelineError(f"No such file: {archive}")

    target = Path(into).expanduser() if into else archive.parent / archive.stem
    target.mkdir(parents=True, exist_ok=True)
    suffix = archive.suffix.lower()

    try:
        if suffix == ".zip" and zipfile.is_zipfile(archive):
            _safe_extract_zip(archive, target, progress, stop)
        elif suffix in (".tar", ".gz", ".tgz", ".bz2", ".xz", ".tbz2", ".txz") \
                and tarfile.is_tarfile(archive):
            _safe_extract_tar(archive, target, progress, stop)
        else:
            tool = _external_unpacker()
            if not tool:
                raise NeedsSevenZip(
                    f"{archive.name} needs 7-Zip to unpack. Zip and tar archives "
                    "unpack without it."
                )
            _run_external(tool, archive, target, progress, stop)
    except (zipfile.BadZipFile, tarfile.TarError) as exc:
        raise PipelineError(f"{archive.name} is damaged ({exc}).") from exc
    except subprocess.SubprocessError as exc:
        raise PipelineError(f"Unpacking {archive.name} failed ({exc}).") from exc

    if not keep_archive:
        try:
            archive.unlink()
        except OSError:
            pass

    if flatten:
        hoist(target)
    else:
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

def _display_stem(path: Path) -> str:
    """A file's name without its extension, or a folder's whole name."""
    if path.is_dir():
        return path.name
    name = path.name
    for suffix in (".tar.gz", ".tar.xz", ".tar.bz2"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


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
                "name": prettify(_display_stem(child)),
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
# what an installer left behind
# --------------------------------------------------------------------------

def installer_in(path: str) -> str:
    """The setup program a downloaded item holds, or "" if it holds none.

    Only real installers count. Any executable used to qualify, so a
    finished download that was already a playable game was announced as
    an installer waiting to be run.
    """
    info = installers.inspect(path)
    return info["source"] if info["kind"] == "installer" else ""


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
