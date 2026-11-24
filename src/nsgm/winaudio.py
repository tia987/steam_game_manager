"""Silence an installer's music while it runs, on Windows.

Many repack installers play music while they unpack. Over Moonlight, from
the sofa, that is just noise. Windows keeps a volume and a mute switch for
every program that plays sound - the per-app sliders in the volume mixer -
and this flips the switch for the installer's programs, the same way the
mixer does, without touching the system volume or anything else.

Only the installer's own programs are muted: the setup program and what
it starts, never anything inside the folder the game is being installed
to, so a game launched at the end of a setup keeps its sound. Whatever is
still running when the install ends is unmuted again.

It is done with Windows' Core Audio interfaces through ctypes, so it needs
nothing installed. Any failure simply leaves the sound on: muting is a
nicety and must never get in the way of an install.
"""

from __future__ import annotations

import os
import threading

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _ole32 = ctypes.WinDLL("ole32")
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        def __init__(self, text: str):
            super().__init__()
            _ole32.CLSIDFromString(ctypes.c_wchar_p("{" + text + "}"), ctypes.byref(self))

    _ole32.CLSIDFromString.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(GUID)]
    _ole32.CLSIDFromString.restype = ctypes.c_long
    _ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    _ole32.CoInitializeEx.restype = ctypes.c_long
    _ole32.CoUninitialize.argtypes = []
    _ole32.CoUninitialize.restype = None
    _ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    _ole32.CoCreateInstance.restype = ctypes.c_long
    _ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    _ole32.CoTaskMemFree.restype = None
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                     wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD)]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    CLSID_MMDeviceEnumerator = "BCDE0395-E52F-467C-8E3D-C4579291692E"
    IID_IMMDeviceEnumerator = "A95664D2-9614-4F35-A746-DE8DB63617E6"
    IID_IAudioSessionManager2 = "77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"
    IID_IAudioSessionControl2 = "BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D"
    IID_ISimpleAudioVolume = "87CE5498-68D6-44E5-9215-6DA47EF883D8"

    CLSCTX_ALL = 0x17
    COINIT_MULTITHREADED = 0x0
    E_RENDER = 0
    DEVICE_STATE_ACTIVE = 0x1
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _call(pointer, index: int, *args, argtypes=(), restype=None):
    """Call method ``index`` of a COM interface through its table of methods."""
    restype = restype or ctypes.c_long
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    function = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(table[index])
    return function(pointer, *args)


def _release(pointer) -> None:
    if pointer:
        try:
            _call(pointer, 2, restype=ctypes.c_ulong)          # IUnknown::Release
        except OSError:
            pass


def _query(pointer, iid: str):
    """IUnknown::QueryInterface, or None."""
    found = ctypes.c_void_p()
    guid = GUID(iid)
    result = _call(pointer, 0, ctypes.byref(guid), ctypes.byref(found),
                   argtypes=(ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)))
    return found if result == 0 and found.value else None


def image_path(pid: int) -> str:
    """The full path of a running program, or "" if it cannot be read."""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        _kernel32.CloseHandle(handle)


def _sessions():
    """(process id, ISimpleAudioVolume) for every program playing sound, on every output.

    The caller releases each volume interface.
    """
    enumerator = ctypes.c_void_p()
    if _ole32.CoCreateInstance(ctypes.byref(GUID(CLSID_MMDeviceEnumerator)), None, CLSCTX_ALL,
                               ctypes.byref(GUID(IID_IMMDeviceEnumerator)),
                               ctypes.byref(enumerator)) != 0:
        return
    try:
        collection = ctypes.c_void_p()
        # IMMDeviceEnumerator::EnumAudioEndpoints
        if _call(enumerator, 3, E_RENDER, DEVICE_STATE_ACTIVE, ctypes.byref(collection),
                 argtypes=(ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))) != 0:
            return
        try:
            count = wintypes.UINT(0)
            _call(collection, 3, ctypes.byref(count),                     # GetCount
                  argtypes=(ctypes.POINTER(wintypes.UINT),))
            for index in range(count.value):
                device = ctypes.c_void_p()
                if _call(collection, 4, index, ctypes.byref(device),      # Item
                         argtypes=(wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))) != 0:
                    continue
                try:
                    yield from _device_sessions(device)
                finally:
                    _release(device)
        finally:
            _release(collection)
    finally:
        _release(enumerator)


