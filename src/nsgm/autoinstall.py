"""Install downloaded games into the library, one at a time.

Every install, whether someone pressed Install or it started by itself,
becomes a task in one queue and runs through the same steps:

1. **Look at it.** What the item is (installer, archive, folder, single
   program), which maker built the installer, how big it will be, and
   whether the game needs a solid state drive. Size and SSD need come
   from the game's Steam store entry when one matches confidently,
   otherwise from the download itself.
2. **Place it.** The library folders are tried in priority order and the
   first with room wins; ``placement`` explains every folder it passes
   over, and a game that cannot be placed waits with that explanation
   instead of being put somewhere wrong.
3. **Install it.** Archives unpack, folders and single programs copy,
   installers run - quietly when their maker is known, or through their
   own screens, held inside the app's window by ``embed``.
4. **Find the game.** The executable is picked out of the folder it went
   into, or, for an installer that ignored the folder it was given, from
   what it registered with Windows.
5. **Hand it on.** The game joins the Add games shelf, selected, ready for
   covers. With the option on it goes straight into Steam as well.

Installs run one at a time. Two installers at once is exactly what
Windows Installer refuses, and one embedded window is all the interface
shows.

Automatic installing is off unless switched on. When it is, the watcher
below notices downloads finishing in the finished-downloads folder and
queues them. What was already in the folder when it was switched on is
left alone - "install new downloads automatically" should not mean
installing a backlog nobody asked about - and is offered as one button
instead.
"""

from __future__ import annotations

import json
import re
import os
import shutil
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import (drives, embed, installers, library, pipeline, placement, scanners, steam,
               storeinfo, tools, uninstall, winaudio, winclean, winproc)

# Whoever runs an external installer or uninstaller holds this, so an
# uninstall never starts while an installer's window is being held.
PROGRAM_LOCK = threading.Lock()

ACTIVE = ("queued", "checking", "installing")
FINISHED = ("done", "failed", "cancelled")



# Side programs installers run once the game is in place. Each opens its own
# window that wants clicking: hopeless with a controller over Moonlight.
#   - file checkers, which hash every installed file (QuickSFV and friends);
#   - redistributable setups (DirectX, Visual C++, .NET, PhysX, OpenAL...),
#     which most PCs already have.
CHECKERS = re.compile(r"(?i)^(quicksfv|quick_sfv|md5check(er)?|hashcheck|rapidcrc|"
                      r"verify(files)?|fitgirl[-_ ]?verify)\w*\.exe$")
REDISTS = re.compile(
    r"(?i)^(vc_?redist\w*|vcredist\w*|vcpp\w*|dxsetup|dxwebsetup|directx\w*|dxredist\w*|"
    r"oalinst|openal\w*|physx\w*|nvidiaphysx\w*|dotnet\w*|dotnetfx\w*|ndp\d\w*|netfx\w*|"
    r"windowsdesktop-runtime\w*|aspnetcore-runtime\w*|xnafx\w*|xna\w*redist\w*|"
    r"ue[345]?prereqsetup\w*|ueprereq\w*|uplay\w*setup\w*|vulkanrt\w*)[\w.-]*\.exe$")


# A browser the installer itself starts is only ever there to show a website.
BROWSERS = re.compile(r"(?i)^(msedge|chrome|firefox|brave|opera|opera_gx|vivaldi|iexplore)\.exe$")


def side_program(name: str) -> str:
    """"checker", "redist", "browser" or "" for a program an installer started."""
    base = os.path.basename(name or "")
    if CHECKERS.match(base):
        return "checker"
    if BROWSERS.match(base):
        return "browser"
    if REDISTS.match(base):
        return "redist"
    return ""


def _setting(name: str, default: bool = True) -> bool:
    try:
        value = library.load_settings().get(name, default)
    except Exception:
        return default
    return default if value is None else value is not False


def _mute_installers() -> bool:
    """Whether installers' own music is silenced (on unless switched off)."""
    try:
        return library.load_settings().get("mute_installers", True) is not False
    except Exception:
        return True

class Cancelled(Exception):
    pass


class Waiting(Exception):
    """The task needs a person: no room, no folder, an unknown installer."""

    def __init__(self, message: str, action: str = ""):
        super().__init__(message)
        self.action = action


@dataclass
class Task:
    id: str
    path: str
    name: str = ""
    kind: str = ""
    framework: str = ""
    label: str = ""
    mode: str = ""                 # quiet or classic
    auto: bool = False
    origin: str = ""               # incoming, download, manual
    status: str = "queued"
    target_root: str = ""
    target_dir: str = ""
    folder_name: str = ""
    target_kind: str = ""
    size: int = 0
    size_note: str = ""
    ssd: str = ""
    ssd_note: str = ""
    ssd_override: str = ""
    size_override: int = 0
    skip_redists: bool = False      # stop DirectX/Visual C++/... setups the installer starts
    skip_checkers: bool = False     # stop file checkers that pop up after installing
    skip_finish: bool = False       # close the installer on its last page, ticked boxes unrun
    installer_asks: bool = False   # classic install with no folder: its screens choose
    notes: list = field(default_factory=list)
    warning: str = ""
    progress: float = -1.0
    message: str = ""
    action: str = ""               # what a waiting task needs: folder, classic, sevenzip
    log: list = field(default_factory=list)
    created: float = 0.0
    started: float = 0.0
    finished: float = 0.0
    result: dict = field(default_factory=dict)
    cancel: bool = False

    def to_json(self) -> dict:
        data = asdict(self)
        data.pop("cancel", None)
        data["log"] = self.log[-30:]
        return data


