"""Start programs the way Explorer does, and follow everything they start.

Two things make installers awkward to run from another program on
Windows:

* An installer that asks for administrator rights cannot be started with
  a plain CreateProcess; Windows refuses with "elevation required". The
  shell's ShellExecuteEx is what shows the permission prompt and starts
  it elevated, exactly as double-clicking would.
* The program started is often not the one doing the work. Inno Setup's
  ``setup.exe`` unpacks and runs ``setup.tmp``; its uninstaller copies
  itself to a temporary folder, starts the copy and exits at once. Waiting
  for the first process alone reports "finished" while the real work has
  barely begun.

So a launch is followed as a tree: every process whose parent is already
being followed joins it, and the install is over when the whole tree has
gone. Parents are matched by process id *and* start time, so a recycled id
cannot pull an unrelated program into the tree.

Elsewhere programs are started with subprocess, and only the process
started is followed; there are no Windows installers to run there.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

IS_WINDOWS = os.name == "nt"


class LaunchError(RuntimeError):
    pass


if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    class _SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    _shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
    _shell32.ShellExecuteExW.restype = wintypes.BOOL
    _shell32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                       wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
    _shell32.ShellExecuteW.restype = wintypes.HINSTANCE
    _shell32.IsUserAnAdmin.argtypes = []
    _shell32.IsUserAnAdmin.restype = wintypes.BOOL

    _kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
    _kernel32.GetProcessId.restype = wintypes.DWORD
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32NextW.restype = wintypes.BOOL

    _INVALID = ctypes.c_void_p(-1).value
    _SEE_MASK_NOCLOSEPROCESS = 0x00000040
    _SEE_MASK_NOASYNC = 0x00000100
    _SEE_MASK_FLAG_NO_UI = 0x00000400
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _TH32CS_SNAPPROCESS = 0x00000002
    _ERROR_CANCELLED = 1223


def is_admin() -> bool:
    """Whether this process already has administrator rights."""
    if IS_WINDOWS:
        try:
            return bool(_shell32.IsUserAnAdmin())
        except OSError:
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


# --------------------------------------------------------------------------
# starting a program
# --------------------------------------------------------------------------

class Launched:
    """A started program: its process id and whatever can be waited on."""

    def __init__(self, pid: int, handle=None, popen=None, label: str = ""):
        self.pid = int(pid or 0)
        self.handle = handle
        self.popen = popen
        self.label = label


def launch(file: str, params: str = "", cwd: str | None = None, elevate: bool = False,
           argv: list | None = None) -> Launched:
    """Start ``file`` with ``params`` as Explorer would.

    ``elevate`` asks for administrator rights up front. Without it a
    program that needs them still gets the prompt, because the shell reads
    its manifest; with it the prompt appears even for programs that would
    only fail later, such as quiet Windows Installer packages.
    """
    if not IS_WINDOWS:
        command = argv or ([file] + (shlex.split(params) if params else []))
        try:
            popen = subprocess.Popen(command, cwd=cwd or None)
        except OSError as exc:
            raise LaunchError(f"Could not start {Path(file).name}: {exc}") from exc
        return Launched(popen.pid, popen=popen, label=Path(file).name)

    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS | _SEE_MASK_NOASYNC | _SEE_MASK_FLAG_NO_UI
    info.hwnd = None
    info.lpVerb = "runas" if elevate else None
    info.lpFile = str(file)
    info.lpParameters = params or None
    info.lpDirectory = cwd or (str(Path(file).parent) if Path(file).is_absolute() else None)
    info.nShow = 1                      # SW_SHOWNORMAL
    if not _shell32.ShellExecuteExW(ctypes.byref(info)):
        code = ctypes.get_last_error()
        if code == _ERROR_CANCELLED:
            raise LaunchError("The Windows permission prompt was declined, so it did not start.")
        raise LaunchError(f"Could not start {Path(file).name} ({ctypes.FormatError(code).strip()}).")
    handle = info.hProcess
    pid = _kernel32.GetProcessId(handle) if handle else 0
    return Launched(pid, handle=handle, label=Path(file).name)


def split_command(command: str) -> tuple[str, str]:
    """Split a stored command line into the program and its arguments.

    Uninstall entries hold whole command lines, quoted or not, and an
    unquoted one may contain spaces ("C:\\Program Files\\X\\uninst.exe /S").
    The shortest prefix naming a file that exists is taken as the program.
    """
    text = str(command or "").strip()
    if not text:
        return "", ""
    if text.startswith('"'):
        end = text.find('"', 1)
        if end > 0:
            return text[1:end], text[end + 1:].strip()
    lower = text.lower()
    for marker in (".exe", ".bat", ".cmd", ".com"):
        start = 0
        while True:
            index = lower.find(marker, start)
            if index < 0:
                break
            candidate = text[:index + len(marker)]
            rest = text[index + len(marker):]
            if (not rest or rest[0] == " ") and (Path(candidate).exists() or "\\" not in candidate):
                return candidate, rest.strip()
            start = index + 1
    head, _, tail = text.partition(" ")
    return head, tail.strip()


# --------------------------------------------------------------------------
# following what it starts
# --------------------------------------------------------------------------

def _snapshot() -> dict[int, tuple[int, str]]:
    """Every process: pid -> (parent pid, executable name)."""
    out: dict[int, tuple[int, str]] = {}
    snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID:
        return out
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), entry.szExeFile)
            ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snap)
    return out


def _created(pid: int) -> int:
    """When a process started, as a FILETIME count; 0 if it cannot be read."""
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return 0
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not _kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                         ctypes.byref(kernel), ctypes.byref(user)):
            return 0
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        _kernel32.CloseHandle(handle)


class ProcessTree:
    """A launched program and everything it starts, followed until all exit."""

    def __init__(self, launched: Launched):
        self.launched = launched
        self.root = launched.pid
        self.lock = threading.Lock()
        self.known: dict[int, int] = {}       # pid -> creation time
        self.names: dict[int, str] = {}
        self.live: set[int] = set()
        self.started = time.time()
        self._exit_code = None
        if self.root:
            self.known[self.root] = _created(self.root) if IS_WINDOWS else 0
            self.live.add(self.root)

    def refresh(self) -> set[int]:
        """Pick up new children and drop the processes that have exited."""
        if not IS_WINDOWS:
            popen = self.launched.popen
            with self.lock:
                if popen is not None and popen.poll() is not None:
                    self._exit_code = popen.returncode
                    self.live = set()
                return set(self.live)

        table = _snapshot()
        with self.lock:
            grew = True
            while grew:                      # grandchildren join in the same pass
                grew = False
                for pid, (parent, name) in table.items():
                    if pid in self.known or parent not in self.known:
                        continue
                    born = _created(pid)
                    # A child cannot predate its parent; if it does, the
                    # parent id was recycled and this is someone else's.
                    if born and self.known[parent] and born < self.known[parent]:
                        continue
                    self.known[pid] = born
                    self.names[pid] = name
                    grew = True
            live = set()
            for pid, born in self.known.items():
                if pid not in table:
                    continue
                if born and _created(pid) not in (0, born):
                    continue                 # same id, different process
                live.add(pid)
            self.live = live
            if self.root not in live:
                self._read_exit_code()
            return set(live)

    def alive(self) -> bool:
        return bool(self.refresh())

    def pids(self) -> set[int]:
        with self.lock:
            return set(self.live)

    def _read_exit_code(self) -> None:
        handle = self.launched.handle
        if not IS_WINDOWS or not handle or self._exit_code is not None:
            return
        code = wintypes.DWORD(0)
        if _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != _STILL_ACTIVE:
            # Exit codes are unsigned here but often negative by intent.
            value = code.value
            self._exit_code = value - (1 << 32) if value >= (1 << 31) else value

    def exit_code(self):
        """The first program's exit code, once it has exited."""
        with self.lock:
            if self._exit_code is None and IS_WINDOWS:
                self._read_exit_code()
            return self._exit_code

    def wait(self, timeout: float | None = None, poll: float = 0.5, stop=None) -> bool:
        """Block until every process in the tree has exited. True if they did."""
        deadline = time.time() + timeout if timeout else None
        while self.alive():
            if stop is not None and stop():
                return False
            if deadline and time.time() > deadline:
                return False
            time.sleep(poll)
        return True

    def terminate(self) -> tuple[bool, str]:
        """Stop everything in the tree. Fails for programs running as administrator."""
        if not IS_WINDOWS:
            popen = self.launched.popen
            if popen is not None and popen.poll() is None:
                popen.terminate()
            return True, ""
        refused = 0
        for pid in sorted(self.refresh(), reverse=True):
            handle = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
            if not handle:
                refused += 1
                continue
            try:
                if not _kernel32.TerminateProcess(handle, 1):
                    refused += 1
            finally:
                _kernel32.CloseHandle(handle)
        if refused:
            return False, ("It runs with administrator rights, so only its own window can "
                           "stop it. Use its Cancel button.")
        return True, ""

    def close(self) -> None:
        handle = self.launched.handle
        if IS_WINDOWS and handle:
            _kernel32.CloseHandle(handle)
            self.launched.handle = None


# --------------------------------------------------------------------------
# restarting with administrator rights
# --------------------------------------------------------------------------

def relaunch_elevated(extra: list | None = None) -> bool:
    """Start a second copy of this program with administrator rights.

    Returns True if it started, so the caller can exit. A source run is
    restarted through ``-c`` with the package folder put on the path
    explicitly, because an elevated process does not inherit this one's
    environment, and with it the PYTHONPATH the launcher scripts set.
    """
    if not IS_WINDOWS:
        return False
    args = list(sys.argv[1:]) + list(extra or [])
    if getattr(sys, "frozen", False):
        program = sys.executable
        params = subprocess.list2cmdline(args)
    else:
        program = sys.executable
        windowless = Path(sys.executable).with_name("pythonw.exe")
        if windowless.exists():
            program = str(windowless)
        package_root = str(Path(__file__).resolve().parent.parent)
        bootstrap = (f"import sys; sys.path.insert(0, {package_root!r}); "
                     "from nsgm.__main__ import main; sys.exit(main())")
        params = subprocess.list2cmdline(["-c", bootstrap] + args)
    result = _shell32.ShellExecuteW(None, "runas", program, params, os.getcwd(), 1)
    return int(result or 0) > 32