def _device_sessions(device):
    manager = ctypes.c_void_p()
    # IMMDevice::Activate(IID_IAudioSessionManager2)
    if _call(device, 3, ctypes.byref(GUID(IID_IAudioSessionManager2)), CLSCTX_ALL, None,
             ctypes.byref(manager),
             argtypes=(ctypes.POINTER(GUID), wintypes.DWORD, ctypes.c_void_p,
                       ctypes.POINTER(ctypes.c_void_p))) != 0:
        return
    try:
        listing = ctypes.c_void_p()
        # IAudioSessionManager2::GetSessionEnumerator
        if _call(manager, 5, ctypes.byref(listing),
                 argtypes=(ctypes.POINTER(ctypes.c_void_p),)) != 0:
            return
        try:
            total = ctypes.c_int(0)
            _call(listing, 3, ctypes.byref(total), argtypes=(ctypes.POINTER(ctypes.c_int),))
            for number in range(total.value):
                control = ctypes.c_void_p()
                if _call(listing, 4, number, ctypes.byref(control),        # GetSession
                         argtypes=(ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))) != 0:
                    continue
                try:
                    control2 = _query(control, IID_IAudioSessionControl2)
                    if control2 is None:
                        continue
                    try:
                        pid = wintypes.DWORD(0)
                        # IAudioSessionControl2::GetProcessId
                        _call(control2, 14, ctypes.byref(pid),
                              argtypes=(ctypes.POINTER(wintypes.DWORD),))
                    finally:
                        _release(control2)
                    if not pid.value:
                        continue
                    volume = _query(control, IID_ISimpleAudioVolume)
                    if volume is not None:
                        yield pid.value, volume
                finally:
                    _release(control)
        finally:
            _release(listing)
    finally:
        _release(manager)


def _set_mute(volume, muted: bool) -> None:
    # ISimpleAudioVolume::SetMute(BOOL, LPCGUID)
    _call(volume, 5, 1 if muted else 0, None, argtypes=(wintypes.BOOL, ctypes.c_void_p))


class Muter:
    """Keeps a set of programs muted while they run.

    ``pids()`` gives the programs to mute, and is asked again every second
    or so, because an installer starts helpers as it goes and a program's
    sound only appears once it starts playing. ``spare`` is a folder whose
    programs are never muted: the game's own.
    """

    INTERVAL = 1.0

    def __init__(self, pids, spare: str = ""):
        self.pids = pids
        self.spare = os.path.normcase(os.path.abspath(spare)) if spare else ""
        self.muted: set[int] = set()
        self.stopping = threading.Event()
        self.thread = None

    def start(self) -> "Muter":
        if IS_WINDOWS:
            self.thread = threading.Thread(target=self._run, daemon=True, name="installer-mute")
            self.thread.start()
        return self

    def stop(self) -> None:
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def _spared(self, pid: int) -> bool:
        if not self.spare:
            return False
        path = image_path(pid)
        if not path:
            return False
        return os.path.normcase(os.path.abspath(path)).startswith(self.spare + os.sep)

    def _pass(self, mute: bool) -> None:
        wanted = set(self.pids()) if mute else set()
        for pid, volume in _sessions():
            try:
                if mute and pid in wanted and pid not in self.muted and not self._spared(pid):
                    _set_mute(volume, True)
                    self.muted.add(pid)
                elif not mute and pid in self.muted:
                    _set_mute(volume, False)
            finally:
                _release(volume)

    def _run(self) -> None:
        try:
            initialised = _ole32.CoInitializeEx(None, COINIT_MULTITHREADED) in (0, 1)
        except OSError:
            return
        try:
            while not self.stopping.is_set():
                try:
                    self._pass(mute=True)
                except Exception:
                    pass                    # leave the sound on rather than fail an install
                self.stopping.wait(self.INTERVAL)
            try:
                self._pass(mute=False)      # anything of theirs still running gets its sound back
            except Exception:
                pass
        finally:
            if initialised:
                _ole32.CoUninitialize()


def mute_while(tree, spare: str = "", enabled: bool = True) -> Muter | None:
    """Start muting a ProcessTree's programs; stop() the result when it ends."""
    if not (IS_WINDOWS and enabled):
        return None
    try:
        return Muter(tree.pids, spare).start()
    except Exception:
        return None