def _state_file() -> Path:
    return library.config_dir() / "installs.json"


def _seen_file() -> Path:
    return library.config_dir() / "incoming-seen.json"


def display_name(info: dict, store: dict | None = None) -> str:
    """The name a download should have once installed."""
    if store and store.get("found") and store.get("name"):
        return store["name"]
    path = Path(info["path"])
    raw = path.name if path.is_dir() else path.stem
    cleaned = storeinfo.search_title(raw) or scanners.prettify(raw)
    # Release names often arrive all lower case ("hades_ii"); only those
    # are title-cased, so a real name keeps its own capitals.
    if cleaned == cleaned.lower():
        cleaned = cleaned.title()
    return cleaned or scanners.prettify(raw) or "Game"


def _unique(folder: Path) -> Path:
    if not folder.exists() or not any(folder.iterdir()):
        return folder
    for index in range(2, 100):
        candidate = folder.with_name(f"{folder.name} ({index})")
        if not candidate.exists() or not any(candidate.iterdir()):
            return candidate
    return folder.with_name(f"{folder.name} ({int(time.time())})")


def _within(path: Path, roots: list) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        if not root:
            continue
        try:
            base = Path(root).expanduser().resolve()
        except OSError:
            continue
        if base in resolved.parents:
            return True
    return False


