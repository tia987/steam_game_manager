"""Recognise what a download is, how it was built, and how to install it.

Installing into a chosen folder without anyone clicking through a wizard
only works if the installer's maker is known, because every maker spells
"be quiet and install here" differently:

=================  ==========================================
Inno Setup         ``/VERYSILENT /DIR="..."``  (GOG's offline installers, most repacks)
NSIS               ``/S /D=...``  (the folder must come last, unquoted)
Windows Installer  ``msiexec /i x.msi /qn INSTALLDIR="..."``
InstallShield      ``/s /v"/qn INSTALLDIR=..."``  (the MSI-based kind)
WiX bundles        ``/quiet InstallFolder="..."``
Advanced Inst.     ``/exenoui /qn APPDIR="..."``
=================  ==========================================

The maker is read from the file itself: each of these leaves a marker in
the executable or in the data appended after it. Anything unrecognised is
never guessed at; it can still be installed through its own screens.

The same switches pre-fill the folder when the installer's own screens
are used, so a classic install still defaults to the library folder.
"""

from __future__ import annotations

import os
import re
import struct
import tarfile
import zipfile
from pathlib import Path

ARCHIVES = (".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".tbz2", ".txz")
DISC_IMAGES = (".iso", ".img", ".bin", ".cue", ".chd", ".mdf", ".nrg")
INSTALLER_HINTS = ("setup", "install", "installer")

FRAMEWORKS: dict[str, dict] = {
    "inno": {"label": "Inno Setup", "quiet": True, "folder": True},
    "nsis": {"label": "NSIS", "quiet": True, "folder": True},
    "msi": {"label": "Windows Installer", "quiet": True, "folder": True},
    "installshield": {"label": "InstallShield", "quiet": True, "folder": True},
    "wix": {"label": "WiX bundle", "quiet": True, "folder": True},
    "advanced": {"label": "Advanced Installer", "quiet": True, "folder": True},
    "7zsfx": {"label": "7-Zip self-extractor", "quiet": True, "folder": True},
    "rarsfx": {"label": "WinRAR self-extractor", "quiet": True, "folder": True},
    "unknown": {"label": "Unrecognised installer", "quiet": False, "folder": False},
}

# Markers, in the order they are trusted. Each maker writes its name into
# the program or its appended data, and none of them write another's.
MARKERS: list[tuple[str, tuple[bytes, ...]]] = [
    ("inno", (b"Inno Setup Setup Data", b"JR.Inno.Setup", b"InnoSetupLdrWindow", b"rDlPtS")),
    ("nsis", (b"NullsoftInst", b"Nullsoft.NSIS", b"Nullsoft Install System")),
    ("advanced", (b"Advanced Installer",)),
    ("installshield", (b"InstallShield",)),
    ("7zsfx", (b"7z\xbc\xaf\x27\x1c",)),
    ("rarsfx", (b"Rar!\x1a\x07",)),
]

HEAD_BYTES = 4 * 1024 * 1024
OVERLAY_BYTES = 512 * 1024


def _pe_layout(handle) -> tuple[int, list[bytes]]:
    """Where a Windows program's own code ends, and its section names.

    Everything past the last section is data glued on afterwards, which
    is where installer makers keep their payload and its signature.
    """
    handle.seek(0)
    dos = handle.read(64)
    if len(dos) < 64 or dos[:2] != b"MZ":
        return 0, []
    offset = struct.unpack_from("<I", dos, 0x3C)[0]
    handle.seek(offset)
    if handle.read(4) != b"PE\0\0":
        return 0, []
    coff = handle.read(20)
    if len(coff) < 20:
        return 0, []
    count = struct.unpack_from("<H", coff, 2)[0]
    optional = struct.unpack_from("<H", coff, 16)[0]
    handle.seek(offset + 24 + optional)
    end, names = 0, []
    for _ in range(min(count, 96)):
        header = handle.read(40)
        if len(header) < 40:
            break
        names.append(header[:8].rstrip(b"\0"))
        size, pointer = struct.unpack_from("<II", header, 16)
        end = max(end, pointer + size)
    return end, names


def detect(path: str | Path) -> dict:
    """``{"framework", "label", "quiet", "folder", "elevation"}`` for one file."""
    target = Path(path)
    suffix = target.suffix.lower()
    unknown = dict(FRAMEWORKS["unknown"], framework="unknown", elevation="")
    if suffix == ".msi":
        return dict(FRAMEWORKS["msi"], framework="msi", elevation="")
    if suffix != ".exe":
        return unknown
    try:
        with open(target, "rb") as handle:
            head = handle.read(HEAD_BYTES)
            end, sections = _pe_layout(handle)
            overlay = b""
            if end and end < target.stat().st_size:
                handle.seek(end)
                overlay = handle.read(OVERLAY_BYTES)
    except OSError:
        return unknown

    level = ""
    found = re.search(rb"requestedExecutionLevel[^>]{0,80}?level=[\"']([A-Za-z]+)", head)
    if found:
        level = found.group(1).decode("ascii", "replace")

    if b".wixburn" in sections:
        return dict(FRAMEWORKS["wix"], framework="wix", elevation=level)
    blob = head + overlay
    for name, needles in MARKERS:
        if name in ("7zsfx", "rarsfx"):
            # Archive signatures only count in the appended data, where a
            # self-extractor keeps its archive; plenty of programs carry
            # these bytes somewhere in their code.
            if any(needle in overlay[:65536] for needle in needles):
                return dict(FRAMEWORKS[name], framework=name, elevation=level)
            continue
        if any(needle in blob for needle in needles):
            return dict(FRAMEWORKS[name], framework=name, elevation=level)
    return dict(unknown, elevation=level)


# --------------------------------------------------------------------------
# what a download is
# --------------------------------------------------------------------------

def is_archive(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(ARCHIVES) or bool(re.search(r"\.(7z|zip|rar)\.0*1$", name))


def first_volume(paths: list[Path]) -> Path | None:
    """The part a multi-volume archive has to be opened from."""
    ranked = []
    for path in paths:
        name = path.name.lower()
        if re.search(r"\.part0*1\.rar$", name) or re.search(r"\.(7z|zip|rar)\.0*1$", name):
            ranked.append((0, path))
        elif re.search(r"\.part\d+\.rar$", name) or re.search(r"\.(7z|zip|rar)\.\d+$", name):
            continue                        # a later volume
        elif name.endswith(ARCHIVES):
            ranked.append((1, path))
    if not ranked:
        return None
    ranked.sort(key=lambda pair: (pair[0], len(pair[1].parts), pair[1].name.lower()))
    return ranked[0][1]


JUNK_PARTS = re.compile(
    r"(?i)^(_?redist.*|_?commonredist|directx|dotnet|vcredist|support|__installer|"
    r"installers?|prerequisites?|redistributables?|dependencies)$"
)
NOT_THE_INSTALLER = re.compile(
    r"(?i)^(unins\d*|uninstall.*|vc_?redist.*|vcredist.*|dxsetup|dxwebsetup|"
    r"dotnet.*|ndp\d+.*|oalinst|physx.*|ue\d?prereqsetup.*|.*redist.*)\.(exe|msi)$"
)


def find_installer(folder: Path, depth: int = 2) -> Path | None:
    """The setup program inside a downloaded folder, if there is one.

    Redistributable installers for DirectX or the Visual C++ runtime sit
    beside many games and are never the game's installer, so folders and
    names that hold them are skipped rather than mistaken for it.
    """
    best: tuple[int, Path] | None = None
    root_depth = len(folder.parts)
    for current, dirs, files in os.walk(folder):
        here = Path(current)
        level = len(here.parts) - root_depth
        dirs[:] = [d for d in dirs if not JUNK_PARTS.match(d) and not d.startswith(".")]
        if level >= depth:
            dirs[:] = []
        for name in files:
            lower = name.lower()
            if not lower.endswith((".exe", ".msi")) or NOT_THE_INSTALLER.match(name):
                continue
            stem = lower.rsplit(".", 1)[0]
            hinted = any(hint in stem for hint in INSTALLER_HINTS)
            if not hinted and not lower.endswith(".msi"):
                continue
            score = level * 10 + (0 if stem in ("setup", "install") else 1)
            if best is None or score < best[0]:
                best = (score, here / name)
    return best[1] if best else None


def parts_of(installer: Path) -> list[Path]:
    """An installer and the data files that travel with it.

    GOG splits big installers into ``setup_x.exe`` plus ``setup_x-1.bin``
    and so on; repacks use their own names. All of it is the payload.
    """
    out = [installer]
    folder = installer.parent
    stem = installer.stem.lower()
    try:
        siblings = list(folder.iterdir())
    except OSError:
        return out
    for sibling in siblings:
        name = sibling.name.lower()
        if sibling == installer or not sibling.is_file():
            continue
        if name.startswith(stem) and name.endswith((".bin", ".cab", ".dat")):
            out.append(sibling)
        elif re.fullmatch(r"(fg|data|setup)-?\d+\.bin", name):
            out.append(sibling)
    return out


def folder_size(path: Path, cap: int = 200000) -> int:
    total = files = 0
    try:
        for current, dirs, names in os.walk(path):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(current, d))]
            for name in names:
                try:
                    total += os.path.getsize(os.path.join(current, name))
                except OSError:
                    pass
                files += 1
                if files > cap:
                    return total
    except OSError:
        pass
    return total


