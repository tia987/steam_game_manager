"""Turn folders of ROMs into launchable entries, one parser per system.

A parser says where the ROMs are, which extensions count, and how to
build the emulator command line. The command template understands:

    {rom}      full path to the ROM file
    {romName}  file name with extension
    {romBase}  file name without extension
    {romDir}   folder holding the ROM
    {emuDir}   folder holding the emulator

Titles get cleaned up on the way out, because ``Chrono Trigger (USA)
[!].sfc`` is not what anyone wants on a library shelf.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from .scanners import make_game

DISC = re.compile(r"(?i)\s*[\(\[]\s*(disc|disk|cd|side)\s*([0-9a-d]+)[^)\]]*\s*[\)\]]")

# Everything in brackets in a ROM set is metadata: region, revision, dump
# flags, group tags. Removing the whole group beats chasing each token,
# which used to leave stray parentheses behind on names like "(USA, Australia)".
BRACKETS = re.compile(r"\s*[\(\[\{][^\(\)\[\]\{\}]*[\)\]\}]")

# Scene and store suffixes that trail the real title.
TAIL = re.compile(r"(?i)\s*[-–_]\s*(gog|repack|portable|proper|readnfo|multi\d*|pc)\s*$")

# "Zelda, The - A Link to the Past" is catalogue order, not how people speak.
ARTICLE = re.compile(
    r"(?i)^(.*?),\s*(The|A|An|Le|La|Les|Der|Die|Das|El|Los|Il)(\s+[-–]\s+.*|\s*)$"
)


def clean_title(name: str, extra_strip: str = "") -> str:
    """``Final Fantasy VII (USA) (Disc 1) [!].bin`` -> ``Final Fantasy VII``."""
    original = Path(name).stem
    text = original
    if extra_strip:
        try:
            text = re.sub(extra_strip, "", text)
        except re.error:
            pass

    text = DISC.sub("", text)
    previous = None
    while previous != text:
        previous = text
        text = BRACKETS.sub(" ", text)

    # Dots separate words in scene names but are real punctuation in
    # "Super Mario Bros. 3", so only unpack them when nothing else does.
    text = text.replace("_", " ")
    if " " not in text.strip():
        text = text.replace(".", " ")

    text = TAIL.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" -–_.")

    match = ARTICLE.match(text)
    if match:
        text = f"{match.group(2).title()} {match.group(1)}{match.group(3)}".strip()
        text = re.sub(r"\s{2,}", " ", text)

    if text.islower():
        text = text.title()
    return text or original


def disc_number(name: str) -> int | None:
    match = DISC.search(name)
    if not match:
        return None
    value = match.group(2)
    return int(value) if value.isdigit() else ord(value.lower()) - 96


def new_parser(**overrides) -> dict:
    """A blank parser with sane defaults."""
    parser = {
        "id": uuid.uuid4().hex[:10],
        "name": "New system",
        "rom_dir": "",
        "extensions": "",
        "recursive": True,
        "emulator": "",
        "args": '"{rom}"',
        "start_dir": "",
        "tags": [],
        "sgdb_platform": "",
        "strip_regex": "",
        "group_multidisc": True,
        "enabled": True,
    }
    parser.update(overrides)
    return parser


def _expand(template: str, rom: Path, emulator: str) -> str:
    emu_dir = str(Path(emulator).parent) if emulator else ""
    return (
        template.replace("{rom}", str(rom))
        .replace("{romName}", rom.name)
        .replace("{romBase}", rom.stem)
        .replace("{romDir}", str(rom.parent))
        .replace("{emuDir}", emu_dir)
    )


STEAM_OWNED = re.compile(r"(?i)[\\/]steamapps[\\/]common[\\/]")


def emulator_warning(emulator: str) -> str:
    """Flag the case where the emulator is itself a Steam game.

    Steam treats a shortcut pointing into its own steamapps folder as the
    game it already owns: it hands the launch back to that app, which
    starts without the arguments, so the ROM is ignored and the emulator
    opens to its own menu. A copy installed outside Steam does not have
    this problem.
    """
    if emulator and STEAM_OWNED.search(emulator):
        name = Path(emulator).stem
        return (
            f"{name} is installed through Steam. Shortcuts pointing at it are "
            "handed back to Steam, which starts it without the ROM argument, "
            "so games open to the emulator menu instead. Install a standalone "
            "copy outside the Steam folder and point this parser at that."
        )
    return ""


def run_parser(parser: dict, notes: list[str]) -> list[dict]:
    """Find every ROM this parser covers and build one entry per game."""
    rom_dir = Path(str(parser.get("rom_dir", "")).strip()).expanduser()
    if not rom_dir.is_dir():
        notes.append(f"{parser.get('name', 'Parser')}: folder not found ({rom_dir}).")
        return []

    extensions = {
        e.strip().lower().lstrip(".")
        for e in re.split(r"[,\s]+", str(parser.get("extensions", "")))
        if e.strip()
    }
    walker = rom_dir.rglob("*") if parser.get("recursive", True) else rom_dir.glob("*")

    files: list[Path] = []
    for path in walker:
        if not path.is_file() or path.name.startswith("."):
            continue
        suffix = path.suffix.lower().lstrip(".")
        if extensions and suffix not in extensions:
            continue
        if not extensions and suffix in {"txt", "xml", "dat", "sav", "srm", "state", "png", "jpg"}:
            continue
        files.append(path)

    if not files:
        notes.append(f"{parser.get('name', 'Parser')}: no ROMs matched in {rom_dir}.")
        return []

    strip = str(parser.get("strip_regex", ""))
    system = str(parser.get("name", "")).strip()
    tags = list(parser.get("tags") or ([system] if system else []))
    emulator = str(parser.get("emulator", "")).strip()
    warning = emulator_warning(emulator)
    if warning:
        notes.append(f"{parser.get('name', 'ROM system')}: {warning}")
    template = str(parser.get("args") or '"{rom}"')

    # Multi-disc sets collapse to a single shelf entry. An .m3u playlist
    # beside the discs wins, because that is what the emulator wants.
    chosen: dict[str, Path] = {}
    for path in sorted(files):
        title = clean_title(path.name, strip)
        key = title.lower()
        current = chosen.get(key)
        if current is None:
            chosen[key] = path
            continue
        if not parser.get("group_multidisc", True):
            chosen[f"{key}::{path.stem.lower()}"] = path
            continue
        if path.suffix.lower() == ".m3u" and current.suffix.lower() != ".m3u":
            chosen[key] = path
        elif current.suffix.lower() != ".m3u":
            if (disc_number(path.name) or 99) < (disc_number(current.name) or 99):
                chosen[key] = path

    games = []
    for path in sorted(chosen.values()):
        title = clean_title(path.name, strip)
        games.append(
            make_game(
                title,
                f"rom:{parser.get('id')}",
                exe=emulator or str(path),
                args=_expand(template, path, emulator) if emulator else "",
                start_dir=parser.get("start_dir") or (str(Path(emulator).parent) if emulator else str(path.parent)),
                tags=tags,
                sgdb_hint=title,
            )
        )
    return games


# --------------------------------------------------------------------------
# starting points so nobody has to look up core names
# --------------------------------------------------------------------------

PRESETS: list[dict] = [
    {"name": "Nintendo Entertainment System", "extensions": "nes,fds,unf,zip",
     "core": "mesen", "sgdb_platform": "Nintendo Entertainment System"},
    {"name": "Super Nintendo", "extensions": "sfc,smc,bs,zip",
     "core": "snes9x", "sgdb_platform": "Super Nintendo Entertainment System"},
    {"name": "Nintendo 64", "extensions": "n64,z64,v64,zip",
     "core": "mupen64plus_next", "sgdb_platform": "Nintendo 64"},
    {"name": "Game Boy / Color", "extensions": "gb,gbc,zip",
     "core": "gambatte", "sgdb_platform": "Game Boy"},
    {"name": "Game Boy Advance", "extensions": "gba,zip",
     "core": "mgba", "sgdb_platform": "Game Boy Advance"},
    {"name": "Nintendo DS", "extensions": "nds,zip",
     "core": "melonds", "sgdb_platform": "Nintendo DS"},
    {"name": "Sega Genesis", "extensions": "md,gen,bin,smd,zip",
     "core": "genesis_plus_gx", "sgdb_platform": "Sega Mega Drive"},
    {"name": "Sega Dreamcast", "extensions": "gdi,cdi,chd,m3u",
     "core": "flycast", "sgdb_platform": "Sega Dreamcast"},
    {"name": "PlayStation", "extensions": "cue,chd,pbp,m3u",
     "core": "swanstation", "sgdb_platform": "Sony PlayStation"},
    {"name": "PlayStation 2", "extensions": "iso,chd,cso",
     "core": "", "standalone": "PCSX2", "sgdb_platform": "Sony PlayStation 2"},
    {"name": "PlayStation Portable", "extensions": "iso,cso,chd",
     "core": "ppsspp", "sgdb_platform": "Sony PlayStation Portable"},
    {"name": "GameCube / Wii", "extensions": "iso,rvz,gcm,wbfs",
     "core": "", "standalone": "Dolphin", "sgdb_platform": "Nintendo GameCube"},
    {"name": "Arcade (FBNeo)", "extensions": "zip,7z",
     "core": "fbneo", "sgdb_platform": "Arcade"},
    {"name": "DOS", "extensions": "conf,exe,bat",
     "core": "dosbox_pure", "sgdb_platform": "DOS"},
]


def preset_parser(preset: dict, retroarch_path: str = "") -> dict:
    """Build a ready-to-edit parser from a preset."""
    core = preset.get("core")
    if core and retroarch_path:
        root = Path(retroarch_path).parent
        ext = "dll" if retroarch_path.lower().endswith(".exe") else "so"
        args = f'-L "{root / "cores" / f"{core}_libretro.{ext}"}" "{{rom}}"'
        emulator = retroarch_path
    else:
        args = '"{rom}"'
        emulator = ""
    return new_parser(
        name=preset["name"],
        extensions=preset["extensions"],
        emulator=emulator,
        args=args,
        tags=[preset["name"]],
        sgdb_platform=preset.get("sgdb_platform", ""),
    )