class Queue:
    def __init__(self):
        self.lock = threading.RLock()
        self.tasks: list[Task] = []
        self.wake = threading.Event()
        self.events: deque = deque(maxlen=200)
        self.seq = 0
        self.thread: threading.Thread | None = None
        self.current: Task | None = None
        # Set by the server: hands staged games to the shelf.
        self.on_staged = None

    # -- events, read by the interface ---------------------------------------

    def emit(self, kind: str, text: str, **extra) -> None:
        with self.lock:
            self.seq += 1
            self.events.append({"seq": self.seq, "kind": kind, "text": text,
                                "time": time.time(), **extra})

    def events_since(self, since: int) -> tuple[int, list]:
        with self.lock:
            return self.seq, [e for e in self.events if e["seq"] > since]

    # -- persistence -----------------------------------------------------------

    def load(self) -> None:
        try:
            raw = json.loads(_state_file().read_text("utf-8"))
        except (OSError, ValueError):
            raw = []
        tasks = []
        known = {f.name for f in Task.__dataclass_fields__.values()}
        for item in raw if isinstance(raw, list) else []:
            try:
                task = Task(**{k: v for k, v in item.items() if k in known})
            except TypeError:
                continue
            if task.status in ("checking", "installing"):
                task.status = "failed"
                task.message = "The app closed while this was installing. Press Retry to run it again."
            tasks.append(task)
        with self.lock:
            self.tasks = tasks

    def save(self) -> None:
        with self.lock:
            keep = [t for t in self.tasks if t.status not in FINISHED]
            finished = [t for t in self.tasks if t.status in FINISHED][-40:]
            data = [t.to_json() for t in self.tasks if t in keep or t in finished]
        try:
            path = _state_file()
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), "utf-8")
            tmp.replace(path)
        except OSError:
            pass

    # -- the interface's verbs -------------------------------------------------

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [t.to_json() for t in self.tasks]

    def find(self, task_id: str) -> Task | None:
        with self.lock:
            return next((t for t in self.tasks if t.id == task_id), None)

    def busy(self) -> bool:
        return self.current is not None

    def add(self, path: str, name: str = "", target_root: str = "", folder_name: str = "",
            mode: str = "", ssd: str = "", size: int = 0, auto: bool = False,
            origin: str = "manual", installer_asks: bool = False,
            skip_redists: bool = False, skip_checkers: bool = False,
            skip_finish: bool = False) -> Task:
        source = str(Path(path).expanduser())
        with self.lock:
            for task in self.tasks:
                if task.path != source:
                    continue
                if task.status in ("checking", "installing"):
                    return task
                if task.status in ("queued", "waiting") and not auto:
                    # Someone pressed Install on something already in the
                    # list, typically one that was waiting for a folder:
                    # what they chose now is what it should do.
                    task.name = name.strip() or task.name
                    task.target_root, task.target_dir = target_root.strip(), ""
                    task.folder_name = folder_name.strip()
                    task.mode, task.ssd_override = mode or task.mode, ssd
                    task.size_override = int(size or 0)
                    task.installer_asks, task.auto = installer_asks, False
                    task.skip_redists, task.skip_checkers = skip_redists, skip_checkers
                    task.skip_finish = skip_finish
                    task.status, task.action, task.warning = "queued", "", ""
                    task.message = "Waiting its turn" if self.current else "Starting"
                    self.save()
                    self.wake.set()
                    return task
                if task.status in ("queued", "waiting"):
                    return task
            task = Task(id=uuid.uuid4().hex[:10], path=source, name=name.strip(),
                        target_root=target_root.strip(), folder_name=folder_name.strip(),
                        mode=mode, auto=auto, origin=origin, ssd_override=ssd,
                        size_override=int(size or 0), installer_asks=installer_asks,
                        skip_redists=skip_redists, skip_checkers=skip_checkers,
                        skip_finish=skip_finish,
                        created=time.time())
            task.message = "Waiting its turn" if self.current else "Starting"
            self.tasks.append(task)
        self.save()
        self.emit("install-queued", f"{task.name or Path(source).name} queued to install",
                  task=task.id, auto=auto)
        self.start()
        self.wake.set()
        return task

    def cancel(self, task_id: str) -> str:
        task = self.find(task_id)
        if not task:
            return "That install is not in the list."
        if task.status in ("queued", "waiting"):
            task.status = "cancelled"
            task.message = "Cancelled."
            task.finished = time.time()
            self.save()
            return ""
        if task.status in ("checking", "installing"):
            task.cancel = True
            task.message = "Stopping…"
            return ""
        return "It has already finished."

    def retry(self, task_id: str, target_root: str = "", mode: str = "") -> Task | None:
        task = self.find(task_id)
        if not task or task.status in ("checking", "installing"):
            return task
        with self.lock:
            task.status = "queued"
            task.cancel = False
            task.progress = -1.0
            task.message = "Waiting its turn" if self.current else "Starting"
            task.action = ""
            task.warning = ""
            task.result = {}
            task.finished = 0.0
            if target_root:
                task.target_root = target_root
                task.target_dir = ""
            if mode:
                task.mode = mode
        self.save()
        self.start()
        self.wake.set()
        return task

    def remove(self, task_id: str) -> None:
        with self.lock:
            self.tasks = [t for t in self.tasks
                          if not (t.id == task_id and t.status not in ("checking", "installing"))]
        self.save()

    def clear_finished(self) -> None:
        with self.lock:
            self.tasks = [t for t in self.tasks if t.status not in FINISHED]
        self.save()

    def retry_waiting(self, action: str) -> None:
        """Requeue everything that was waiting for one thing, now provided."""
        with self.lock:
            waiting = [t for t in self.tasks if t.status == "waiting" and t.action == action]
        for task in waiting:
            self.retry(task.id)

    # -- looking before installing -------------------------------------------

    def analyse(self, path: str) -> dict:
        """Everything the install sheet shows before anything happens."""
        settings = library.load_settings()
        info = installers.inspect(path)
        if info["kind"] == "unsupported":
            return {"item": info, "error": info["reason"] or "This cannot be installed."}
        store = storeinfo.lookup(display_name(info), library.config_dir()) \
            if info["kind"] in ("installer", "archive", "folder") else {"found": False}
        name = display_name(info, store)
        size, size_note = self._size(info, store)
        need = store.get("ssd", "") if store.get("found") else ""
        targets = placement.describe_all(settings.get("library_folders") or [])
        choice = placement.choose(size, need, targets, settings.get("ssd_policy", "strict"),
                                  bool(settings.get("prefer_ssd_recommended")))
        quiet_ok = info["kind"] != "installer" or bool(info.get("quiet"))
        on_windows = winproc.IS_WINDOWS
        if info["kind"] == "installer" and not on_windows:
            return {"item": info, "error": "Windows installers can only run on Windows."}
        return {
            "item": info,
            "name": name,
            "folder_name": placement.safe_folder_name(name),
            "size": size,
            "size_note": size_note,
            "store": store,
            "ssd": need,
            "ssd_note": store.get("evidence", ""),
            "targets": targets,
            "choice": {"target": (choice["target"] or {}).get("path", ""),
                       "notes": choice["notes"], "warning": choice["warning"]},
            "modes": {
                "quiet": quiet_ok,
                "classic": info["kind"] in ("installer", "pkg"),
                "embed": embed.available(),
                "quiet_note": "" if quiet_ok else
                f"{info.get('label') or 'This installer'} is not one whose quiet switches "
                "are known, so it has to be installed through its own screens.",
            },
            "default_mode": ("classic" if info["kind"] in ("installer", "pkg") and
                             (settings.get("install_mode", "classic") == "classic" or not quiet_ok)
                             else "quiet"),
            "admin": winproc.is_admin(),
        }

    @staticmethod
    def _size(info: dict, store: dict) -> tuple[int, str]:
        if store.get("found") and store.get("size"):
            return int(store["size"]), f"the Steam store lists {placement.human(store['size'])}"
        size, note = installers.estimate(info)
        return size, note

    # -- the worker -------------------------------------------------------------

    def start(self) -> None:
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, daemon=True)
                self.thread.start()

    def _next(self) -> Task | None:
        with self.lock:
            return next((t for t in self.tasks if t.status == "queued"), None)

    def _loop(self) -> None:
        while True:
            task = self._next()
            if task is None:
                self.wake.wait(5)
                self.wake.clear()
                continue
            self.current = task
            task.status, task.started, task.cancel = "checking", time.time(), False
            task.log = []
            try:
                self._run(task)
            except Waiting as wait:
                task.status, task.message, task.action = "waiting", str(wait), wait.action
                task.progress = -1.0
                self.emit("install-waiting", f"{task.name or Path(task.path).name}: {wait}",
                          task=task.id)
            except Cancelled:
                task.status, task.message, task.finished = "cancelled", "Cancelled.", time.time()
                self.emit("install-cancelled", f"{task.name} was cancelled", task=task.id)
            except Exception as exc:              # a failure must not stop the queue
                task.status, task.finished = "failed", time.time()
                task.message = str(exc) or type(exc).__name__
                task.log.append(f"Stopped: {task.message}")
                self.emit("install-failed", f"{task.name or Path(task.path).name} did not "
                          f"install: {task.message}", task=task.id)
            finally:
                self.current = None
                self.save()

    def _check(self, task: Task) -> None:
        if task.cancel:
            raise Cancelled()

    def _note(self, task: Task, line: str) -> None:
        task.log.append(line)

    def _run(self, task: Task) -> None:
        settings = library.load_settings()
        task.message = "Looking at the download"
        info = installers.inspect(task.path)
        if info["kind"] == "unsupported":
            raise RuntimeError(info["reason"] or "This cannot be installed.")
        task.kind, task.framework, task.label = info["kind"], info["framework"], info["label"]

        store = {"found": False}
        if info["kind"] in ("installer", "archive", "folder"):
            task.message = "Checking what the game needs"
            store = storeinfo.lookup(task.name or display_name(info), library.config_dir())
        if not task.name:
            task.name = display_name(info, store)
        self._check(task)

        size, note = self._size(info, store)
        if task.size_override:
            size, note = task.size_override, "set by you"
        task.size, task.size_note = size, note
        need = store.get("ssd", "") if store.get("found") else ""
        task.ssd_note = store.get("evidence", "")
        if task.ssd_override in ("required", "any"):
            need = "required" if task.ssd_override == "required" else ""
            task.ssd_note = "set by you"
        task.ssd = need
        if store.get("found"):
            self._note(task, f"Steam store: {store['name']}, "
                             f"{placement.human(store.get('size', 0)) if store.get('size') else 'no size listed'}"
                             f"{', SSD ' + need if need else ''}.")

        mode = task.mode or ("quiet" if task.auto else settings.get("install_mode", "classic"))
        if info["kind"] == "installer":
            if not winproc.IS_WINDOWS:
                raise RuntimeError("Windows installers can only run on Windows.")
            if mode == "quiet" and not info.get("quiet"):
                if task.auto:
                    raise Waiting(f"{info.get('label') or 'Its installer'} cannot install "
                                  "unattended. Press Install to go through its own screens.",
                                  action="classic")
                mode = "classic"
        task.mode = mode

        self._place(task, info, settings)
        self._check(task)

        task.status = "installing"
        self.emit("install-started", f"Installing {task.name}", task=task.id)
        before = pipeline.installed_snapshot() if info["kind"] in ("installer", "pkg") else {}
        target = Path(task.target_dir) if task.target_dir else None
        created = target is not None and not target.exists()
        try:
            if info["kind"] == "archive":
                self._unpack(task, info, target)
            elif info["kind"] == "folder":
                self._copy_tree(task, Path(info["source"]), target)
            elif info["kind"] in ("program", "appimage"):
                self._copy_file(task, Path(info["source"]), target)
            elif info["kind"] == "dmg":
                self._mount_and_copy(task, Path(info["source"]), target)
            elif info["kind"] in ("installer", "pkg"):
                if info["kind"] == "pkg" and sys.platform != "darwin":
                    raise RuntimeError("macOS packages can only be installed on macOS.")
                self._run_installer(task, info, target)
        except (Cancelled, pipeline.Stopped):
            if created and target is not None and info["kind"] not in ("installer", "pkg"):
                shutil.rmtree(target, ignore_errors=True)
            raise Cancelled()
        except Exception:
            if created and target is not None and info["kind"] not in ("installer", "pkg"):
                shutil.rmtree(target, ignore_errors=True)
            raise

        task.progress = 1.0
        task.message = "Finding the game"
        games = self._find_games(task, target, before)
        if not games and info["kind"] in ("installer", "pkg") \
                and task.result.get("exit_ok") is not True and not (
                target is not None and target.is_dir() and any(target.iterdir())):
            # Nothing in the folder and nothing new registered: an installer
            # of unknown make exits the same way whether it finished or was
            # cancelled, so the disk is what tells.
            raise RuntimeError("Nothing new turned up after the installer closed. If it was "
                               "cancelled, that is why; if it installed somewhere else, scan "
                               "for games to find it.")
        task.result["games"] = games
        if not games:
            task.result["note"] = ("Installed, but no game executable was found in it. "
                                   "Use Open folder to look, or scan for games.")
        self._stage(task, games, settings)
        self._tidy(task, info, settings)
        task.status, task.finished = "done", time.time()
        task.message = task.result.get("note") or task.warning or "Installed."
        self.emit("install-done", f"{task.name} is installed", task=task.id,
                  games=[g.get("uid") for g in games if g.get("uid")])

    # -- placement -------------------------------------------------------------

    def _place(self, task: Task, info: dict, settings: dict) -> None:
        if task.target_dir:
            return
        folder_name = placement.safe_folder_name(task.folder_name or task.name)
        targets = placement.describe_all(settings.get("library_folders") or [])

        if task.target_root:
            root = Path(task.target_root).expanduser()
            described = next((t for t in targets
                              if Path(t["path"]).expanduser() == root), None)
            task.target_kind = (described or {}).get("kind", "")
            room = (described or {}).get("free") or drives.space(root)["free"]
            if task.size and room and room < task.size:
                task.warning = (f"{root} has {placement.human(room)} free, which may not be "
                                f"enough for about {placement.human(task.size)}.")
        elif info["kind"] in ("installer", "pkg") and task.mode == "classic" and (
                task.installer_asks or not targets):
            # The installer's own screens ask where, as chosen, or because
            # there is no library to choose from.
            task.notes = ["The installer chooses the folder."]
            return
        else:
            choice = placement.choose(task.size, task.ssd, targets,
                                      settings.get("ssd_policy", "strict"),
                                      bool(settings.get("prefer_ssd_recommended")))
            task.notes = choice["notes"]
            task.warning = choice["warning"]
            if not choice["target"]:
                if not targets:
                    raise Waiting("Add a library folder in the Installs tab, or choose a folder "
                                  "for this one.", action="folder")
                summary = choice["notes"][-1] if choice["notes"] else "No folder has room."
                raise Waiting(f"No library folder can take it. {summary}", action="folder")
            root = Path(choice["target"]["path"]).expanduser()
            task.target_kind = choice["target"]["kind"]
            for line in choice["notes"]:
                self._note(task, line)

        task.target_root = str(root)
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise Waiting(f"{root} cannot be created ({exc.strerror or exc}).", action="folder")
        target = root / folder_name
        if task.auto or not task.folder_name:
            target = _unique(target)
        task.target_dir = str(target)
        self._note(task, f"Installing into {target}")

    # -- the methods -------------------------------------------------------------

    def _progress(self, task: Task):
        def report(fraction: float, name: str = "") -> None:
            task.progress = max(0.0, min(float(fraction), 1.0))
            if name:
                task.message = f"Unpacking {Path(name).name}"
        return report

    def _unpack(self, task: Task, info: dict, target: Path) -> None:
        task.message = "Unpacking"
        task.progress = 0.0
        try:
            pipeline.unpack(info["source"], into=str(target), keep_archive=True,
                            progress=self._progress(task), flatten=True,
                            stop=lambda: task.cancel)
        except pipeline.NeedsSevenZip as exc:
            if tools.can_fetch():
                raise Waiting("This archive needs 7-Zip. Get it from the Installs tab and "
                              "this continues by itself.", action="sevenzip") from exc
            raise RuntimeError(f"{exc} Install p7zip (7-Zip) with your package manager.") from exc

    def _copy_tree(self, task: Task, source: Path, target: Path) -> None:
        total = installers.folder_size(source) or 1
        done = 0
        task.message = "Copying"
        for current, dirs, files in os.walk(source):
            here = Path(current)
            relative = here.relative_to(source)
            (target / relative).mkdir(parents=True, exist_ok=True)
            for name in files:
                self._check(task)
                origin = here / name
                shutil.copy2(origin, target / relative / name)
                try:
                    done += origin.stat().st_size
                except OSError:
                    pass
                task.progress = min(done / total, 1.0)
                task.message = f"Copying {name}"
        pipeline.hoist(target)

    def _copy_file(self, task: Task, source: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        destination = target / source.name
        task.message = f"Copying {source.name}"
        shutil.copy2(source, destination)
        if task.kind == "appimage":
            destination.chmod(destination.stat().st_mode | 0o111)

    def _mount_and_copy(self, task: Task, image: Path, target: Path) -> None:
        import plistlib
        from . import proc

        if not hasattr(os, "uname") or os.uname().sysname != "Darwin":
            raise RuntimeError("Disk images can only be installed on macOS.")
        task.message = "Opening the disk image"
        # "Y" answers a licence prompt, which would otherwise wait forever.
        attached = proc.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-plist", str(image)],
                            input=b"Y\n", capture_output=True, timeout=300)
        if attached.returncode != 0:
            raise RuntimeError("macOS could not open the disk image.")
        mounts = [e.get("mount-point") for e in plistlib.loads(attached.stdout).get(
            "system-entities", []) if e.get("mount-point")]
        try:
            apps = [p for m in mounts for p in Path(m).glob("*.app")]
            if not apps:
                raise RuntimeError("The disk image holds no application.")
            target.mkdir(parents=True, exist_ok=True)
            for app in apps:
                task.message = f"Copying {app.name}"
                shutil.copytree(app, target / app.name, symlinks=True, dirs_exist_ok=True)
        finally:
            for mount in mounts:
                proc.run(["hdiutil", "detach", mount, "-quiet"], capture_output=True, timeout=120)

    def _run_installer(self, task: Task, info: dict, target: Path | None) -> None:
        log = ""
        if info["kind"] == "pkg":
            command = {"file": "open", "params": "", "elevate": False}
            argv = ["open", "-W", info["source"]]
        else:
            argv = None
            if target is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
            run_target = str(target) if target is not None else ""
            if info["kind"] == "installer" and os.name == "nt":
                info = self._plain_paths(task, info, target)
                run_target = info.get("run_target") or run_target
                self._wait_for_data(task, Path(info["source"]).parent)
            if task.mode == "quiet":
                log = str(library.config_dir() / f"install-{task.id}.log")
                command = installers.quiet_command(info, run_target, log)
            else:
                command = installers.classic_command(info, run_target)

        task.message = "Waiting for the current program to finish"
        with PROGRAM_LOCK:
            self._check(task)
            task.message = ("Installing quietly" if task.mode == "quiet"
                            else "The installer is open; answer it here")
            self._note(task, f"Running {Path(info['source']).name}"
                             f"{' ' + command['params'] if command['params'] else ''}")
            desktop = winclean.DesktopWatch(enabled=_setting("no_desktop_shortcuts"))
            try:
                launched = winproc.launch(command["file"], command["params"],
                                          cwd=str(Path(info["source"]).parent),
                                          elevate=command["elevate"], argv=argv)
            except winproc.LaunchError as exc:
                raise RuntimeError(str(exc)) from exc
            tree = winproc.ProcessTree(launched)
            session = embed.MANAGER.start(embed.Session(
                tree, f"Installing {task.name}",
                kind="quiet" if task.mode == "quiet" else "classic", task_id=task.id))
            task.result["session"] = session.id
            # Repack installers like to play music while they unpack.
            muter = winaudio.mute_while(tree, spare=str(target) if target else "",
                                        enabled=_mute_installers())
            last_size = 0.0
            handled: set = set()
            closed_at_finish = False
            finish_seen = 0.0
            finish_pressed_at = 0.0
            untick_tried = False
            temp_before = winclean.inno_temp_dirs()
            try:
                while tree.alive():
                    self._stop_side_programs(task, tree, handled)
                    if task.skip_finish and not closed_at_finish \
                            and embed.finished_page(tree.pids()):
                        # Seen twice in a row a moment apart, so a page that
                        # only flashes past is left alone.
                        if finish_seen and time.time() - finish_seen > 1.0:
                            closed_at_finish, untick_tried = self._close_at_finish(
                                task, tree, untick_tried)
                            finish_pressed_at = time.time()
                        finish_seen = finish_seen or time.time()
                    else:
                        finish_seen = 0.0
                    if closed_at_finish and tree.alive() and \
                            time.time() - finish_pressed_at > 10:
                        # The wizard is gone but something it started lingers
                        # (setup.exe tidying up, a console): done anyway.
                        tree.terminate()
                        break
                    if task.cancel:
                        stopped, why = tree.terminate()
                        if not stopped:
                            task.message = why
                            task.cancel = False
                    if target is not None and task.size and time.time() - last_size > 2:
                        last_size = time.time()
                        written = installers.folder_size(target) if target.exists() else 0
                        task.progress = min(written / task.size, 0.99) if written else -1.0
                    if log:
                        current = self._last_file(log)
                        if current:
                            task.message = f"Installing {current}"
                    time.sleep(0.5)
            finally:
                if muter is not None:
                    muter.stop()
                embed.MANAGER.finish(session)
                code = tree.exit_code()
                ran = sorted({n for n in tree.names.values() if n})
                tree.close()
                if ran:
                    self._note(task, "Programs it started: " + ", ".join(ran) + ".")
                if closed_at_finish:
                    # The wizard was stopped before it could delete its own
                    # temporary folder (its unpacking tools, a few MB).
                    for left in winclean.remove_new_inno_temp(temp_before):
                        self._note(task, f"Removed the installer's leftover folder {left}.")
                for shortcut in desktop.tidy():
                    self._note(task, f"Removed the desktop shortcut {Path(shortcut).name}.")
        if task.cancel:
            raise Cancelled()
        if target is not None and _setting("clear_run_as_admin"):
            # Many repacks tick "run as administrator" on the game, which then
            # asks for permission at every start: a prompt Big Picture hides.
            for program in winclean.clear_run_as_admin(str(target)):
                self._note(task, f"{Path(program).name} no longer asks for administrator rights.")

        if closed_at_finish:
            code = 0                 # it had finished; only its last page was skipped
        ok, said = installers.interpret(task.framework, code)
        self._note(task, f"The installer exited{'' if code is None else f' with code {code}'}.")
        if said:
            self._note(task, said)
        has_files = target is not None and target.exists() and any(target.iterdir())
        task.result["exit_ok"] = ok
        if ok is False and "cancelled" in said.lower():
            raise Cancelled()
        if ok is False:
            raise RuntimeError(said)
        if ok is None and target is not None and not has_files and task.mode == "quiet":
            raise RuntimeError("The installer closed without putting anything in the folder. "
                               "Try again through its own screens.")
        if said:
            task.warning = said

    def _plain_paths(self, task: Task, info: dict, target: Path | None) -> dict:
        """Give a setup program paths it can open.

        Repack unpackers (ISDone with unarc.dll) cannot read or write a path
        with characters outside plain ASCII, such as a user name with an
        accent. Windows' short 8.3 names spell any path in ASCII, so the
        setup program is started, and told to install, through those.
        """
        info = dict(info)
        source = str(info["source"])
        if not source.isascii():
            short = winproc.short_path(source)
            if short != source and short.isascii():
                info["source"] = short
                self._note(task, f"Starting the installer as {short}, a path it can read.")
        if target is not None and not str(target).isascii():
            target.mkdir(parents=True, exist_ok=True)
            short = winproc.short_path(str(target))
            if short != str(target) and short.isascii():
                info["run_target"] = short
                self._note(task, f"Installing through {short}, a path the installer can write.")
        if not str(info["source"]).isascii():
            task.warning = ("The installer's folder has letters outside plain English in its "
                            "path; some repack installers cannot read their files there. If it "
                            "fails, pick a download folder with a plain name.")
        return info

    def _wait_for_data(self, task: Task, folder: Path, patience: float = 90) -> None:
        """Wait until the setup program's data files can be read.

        Right after a download, a file can still be held open by the program
        that wrote it or by a virus scanner, and a setup program that cannot
        open its data stops with "can't open fg-01.bin".
        """
        data = [p for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() in (".bin", ".cab", ".dat")]
        deadline = time.time() + patience
        for item in data:
            while True:
                try:
                    with open(item, "rb") as handle:
                        handle.read(1)
                    break
                except OSError as exc:
                    if time.time() > deadline:
                        raise RuntimeError(f"{item.name} cannot be opened ({exc.strerror or exc}); "
                                           "something still has it open, or it was removed.") \
                            from exc
                    task.message = f"Waiting for {item.name} to be free"
                    self._check(task)
                    time.sleep(2)

    def _close_at_finish(self, task: Task, tree, untick_tried: bool) -> tuple[bool, bool]:
        """The installer is on its last page: close it without running anything more.

        The install is complete by then. Pressing Finish (or closing the
        window, which Inno Setup treats the same) runs the ticked boxes, and
        repacks also open their website from code that runs whenever the
        installer exits, ticked or not. So the wizard is stopped outright,
        the way Task Manager would: nothing it would still do runs.

        That needs the same rights as the installer. Without them, the boxes
        are unticked and Finish pressed instead, which stops the boxes but
        not the website. Returns (closed, untick_tried).
        """
        owner = embed.finish_owner(tree.pids())
        if owner and tree.stop_branch(owner):
            name = tree.names.get(owner) or "the installer"
            self._note(task, f"Last page reached: closed {name} there, so nothing it runs "
                             "on Finish or on exit (ticked boxes, its website) runs.")
            return True, untick_tried
        if untick_tried:
            return False, True
        done = embed.finish_unticked(tree.pids())
        if not done["found"]:
            return False, True
        names = ", ".join(done["unticked"]) or "nothing was ticked"
        self._note(task, f"Last page: could not close the installer (it runs as administrator "
                         f"and this app does not), so unticked {names} and pressed Finish.")
        task.warning = ("The installer runs as administrator and this app does not, so it could "
                        "only untick its last page, not close it: it may still open its website. "
                        "Turn on Run as administrator in the store's Settings (or Sunshine's "
                        "Elevated option) so installers are closed on their last page.")
        return True, True

    def _stop_side_programs(self, task: Task, tree, handled: set) -> None:
        """Stop the extras an installer starts, when this install should skip them."""
        if not (task.skip_redists or task.skip_checkers or task.skip_finish):
            return
        for pid in tree.pids():
            if pid in handled or pid == tree.root:
                continue
            kind = side_program(tree.names.get(pid, ""))
            if not kind or (kind == "redist" and not task.skip_redists) \
                    or (kind == "checker" and not task.skip_checkers) \
                    or (kind == "browser" and not task.skip_finish):
                continue
            handled.add(pid)
            what = {"checker": "file check", "browser": "website"}.get(kind, "extra setup")
            name = tree.names.get(pid, "")
            if tree.stop_branch(pid):
                self._note(task, f"Skipped the {what} {name}.")
            else:
                self._note(task, f"Could not stop the {what} {name}: it runs as administrator "
                                 "and this app does not.")
                task.warning = (f"The installer opened {name}, which this app cannot close "
                                "because it runs as administrator. Turn on Run as administrator "
                                "to have extras skipped.")

    @staticmethod
    def _last_file(log: str) -> str:
        """The file Inno Setup most recently wrote, from its log."""
        try:
            with open(log, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 4096))
                tail = handle.read().decode("utf-8", "replace")
        except OSError:
            return ""
        for line in reversed(tail.splitlines()):
            if "Dest filename:" in line:
                return Path(line.split("Dest filename:", 1)[1].strip()).name
        return ""

    # -- afterwards ------------------------------------------------------------

    def _find_games(self, task: Task, target: Path | None, before: dict) -> list[dict]:
        games: list[dict] = []
        if target is not None and target.is_dir():
            exe = scanners.pick_executable(target, max_depth=4)
            if exe:
                games.append(scanners.make_game(task.name, "installed", exe=str(exe),
                                                tags=["Installed"], sgdb_hint=task.name,
                                                detail=str(target)))
        if not games and task.kind in ("installer", "pkg"):
            # An installer that ignored the folder it was given, or one run
            # without a library: whatever it registered with the system.
            for game in pipeline.newly_installed(before):
                game = dict(game)
                game["tags"] = list(game.get("tags") or []) + ["Installed"]
                games.append(game)
                if not task.target_dir:
                    task.target_dir = game.get("start_dir", "")
        for game in games:
            game["uid"] = uuid.uuid4().hex[:12]
        return games

    def _stage(self, task: Task, games: list[dict], settings: dict) -> None:
        if not games or task.origin == "store":
            # The store names, dresses and adds its own games, and keeps
            # them for a Steam restart when Steam is open.
            return
        if self.on_staged:
            try:
                self.on_staged(games)
            except Exception as exc:
                self._note(task, f"Could not put it on the shelf: {exc}")
        if not settings.get("auto_add_to_steam"):
            return
        if steam.steam_is_running():
            task.result["steam"] = "Steam was open, so it is waiting on the Add games shelf."
            return
        root = steam.find_steam_root()
        users = steam.list_users(root) if root else []
        wanted = str(settings.get("account") or "")
        user = next((u for u in users if u.id3 == wanted), users[0] if users else None)
        if not user:
            task.result["steam"] = "No Steam account was found to add it to."
            return
        task.message = "Adding to Steam"
        job = library.Job(id=uuid.uuid4().hex[:12])
        try:
            library.apply_games(job, user, games, settings)
            task.result["steam"] = f"Added to Steam for {user.name}."
            task.result["added_to_steam"] = True
        except Exception as exc:
            task.result["steam"] = f"Could not add it to Steam: {exc}"
        for line in job.log[-5:]:
            self._note(task, line)

    def _tidy(self, task: Task, info: dict, settings: dict) -> None:
        """Delete the download after a good install, when asked to."""
        if not settings.get("delete_after_install") or not task.result.get("games"):
            return
        from . import download

        source = Path(task.path)
        # Only ever inside the download folders, never something picked
        # from elsewhere on the disk.
        roots = [settings.get("incoming_folder", ""), str(download.default_folder())]
        if not _within(source, roots):
            return
        doomed = [source]
        if source.is_file() and info["kind"] == "installer":
            doomed = installers.parts_of(source)
        for path in doomed:
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError as exc:
                self._note(task, f"Could not delete {path.name}: {exc.strerror or exc}")
                return
        self._note(task, "Deleted the download.")