def inspect(path: str | Path) -> dict:
    """Work out what a downloaded item is and how it gets installed.

    ``kind`` is one of ``installer``, ``archive``, ``folder``, ``program``
    (a single portable executable), ``appimage``, ``dmg``, ``pkg`` or
    ``unsupported``. ``source`` is the file the work starts from, which
    for a downloaded folder may be something inside it.
    """
    item = Path(path).expanduser()
    out = {"path": str(item), "kind": "unsupported", "source": str(item),
           "framework": "", "label": "", "quiet": False, "folder": False,
           "elevation": "", "payload": 0, "reason": ""}
    if not item.exists():
        out["reason"] = "It is no longer there."
        return out

    if item.is_dir():
        from .scanners import pick_executable

        installer = find_installer(item)
        ready = pick_executable(item)
        if ready and installer and Path(ready).resolve() == installer.resolve():
            ready = None
        if ready and not installer:
            out.update(kind="folder", payload=folder_size(item))
            return out
        if installer:
            found = detect(installer)
            out.update(kind="installer", source=str(installer), payload=folder_size(item),
                       **{k: found[k] for k in ("framework", "label", "quiet", "folder",
                                                "elevation")})
            return out
        try:
            volume = first_volume([p for p in item.rglob("*") if p.is_file()])
        except OSError:
            volume = None
        if volume:
            out.update(kind="archive", source=str(volume), payload=folder_size(item))
            return out
        out["reason"] = "Nothing in it can be run or unpacked."
        return out

    name = item.name.lower()
    size = item.stat().st_size
    if is_archive(item):
        out.update(kind="archive", payload=size)
    elif name.endswith(".appimage"):
        out.update(kind="appimage", payload=size)
    elif name.endswith(".dmg"):
        out.update(kind="dmg", payload=size)
    elif name.endswith(".pkg"):
        out.update(kind="pkg", payload=size)
    elif name.endswith((".exe", ".msi")):
        found = detect(item)
        hinted = any(hint in item.stem.lower() for hint in INSTALLER_HINTS)
        if found["framework"] in ("7zsfx", "rarsfx"):
            out.update(kind="archive", payload=size, framework=found["framework"],
                       label=found["label"])
        elif found["framework"] != "unknown" or hinted or name.endswith(".msi"):
            payload = sum(p.stat().st_size for p in parts_of(item) if p.exists())
            out.update(kind="installer", payload=payload,
                       **{k: found[k] for k in ("framework", "label", "quiet", "folder",
                                                "elevation")})
        else:
            out.update(kind="program", payload=size)
    elif name.endswith(DISC_IMAGES):
        out["reason"] = "Disc images need mounting or an emulator, not installing."
    else:
        out["reason"] = "Not something that can be installed."
    return out