QUEUE = Queue()


# --------------------------------------------------------------------------
# watching the finished-downloads folder
# --------------------------------------------------------------------------

class Watcher:
    """Notices downloads finishing, and queues them when auto-install is on.

    Lives in the server so it works whether or not a page is open, and
    remembers what it has seen across restarts: a download that finished
    while the app was closed is still new when it next starts.
    """

    INTERVAL = 15

    def __init__(self, queue: Queue):
        self.queue = queue
        self.previous: dict = {}
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.poke = threading.Event()

    def _load_seen(self) -> dict:
        try:
            data = json.loads(_seen_file().read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_seen(self, data: dict) -> None:
        try:
            tmp = _seen_file().with_suffix(".tmp")
            tmp.write_text(json.dumps(data), "utf-8")
            tmp.replace(_seen_file())
        except OSError:
            pass

    def rebaseline(self, folder: str = "") -> int:
        """Treat everything settled in the folder now as already seen.

        Called when automatic installing is switched on, so that it means
        "from now on" rather than "everything in the folder".
        """
        folder = folder or str(library.load_settings().get("incoming_folder") or "")
        with self.lock:
            current = pipeline.sample(folder) if folder else {}
            seen = {path: info["size"] for path, info in current.items() if info["settled"]}
            self._save_seen({"folder": folder, "items": seen})
            self.previous = current
        return len(seen)

    def start(self) -> None:
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()

    def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                pass
            self.poke.wait(self.INTERVAL)
            self.poke.clear()

    def tick(self) -> list[str]:
        settings = library.load_settings()
        folder = str(settings.get("incoming_folder") or "")
        auto = bool(settings.get("auto_install"))
        if not folder or not (auto or settings.get("watch_incoming")):
            self.previous = {}
            return []
        seen = self._load_seen()
        if seen.get("folder") != folder:
            # A folder never watched before: what is in it now is the
            # starting point, not a list of things to install.
            self.rebaseline(folder)
            return []
        with self.lock:
            items = seen.get("items") or {}
            current = pipeline.sample(folder)
            finished = pipeline.finished_since(self.previous, current)
            self.previous = current
            fresh = [p for p in finished if items.get(p) != current[p]["size"]]
            for path in fresh:
                items[path] = current[path]["size"]
            if fresh or any(p not in current for p in items):
                items = {p: size for p, size in items.items() if p in current}
                self._save_seen({"folder": folder, "items": items})

        for path in fresh:
            info = installers.inspect(path)
            name = display_name(info)
            installable = info["kind"] not in ("unsupported",)
            if auto and installable:
                self.queue.add(path, auto=True, origin="incoming")
                self.queue.emit("download-finished", f"{name} finished downloading and is "
                                "being installed", path=path, auto=True, installable=True)
            else:
                self.queue.emit("download-finished", f"{name} finished downloading",
                                path=path, auto=False, installable=installable,
                                kind=info["kind"])
        return fresh

    def backlog(self) -> list[dict]:
        """Installable items in the folder that have not been installed."""
        settings = library.load_settings()
        folder = str(settings.get("incoming_folder") or "")
        if not folder:
            return []
        try:
            items = pipeline.survey(folder)
        except pipeline.PipelineError:
            return []
        handled = {t.path for t in self.queue.tasks if t.status not in ("cancelled", "failed")}
        out = []
        for item in items:
            if item["path"] in handled or item["state"] == "image":
                continue
            if item["state"] == "unknown":
                continue
            out.append(item)
        return out


WATCHER = Watcher(QUEUE)


# --------------------------------------------------------------------------
# uninstallers go through the same window handling
# --------------------------------------------------------------------------

def run_uninstaller(program: dict, silent: bool = False, timeout: int = 3600) -> dict:
    """Run a program's uninstaller, its window held inside the app.

    Waits for every process the uninstaller starts, which matters: Inno
    Setup's uninstaller copies itself elsewhere and exits immediately, so
    waiting for the first process alone checked the registry long before
    anything had been removed.
    """
    if not uninstall.IS_WINDOWS:
        return {"ok": False, "message": "Uninstalling is only supported on Windows."}
    command = uninstall.uninstall_command(program, silent)
    if not command:
        return {"ok": False, "message": "This program did not register an uninstaller."}
    file, params = winproc.split_command(command)
    started = time.time()
    with PROGRAM_LOCK:
        try:
            launched = winproc.launch(file, params, elevate=silent and "msiexec" in file.lower())
        except winproc.LaunchError as exc:
            return {"ok": False, "message": str(exc)}
        tree = winproc.ProcessTree(launched)
        session = embed.MANAGER.start(embed.Session(
            tree, f"Uninstalling {program.get('name', '')}",
            kind="quiet" if silent else "classic"))
        muter = winaudio.mute_while(tree, enabled=_mute_installers())
        try:
            finished = tree.wait(timeout=timeout)
        finally:
            if muter is not None:
                muter.stop()
            embed.MANAGER.finish(session)
            code = tree.exit_code()
            tree.close()
    if not finished:
        return {"ok": False, "message": "The uninstaller is still running; leaving it alone."}
    gone = not uninstall.still_installed(program)
    return {
        "ok": gone,
        "code": code,
        "seconds": round(time.time() - started, 1),
        "message": (
            "Uninstalled." if gone else
            "The uninstaller closed but the program is still registered. "
            "It may have been cancelled, or it may need a restart to finish."
        ),
    }