def unpacked_size(archive: Path) -> int:
    """Bytes an archive takes once unpacked; 0 when it cannot be told."""
    name = archive.name.lower()
    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as bundle:
                return sum(info.file_size for info in bundle.infolist())
        if name.endswith((".tar", ".tgz", ".tbz2", ".txz", ".gz", ".bz2", ".xz")) \
                and tarfile.is_tarfile(archive):
            with tarfile.open(archive) as bundle:
                return sum(member.size for member in bundle.getmembers())
    except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError):
        return 0
    return 0


# Installed size per byte of installer when nothing better is known.
# Game data is mostly already compressed, so ordinary installers expand
# modestly; repacks are squeezed much harder, which this deliberately
# leans towards, because overestimating only means a bigger disk is
# picked, while underestimating fills one up.
EXPANSION = 2.0


def estimate(info: dict) -> tuple[int, str]:
    """A size estimate for an inspected item and a note saying how."""
    kind, payload = info.get("kind"), int(info.get("payload") or 0)
    if kind in ("folder", "program", "appimage"):
        return payload, "its size on disk"
    if kind == "archive":
        exact = unpacked_size(Path(info["source"]))
        if exact:
            return exact, "the archive's unpacked size"
        return int(payload * EXPANSION), "estimated from the archive's size"
    if kind in ("installer", "dmg", "pkg"):
        return int(payload * EXPANSION), "estimated from the installer's size"
    return 0, ""


# --------------------------------------------------------------------------
# command lines
# --------------------------------------------------------------------------

def _q(value: str) -> str:
    return '"' + str(value).replace('"', "") + '"'


def quiet_command(info: dict, target: str, log: str = "") -> dict:
    """``{"file", "params", "elevate"}`` for an unattended install into ``target``."""
    framework = info.get("framework")
    source = info["source"]
    if framework == "inno":
        params = f"/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /DIR={_q(target)}"
        if log:
            params += f" /LOG={_q(log)}"
        return {"file": source, "params": params, "elevate": False}
    if framework == "nsis":
        # NSIS reads /D= to the end of the line, spaces and all, and a
        # quote would become part of the folder name.
        return {"file": source, "params": f"/S /D={target}", "elevate": False}
    if framework == "msi":
        folder = " ".join(f"{prop}={_q(target)}" for prop in
                          ("INSTALLDIR", "INSTALLFOLDER", "APPLICATIONFOLDER",
                           "INSTALLLOCATION", "TARGETDIR"))
        params = f"/i {_q(source)} /qn /norestart {folder}"
        if log:
            params += f" /l*v {_q(log)}"
        # Silent Windows Installer cannot show the permission prompt itself,
        # so it is asked for up front instead.
        return {"file": "msiexec.exe", "params": params, "elevate": True}
    if framework == "installshield":
        inner = target.replace('"', "")
        return {"file": source, "params": f'/s /v"/qn INSTALLDIR=\\"{inner}\\""',
                "elevate": False}
    if framework == "wix":
        return {"file": source, "params": f"/quiet /norestart InstallFolder={_q(target)}",
                "elevate": False}
    if framework == "advanced":
        return {"file": source, "params": f"/exenoui /qn APPDIR={_q(target)}",
                "elevate": False}
    raise ValueError("This installer cannot be run quietly.")


def classic_command(info: dict, target: str = "") -> dict:
    """The installer's own screens, with the folder pre-filled where possible."""
    framework = info.get("framework")
    source = info["source"]
    if framework == "msi":
        params = f"/i {_q(source)}"
        if target:
            params += f" INSTALLDIR={_q(target)} INSTALLFOLDER={_q(target)}" \
                      f" APPLICATIONFOLDER={_q(target)}"
        return {"file": "msiexec.exe", "params": params, "elevate": False}
    if not target:
        return {"file": source, "params": "", "elevate": False}
    if framework == "inno":
        return {"file": source, "params": f"/DIR={_q(target)}", "elevate": False}
    if framework == "nsis":
        return {"file": source, "params": f"/D={target}", "elevate": False}
    if framework == "installshield":
        inner = target.replace('"', "")
        return {"file": source, "params": f'/v"INSTALLDIR=\\"{inner}\\""', "elevate": False}
    if framework == "wix":
        return {"file": source, "params": f"InstallFolder={_q(target)}", "elevate": False}
    if framework == "advanced":
        return {"file": source, "params": f"APPDIR={_q(target)}", "elevate": False}
    return {"file": source, "params": "", "elevate": False}


SUCCESS = {
    "inno": {0: "", 8: "Installed. Windows needs a restart to finish."},
    "nsis": {0: ""},
    "msi": {0: "", 3010: "Installed. Windows needs a restart to finish.",
            1641: "Installed. Windows is restarting to finish."},
}
FAILURE = {
    "inno": {1: "Setup could not start.", 2: "It was cancelled before installing anything.",
             3: "Setup hit an error while preparing.", 4: "Setup hit an error while installing.",
             5: "It was cancelled part way through.", 6: "Setup was stopped.",
             7: "Setup decided it cannot continue on this computer."},
    "nsis": {1: "It was cancelled.", 2: "The installer stopped itself."},
    "msi": {1602: "It was cancelled.", 1603: "Windows Installer hit a fatal error.",
            1618: "Another installation is already running. Try again when it finishes.",
            1619: "Windows Installer could not open the package.",
            1625: "System policy blocked the installation.",
            1638: "Another version of this product is already installed."},
}


def interpret(framework: str, code) -> tuple[bool | None, str]:
    """Whether an exit code means success, and what to say about it.

    ``None`` means the code proves nothing either way, which is common:
    plenty of installers exit 0 when cancelled, and the outcome then has
    to be read from the disk instead.
    """
    if code is None:
        return None, ""
    family = "msi" if framework in ("msi", "installshield", "advanced") else framework
    if code in SUCCESS.get(family, {}):
        return True, SUCCESS[family][code]
    if code in FAILURE.get(family, {}):
        return False, FAILURE[family][code]
    if family in SUCCESS and code != 0:
        return False, f"The installer stopped with code {code}."
    return None, ""
