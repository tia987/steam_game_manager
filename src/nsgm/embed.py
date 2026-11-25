"""Hold an installer's window inside the app's own window.

An installer draws its own screens, and normally they open as a separate
window somewhere on the desktop. On Windows a window can be given a new
parent, so instead the installer's window is adopted into the app's: its
title bar and frame are removed, it becomes a child of the app window,
and it is kept centred over a slot the interface reserves for it. It goes
on working exactly as before - it is still the installer's own process
drawing it - it just lives inside the app.

What it takes, and what stops it:

* The window has to be found. Every top-level window belonging to the
  installer's process tree is a candidate; message boxes and dialogs it
  opens on top of its main window are left floating, as they would over
  any window.
* Windows only lets a program re-parent another program's window when
  both run at the same privilege level. An installer that asks for
  administrator rights runs above an app that did not, and its window
  then has to stay separate. Running the app itself with administrator
  rights (a setting) removes that limit. The interface says which case
  applies rather than leaving the window to appear unexplained.
* Native windows always draw above web content. The interface reports
  whether the slot is on screen; when it is covered, scrolled away or its
  panel is hidden, the installer's window is hidden with it.

If anything goes wrong the window can be popped back out to stand on its
own, and when the app closes every held window is released first, so a
running installer is never destroyed with it.
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid

IS_WINDOWS = os.name == "nt"
HOST_TITLE = "Non-Steam Game Manager"
# The store's full-screen window has a title of its own; it holds installer
# windows just the same.
HOST_TITLES = (HOST_TITLE, "Game Store")

# Set by the server when the interface runs in the app's own window. In a
# browser tab there is no window of ours to hold anything.
ENABLED = False

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _bind(name, args, result):
        function = getattr(_user32, name)
        function.argtypes = args
        function.restype = result
        return function

    _EnumWindows = _bind("EnumWindows", [_WNDENUMPROC, wintypes.LPARAM], wintypes.BOOL)
    _GetWindowThreadProcessId = _bind("GetWindowThreadProcessId",
                                      [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
                                      wintypes.DWORD)
    _IsWindowVisible = _bind("IsWindowVisible", [wintypes.HWND], wintypes.BOOL)
    _IsWindow = _bind("IsWindow", [wintypes.HWND], wintypes.BOOL)
    _IsIconic = _bind("IsIconic", [wintypes.HWND], wintypes.BOOL)
    _GetWindow = _bind("GetWindow", [wintypes.HWND, wintypes.UINT], wintypes.HWND)
    _GetWindowTextW = _bind("GetWindowTextW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int],
                            ctypes.c_int)
    _GetClassNameW = _bind("GetClassNameW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int],
                           ctypes.c_int)
    _GetWindowRect = _bind("GetWindowRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)],
                           wintypes.BOOL)
    _GetClientRect = _bind("GetClientRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)],
                           wintypes.BOOL)
    _SetParent = _bind("SetParent", [wintypes.HWND, wintypes.HWND], wintypes.HWND)
    _GetAncestor = _bind("GetAncestor", [wintypes.HWND, wintypes.UINT], wintypes.HWND)
    _SetWindowPos = _bind("SetWindowPos",
                          [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL)
    _ShowWindow = _bind("ShowWindow", [wintypes.HWND, ctypes.c_int], wintypes.BOOL)
    _SetForegroundWindow = _bind("SetForegroundWindow", [wintypes.HWND], wintypes.BOOL)
    _GetForegroundWindow = _bind("GetForegroundWindow", [], wintypes.HWND)
    _EnumChildWindows = _bind("EnumChildWindows", [wintypes.HWND, _WNDENUMPROC, wintypes.LPARAM],
                              wintypes.BOOL)
    _BringWindowToTop = _bind("BringWindowToTop", [wintypes.HWND], wintypes.BOOL)
    _keybd_event = _bind("keybd_event", [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD,
                                         ctypes.c_void_p], None)

    # -- driving a held installer from a controller ---------------------------
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _GetCurrentThreadId = _kernel32.GetCurrentThreadId
    _GetCurrentThreadId.argtypes = []
    _GetCurrentThreadId.restype = wintypes.DWORD

    class _GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                    ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                    ("rcCaret", wintypes.RECT)]

    _ULONG_PTR = ctypes.c_size_t

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", _ULONG_PTR)]

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", _ULONG_PTR)]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    _GetGUIThreadInfo = _bind("GetGUIThreadInfo",
                              [wintypes.DWORD, ctypes.POINTER(_GUITHREADINFO)], wintypes.BOOL)
    _AttachThreadInput = _bind("AttachThreadInput",
                               [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL], wintypes.BOOL)
    _SetFocus = _bind("SetFocus", [wintypes.HWND], wintypes.HWND)
    _IsChild = _bind("IsChild", [wintypes.HWND, wintypes.HWND], wintypes.BOOL)
    _IsWindowEnabled = _bind("IsWindowEnabled", [wintypes.HWND], wintypes.BOOL)
    _SendInput = _bind("SendInput", [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int],
                       wintypes.UINT)
    _SendMessageTimeoutW = _bind("SendMessageTimeoutW",
                                 [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                  wintypes.UINT, wintypes.UINT, ctypes.POINTER(_ULONG_PTR)],
                                 wintypes.LPARAM)

    if hasattr(_user32, "GetWindowLongPtrW"):
        _GetLong = _bind("GetWindowLongPtrW", [wintypes.HWND, ctypes.c_int], ctypes.c_ssize_t)
        _SetLong = _bind("SetWindowLongPtrW", [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t],
                         ctypes.c_ssize_t)
    else:                                   # 32-bit Python
        _GetLong = _bind("GetWindowLongW", [wintypes.HWND, ctypes.c_int], ctypes.c_long)
        _SetLong = _bind("SetWindowLongW", [wintypes.HWND, ctypes.c_int, ctypes.c_long],
                         ctypes.c_long)

GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_CHILD, WS_POPUP, WS_VISIBLE = 0x40000000, 0x80000000, 0x10000000
WS_CAPTION, WS_THICKFRAME, WS_SYSMENU = 0x00C00000, 0x00040000, 0x00080000
WS_MINIMIZEBOX, WS_MAXIMIZEBOX, WS_CLIPSIBLINGS = 0x00020000, 0x00010000, 0x04000000
WS_EX_DLGMODALFRAME, WS_EX_TOPMOST, WS_EX_WINDOWEDGE = 0x1, 0x8, 0x100
WS_EX_CLIENTEDGE, WS_EX_STATICEDGE, WS_EX_APPWINDOW = 0x200, 0x20000, 0x40000
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
SWP_FRAMECHANGED, SWP_SHOWWINDOW, SWP_HIDEWINDOW = 0x20, 0x40, 0x80
SWP_ASYNCWINDOWPOS = 0x4000
SW_HIDE, SW_SHOWNA, SW_RESTORE = 0, 8, 9
GW_OWNER = 4

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4
VK_TAB, VK_RETURN, VK_SHIFT, VK_ESCAPE, VK_SPACE = 0x09, 0x0D, 0x10, 0x1B, 0x20
VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN = 0x25, 0x26, 0x27, 0x28
WM_SETTEXT, WM_GETTEXT, BM_GETCHECK = 0x000C, 0x000D, 0x00F0
SMTO_ABORTIFHUNG = 0x0002

# What each controller action presses: (modifier, key). These are the keys
# every Windows wizard already answers to, whoever made it: Tab moves
# between controls, Space presses a button or ticks a box, Enter is the
# default button (Next, Install, Finish), arrows move inside lists.
REMOTE_KEYS = {
    "next": (0, VK_TAB),
    "prev": (VK_SHIFT, VK_TAB),
    "left": (0, VK_LEFT),
    "right": (0, VK_RIGHT),
    "up": (0, VK_UP),
    "down": (0, VK_DOWN),
    "press": (0, VK_SPACE),
    "enter": (0, VK_RETURN),
    "escape": (0, VK_ESCAPE),
}
EXTENDED_KEYS = {VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN}
GA_PARENT = 1
ERROR_ACCESS_DENIED = 5

FRAME = WS_POPUP | WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
FRAME_EX = (WS_EX_DLGMODALFRAME | WS_EX_TOPMOST | WS_EX_WINDOWEDGE | WS_EX_CLIENTEDGE
            | WS_EX_STATICEDGE | WS_EX_APPWINDOW)

# Windows every program has that are never the thing to show.
# A command window: the installer running a script (repacks edit the hosts
# file this way, "Applying redirection rules for fake FitGirl sites...").
CONSOLE_CLASSES = ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "PseudoConsoleWindow")
QUIET_SETTLE = 1.0        # seconds a silent install's window must stay before it is held

NOT_A_WINDOW = {"IME", "MSCTFIME UI", "tooltips_class32", "#32768", "SysShadow",
                "GDI+ Hook Window Class", "CicMarshalWndClass", "OleMainThreadWndClass"}


def _signed(value: int) -> int:
    value &= 0xFFFFFFFF
    if ctypes.sizeof(ctypes.c_void_p) == 4 and value >= 1 << 31:
        return value - (1 << 32)
    return value


def _text(hwnd) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    _GetWindowTextW(hwnd, buffer, 256)
    return buffer.value


def _class(hwnd) -> str:
    buffer = ctypes.create_unicode_buffer(128)
    _GetClassNameW(hwnd, buffer, 128)
    return buffer.value


def _rect(hwnd, client: bool = False) -> tuple[int, int, int, int]:
    box = wintypes.RECT()
    (_GetClientRect if client else _GetWindowRect)(hwnd, ctypes.byref(box))
    return box.left, box.top, box.right - box.left, box.bottom - box.top


def _pid(hwnd) -> int:
    pid = wintypes.DWORD(0)
    _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _top_level_windows() -> list[int]:
    found: list[int] = []

    @_WNDENUMPROC
    def collect(hwnd, _lparam):
        found.append(int(hwnd))
        return True

    _EnumWindows(collect, 0)
    return found


def _child_windows(parent) -> list[int]:
    found: list[int] = []

    @_WNDENUMPROC
    def collect(hwnd, _lparam):
        found.append(int(hwnd))
        return True

    _EnumChildWindows(parent, collect, 0)
    return found


# The last button of an installer, in the languages repacks come in.
FINISH_WORDS = {
    "finish", "done", "fertig stellen", "fertigstellen", "terminer", "fine", "finalizar",
    "concluir", "voltooien", "beëindigen", "zakończ", "готово", "завершить", "完成", "完了",
    "완료", "avsluta", "afslut", "fullfør", "valmis", "dokončit", "befejezés", "bitir",
    "τέλος", "încheiere", "završi", "dokončať",
}


def _caption(hwnd) -> str:
    return _text(hwnd).replace("&", "").strip().rstrip(".").lower()


BUTTON_CLASSES = ("TNewButton", "TButton", "TBitBtn", "Button")
CHECKBOX_CLASSES = ("TNewCheckBox", "TCheckBox", "Button")
CHECKLIST_CLASSES = ("TNewCheckListBox", "TCheckListBox")
BM_GETCHECK, BM_CLICK = 0x00F0, 0x00F5
BS_TYPEMASK = 0x0F
CHECKBOX_STYLES = (2, 3, 5, 6)          # BS_CHECKBOX, BS_AUTOCHECKBOX, BS_3STATE, BS_AUTO3STATE
LB_GETCOUNT, LB_SETCURSEL = 0x018B, 0x0186
WM_KEYDOWN, WM_KEYUP, VK_SPACE = 0x0100, 0x0101, 0x20


def _installer_windows(pids: set) -> list[int]:
    """Visible windows of these processes, whether floating or held in ours."""
    windows = [h for h in _top_level_windows() if _pid(h) in pids]
    host = find_host()
    if host:
        windows += [h for h in _child_windows(host)
                    if _pid(h) in pids and int(_GetAncestor(h, GA_PARENT) or 0) == host]
    return [h for h in windows if _IsWindowVisible(h)]


def _finish_button(hwnd) -> int:
    for kid in _child_windows(hwnd):
        if (_IsWindowVisible(kid) and _IsWindowEnabled(kid)
                and _class(kid) in BUTTON_CLASSES and _caption(kid) in FINISH_WORDS):
            return kid
    return 0


def finished_page(pids: set) -> bool:
    """Whether an installer of these processes is showing its last page.

    Any installer window with a Finish button: Inno Setup, NSIS and the
    reskinned wizards repacks use all end on one. The installation is
    complete by then, and the ticked boxes on that page - launch the game,
    visit a website, read me - only run once the button is pressed.
    """
    if not IS_WINDOWS or not pids:
        return False
    return any(_finish_button(h) for h in _installer_windows(pids))


def finish_owner(pids: set) -> int:
    """The process showing an installer's last page, or 0.

    For Inno Setup that is setup.tmp, the wizard; setup.exe only waits for
    it and tidies its temporary folder once it is gone.
    """
    if not IS_WINDOWS or not pids:
        return 0
    for hwnd in _installer_windows(pids):
        if _finish_button(hwnd):
            return _pid(hwnd)
    return 0


def _send(hwnd, msg: int, wparam: int = 0, lparam: int = 0) -> int:
    result = _ULONG_PTR(0)
    if not _SendMessageTimeoutW(hwnd, msg, wparam, lparam, SMTO_ABORTIFHUNG, 1000,
                                ctypes.byref(result)):
        return -1
    return int(result.value)


def _untick_checkboxes(hwnd) -> list[str]:
    unticked = []
    for kid in _child_windows(hwnd):
        if not (_IsWindowVisible(kid) and _IsWindowEnabled(kid)):
            continue
        if _class(kid) not in CHECKBOX_CLASSES:
            continue
        if (_GetLong(kid, GWL_STYLE) & BS_TYPEMASK) not in CHECKBOX_STYLES:
            continue
        if _send(kid, BM_GETCHECK) != 1:
            continue
        _send(kid, BM_CLICK)              # a click, so the program notices
        if _send(kid, BM_GETCHECK) == 0:
            unticked.append(_caption(kid) or "a box")
    return unticked


def _untick_checklists(hwnd) -> list[str]:
    """Untick the items of Inno Setup's checkbox list (the "Run" list on its last page).

    That list draws its own boxes, so their state is read through Windows'
    accessibility interface, which Inno Setup provides for screen readers,
    and each ticked item is selected and toggled with Space, as by hand.
    """
    unticked = []
    for kid in _child_windows(hwnd):
        if not (_IsWindowVisible(kid) and _IsWindowEnabled(kid)):
            continue
        if _class(kid) not in CHECKLIST_CLASSES:
            continue
        items = _accessible_items(kid)
        for index, (name, checked) in enumerate(items):
            if not checked:
                continue
            _send(kid, LB_SETCURSEL, index)
            _send(kid, WM_KEYDOWN, VK_SPACE, 0x00390001)
            _send(kid, WM_KEYUP, VK_SPACE, 0xC0390001)
            after = _accessible_items(kid)
            if index < len(after) and not after[index][1]:
                unticked.append(name or f"item {index + 1}")
    return unticked


def page_options(pids: set) -> list[dict]:
    """Every tick box an installer's windows show right now.

    ``{"label", "checked", "hwnd", "index"}``: ordinary check boxes have
    index -1; items of Inno Setup's check list (its Select Tasks and
    Select Components pages, and the Run list on its last page) are read
    through the accessibility interface and carry their position.
    """
    out: list[dict] = []
    if not IS_WINDOWS or not pids:
        return out
    for hwnd in _installer_windows(pids):
        for kid in _child_windows(hwnd):
            if not (_IsWindowVisible(kid) and _IsWindowEnabled(kid)):
                continue
            kind = _class(kid)
            if kind in CHECKLIST_CLASSES:
                for index, (name, checked) in enumerate(_accessible_items(kid)):
                    out.append({"label": name, "checked": checked, "hwnd": kid, "index": index})
            elif kind in CHECKBOX_CLASSES and \
                    (_GetLong(kid, GWL_STYLE) & BS_TYPEMASK) in CHECKBOX_STYLES:
                out.append({"label": _text(kid).replace("&", "").strip(),
                            "checked": _send(kid, BM_GETCHECK) == 1, "hwnd": kid, "index": -1})
    return out


def untick_option(option: dict) -> bool:
    """Untick one box from ``page_options``, as a click or Space would. True if it took."""
    if not IS_WINDOWS:
        return False
    kid, index = option["hwnd"], option["index"]
    if index < 0:
        _send(kid, BM_CLICK)
        return _send(kid, BM_GETCHECK) == 0
    _send(kid, LB_SETCURSEL, index)
    _send(kid, WM_KEYDOWN, VK_SPACE, 0x00390001)
    _send(kid, WM_KEYUP, VK_SPACE, 0xC0390001)
    after = _accessible_items(kid)
    return index < len(after) and not after[index][1]


def finish_unticked(pids: set) -> dict:
    """On an installer's last page: untick every box, then press Finish.

    Returns ``{"found", "unticked": [names], "pressed"}``.
    """
    out = {"found": False, "unticked": [], "pressed": False}
    if not IS_WINDOWS or not pids:
        return out
    for hwnd in _installer_windows(pids):
        button = _finish_button(hwnd)
        if not button:
            continue
        out["found"] = True
        try:
            out["unticked"] += _untick_checkboxes(hwnd)
        except Exception:
            pass
        try:
            out["unticked"] += _untick_checklists(hwnd)
        except Exception:
            pass
        out["pressed"] = _send(button, BM_CLICK) != -1
        break
    return out


# --------------------------------------------------------------------------
# reading a checkbox list through Windows' accessibility interface (MSAA)
# --------------------------------------------------------------------------

if IS_WINDOWS:
    class _VARIANT(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                    ("r3", ctypes.c_ushort), ("lVal", ctypes.c_long),
                    ("rest", ctypes.c_byte * (12 if ctypes.sizeof(ctypes.c_void_p) == 8 else 4))]

    class _IID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    _IID_IAccessible = _IID(0x618736E0, 0x3C3D, 0x11CF,
                            (ctypes.c_ubyte * 8)(0x81, 0x0C, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71))
    try:
        _oleacc = ctypes.WinDLL("oleacc")
        _oleacc.AccessibleObjectFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD,
                                                       ctypes.POINTER(_IID),
                                                       ctypes.POINTER(ctypes.c_void_p)]
        _oleacc.AccessibleObjectFromWindow.restype = ctypes.c_long
        _oleaut32 = ctypes.WinDLL("oleaut32")
        _oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]
        _oleaut32.SysFreeString.restype = None
    except OSError:
        _oleacc = None

OBJID_CLIENT = 0xFFFFFFFC
VT_I4 = 3
STATE_SYSTEM_CHECKED = 0x10


def _com(pointer, index: int, *args, argtypes=()):
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    function = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(table[index])
    return function(pointer, *args)


def _accessible_items(hwnd) -> list[tuple[str, bool]]:
    """(name, ticked) for every item of a list, from its accessibility interface."""
    if not IS_WINDOWS or _oleacc is None:
        return []
    accessible = ctypes.c_void_p()
    if _oleacc.AccessibleObjectFromWindow(hwnd, OBJID_CLIENT, ctypes.byref(_IID_IAccessible),
                                          ctypes.byref(accessible)) != 0 or not accessible.value:
        return []
    items = []
    try:
        count = ctypes.c_long(0)
        _com(accessible, 8, ctypes.byref(count), argtypes=(ctypes.POINTER(ctypes.c_long),))
        for child in range(1, count.value + 1):
            which = _VARIANT(vt=VT_I4, lVal=child)
            state = _VARIANT()
            # IAccessible::get_accState(VARIANT child, VARIANT* state)
            _com(accessible, 14, which, ctypes.byref(state),
                 argtypes=(_VARIANT, ctypes.POINTER(_VARIANT)))
            name = ctypes.c_void_p()
            # IAccessible::get_accName(VARIANT child, BSTR* name)
            _com(accessible, 10, which, ctypes.byref(name),
                 argtypes=(_VARIANT, ctypes.POINTER(ctypes.c_void_p)))
            text = ctypes.wstring_at(name.value) if name.value else ""
            if name.value:
                _oleaut32.SysFreeString(name)
            ticked = state.vt == VT_I4 and bool(state.lVal & STATE_SYSTEM_CHECKED)
            items.append((text.replace("&", "").strip(), ticked))
    finally:
        _com(accessible, 2)                 # IUnknown::Release
    return items


def find_host() -> int:
    """The app's own window, or 0 when there is none to hold anything in."""
    if not IS_WINDOWS:
        return 0
    me = os.getpid()
    for hwnd in _top_level_windows():
        if _pid(hwnd) == me and _IsWindowVisible(hwnd) and _text(hwnd) in HOST_TITLES:
            return hwnd
    return 0


def available() -> bool:
    return IS_WINDOWS and ENABLED


def take_foreground(tries: int = 20, pause: float = 0.25) -> bool:
    """Bring the app's own window to the front, and keep trying for a few seconds.

    Over Moonlight the store window has to be the foreground window: that
    is the one the controller, the phone's keyboard and touches reach. A
    program started by a service (Sunshine), or straight after Windows'
    permission prompt, is not allowed to take the foreground by itself;
    a tap of Alt first, which is what Windows checks for, lets it.
    """
    if not IS_WINDOWS:
        return False
    import time as _time
    for _ in range(tries):
        host = find_host()
        if host:
            if _IsIconic(host):
                _ShowWindow(host, SW_RESTORE)
            if int(_GetForegroundWindow() or 0) == host:
                return True
            _BringWindowToTop(host)
            if not _SetForegroundWindow(host):
                _keybd_event(0x12, 0, 0, None)          # Alt down
                _keybd_event(0x12, 0, 0x0002, None)     # Alt up
                _SetForegroundWindow(host)
            if int(_GetForegroundWindow() or 0) == host:
                return True
        _time.sleep(pause)
    return False


# Windows that mean "nothing in particular is in front": the desktop, the
# taskbar, or no window at all, which is what a program started by a
# service often leaves behind.
NOBODY_IN_FRONT = ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "")


def keep_in_front(every: float = 2.0, stop=None) -> None:
    """While streamed: whenever nothing else is in front, put the store there.

    The phone keyboard and touches over Moonlight go to the front window,
    so the store must be it. It never takes over from a real window - an
    installer's question, say - only from the desktop or the taskbar.
    """
    if not IS_WINDOWS:
        return
    import time as _time

    def run() -> None:
        while not (stop and stop.is_set()):
            try:
                front = int(_GetForegroundWindow() or 0)
                if find_host() and (not front or _class(front) in NOBODY_IN_FRONT):
                    take_foreground(tries=1)
            except Exception:
                pass
            _time.sleep(every)

    threading.Thread(target=run, name="nsgm-keep-in-front", daemon=True).start()


# --------------------------------------------------------------------------
# while a store install runs, and a little after
# --------------------------------------------------------------------------

# A browser page an installer opened: repacks send their bit.ly link, which
# shows a Bitly preview and then forwards to the repacker's site.
INSTALLER_PAGE = re.compile(r"(?i)\bbit\.?ly\b|fitgirl")


class Tidy:
    """Keeps a store install out of sight and the store in front.

    - The installer's console windows are hidden; they keep running. One
      still open after CONSOLE_PATIENCE seconds is shown again, in case it
      waits for a key.
    - One of those consoles, or a browser showing the installer's page,
      taking the front gets the store put back there. The installer's own
      windows are never covered, nor a browser someone switched to.
    - A browser tab that turns to an installer's page (bit.ly, the
      repacker's site) after the install started is closed with Ctrl+W, the
      store coming back in front straight after. Only a window whose title
      changed to such a page since the install began, and only once it is
      confirmed to be the front window, so a key never lands elsewhere.

    Runs until AFTER seconds past ``stop()``: the page tends to open as the
    installer exits.
    """

    EVERY = 0.1
    AFTER = 20.0
    CONSOLE_PATIENCE = 45.0

    def __init__(self, tree, is_browser, store_in_front: bool = True):
        self.tree = tree
        self.is_browser = is_browser            # executable name -> bool
        self.store_in_front = store_in_front
        self.stopped_at = 0.0
        self.hidden: dict[int, float] = {}
        self.closed: set = set()
        self.before: dict[int, str] = {}
        self.notes: list[str] = []

    def start(self) -> "Tidy":
        if IS_WINDOWS:
            self.before = {h: _text(h) for h in _top_level_windows() if _IsWindowVisible(h)}
            threading.Thread(target=self._run, name="nsgm-install-tidy", daemon=True).start()
        return self

    def stop(self) -> None:
        self.stopped_at = self.stopped_at or time.time()

    def _run(self) -> None:
        names: dict[int, str] = {}
        while not (self.stopped_at and time.time() - self.stopped_at > self.AFTER):
            try:
                self._tick(names)
            except Exception:
                pass
            time.sleep(self.EVERY)

    def _name(self, pid: int, names: dict) -> str:
        if pid not in names:
            from . import winproc
            names.update({p: n for p, (_parent, n) in winproc._snapshot().items()})
        return names.get(pid, "")

    def _tick(self, names: dict) -> None:
        live = self.tree.pids() if not self.stopped_at else set()
        host = find_host()
        for hwnd in _top_level_windows():
            if not _IsWindowVisible(hwnd):
                continue
            pid = _pid(hwnd)
            if pid in live and _class(hwnd) in CONSOLE_CLASSES and hwnd not in self.hidden:
                _ShowWindow(hwnd, SW_HIDE)
                self.hidden[hwnd] = time.time()
                self.notes.append(f"Hid the installer's command window “{_text(hwnd)}”.")
                continue
            title = _text(hwnd)
            if (INSTALLER_PAGE.search(title) and (hwnd, title) not in self.closed
                    and not INSTALLER_PAGE.search(self.before.get(hwnd, ""))
                    and self.is_browser(self._name(pid, names))):
                self.closed.add((hwnd, title))
                if self._close_tab(hwnd, host):
                    self.notes.append(f"Closed the page the installer opened: “{title}”.")
        # A console that has been waiting a long time may want a key.
        for hwnd, when in list(self.hidden.items()):
            if not _IsWindow(hwnd):
                self.hidden.pop(hwnd)
            elif time.time() - when > self.CONSOLE_PATIENCE and when > 0:
                _ShowWindow(hwnd, SW_SHOWNA)
                self.hidden[hwnd] = -1.0
        if host and self.store_in_front:
            front = int(_GetForegroundWindow() or 0)
            if front and front != host and not _IsChild(host, front):
                # Only from what the install itself put there: its console,
                # or a browser showing its page. Someone switching to their
                # browser during a long install keeps it.
                pid = _pid(front)
                if (pid in live and _class(front) in CONSOLE_CLASSES) or (
                        INSTALLER_PAGE.search(_text(front))
                        and self.is_browser(self._name(pid, names))):
                    take_foreground(tries=1)

    def _close_tab(self, hwnd: int, host: int) -> bool:
        if _IsIconic(hwnd):
            _ShowWindow(hwnd, SW_RESTORE)
        _SetForegroundWindow(hwnd)
        time.sleep(0.15)
        if int(_GetForegroundWindow() or 0) != hwnd or not INSTALLER_PAGE.search(_text(hwnd)):
            if host:
                take_foreground(tries=2)
            return False
        control, w_key, up = 0x11, 0x57, 0x0002
        _keybd_event(control, 0, 0, None)
        _keybd_event(w_key, 0, 0, None)
        _keybd_event(w_key, 0, up, None)
        _keybd_event(control, 0, up, None)
        time.sleep(0.2)
        if host:
            take_foreground(tries=4)
        return True


# --------------------------------------------------------------------------
# one program's windows
# --------------------------------------------------------------------------

class Session:
    """The windows of one installer or uninstaller, held or floating."""

    def __init__(self, tree, title: str, kind: str = "install", task_id: str = ""):
        self.id = uuid.uuid4().hex[:10]
        self.tree = tree
        self.title = title
        self.kind = kind
        self.task_id = task_id
        self.state = "starting"       # starting, embedded, floating, hidden, ended
        self.reason = ""
        self.window_title = ""
        self.size = (0, 0)
        self.held: dict[int, tuple[int, int, tuple]] = {}   # hwnd -> saved style, ex, rect
        self.refused: set[int] = set()
        self.active = 0
        self.popped = False
        self.we_hid: set[int] = set()
        self.placed: dict[int, tuple] = {}
        self.started = time.time()
        self.last_focus = 0           # the installer control the controller last selected
        self.first_seen: dict[int, float] = {}

    def to_json(self) -> dict:
        return {"id": self.id, "title": self.title, "kind": self.kind, "task": self.task_id,
                "state": self.state, "reason": self.reason, "window": self.window_title,
                "size": list(self.size), "popped": self.popped,
                "has_window": bool(self.active), "available": available()}


class Manager:
    """Keeps the current session's window where the interface wants it."""

    def __init__(self):
        self.lock = threading.RLock()
        self.session: Session | None = None
        self.visible = False
        self.rect: tuple[int, int, int, int] | None = None
        self.host = 0
        self.wake = threading.Event()
        self.thread: threading.Thread | None = None

    # -- interface ----------------------------------------------------------

    def start(self, session: Session) -> Session:
        with self.lock:
            self.session = session
            if not available():
                session.state = "floating"
                session.reason = ("The app is running in a browser tab, so the installer "
                                  "has a window of its own." if IS_WINDOWS else "")
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, daemon=True)
                self.thread.start()
        self.wake.set()
        return session

    def place(self, visible: bool, rect) -> None:
        with self.lock:
            self.visible = bool(visible)
            if rect and len(rect) == 4:
                self.rect = tuple(int(v) for v in rect)
        self.wake.set()

    def current(self) -> dict | None:
        with self.lock:
            return self.session.to_json() if self.session else None

    def pop_out(self) -> None:
        with self.lock:
            session = self.session
            if session and IS_WINDOWS:
                session.popped = True
                for hwnd in list(session.held):
                    self._release(session, hwnd)
                session.state = "floating"
                session.reason = "Popped out into its own window."
                self._front(session)

    def bring_back(self) -> None:
        with self.lock:
            session = self.session
            if session:
                session.popped = False
                session.refused.clear()
                session.state = "starting"
                session.reason = ""
        self.wake.set()

    def front(self) -> None:
        with self.lock:
            if self.session and IS_WINDOWS:
                self._front(self.session)

    def release_all(self) -> None:
        """Let go of every held window. Called as the app closes.

        This runs on the window's own thread. The loop thread may be in the
        middle of a call that waits on that same thread, so the lock is
        only waited for briefly; releasing without it beats a hung exit.
        """
        got = self.lock.acquire(timeout=1.0)
        try:
            session = self.session
            if session and IS_WINDOWS:
                for hwnd in list(session.held):
                    self._release(session, hwnd)
        finally:
            if got:
                self.lock.release()

    def finish(self, session: Session) -> None:
        with self.lock:
            if IS_WINDOWS:
                for hwnd in list(session.held):
                    self._release(session, hwnd)
            session.state = "ended"
            if self.session is session:
                self.session = None

    # -- the loop -------------------------------------------------------------

    def _loop(self) -> None:
        while True:
            self.wake.wait(0.08)
            self.wake.clear()
            with self.lock:
                session = self.session
                if session is None:
                    self.thread = None
                    return
                try:
                    self._tick(session)
                except Exception as exc:          # never let this thread die
                    session.reason = f"Lost track of the window ({exc})."

    def _tick(self, session: Session) -> None:
        live = session.tree.refresh()
        if not live:
            self.finish(session)
            return
        if not IS_WINDOWS or not available():
            return
        if not self.host or not _IsWindow(self.host):
            self.host = find_host()
            if not self.host:
                session.state = "floating"
                session.reason = "The app window could not be found."
                return

        for hwnd in list(session.held):
            if not _IsWindow(hwnd):
                session.held.pop(hwnd, None)
                session.we_hid.discard(hwnd)
                session.placed.pop(hwnd, None)
        if session.popped:
            return

        # The window to show: a held one that is still meant to be visible,
        # otherwise a new top-level window from the tree.
        active = 0
        for hwnd in session.held:
            if _IsWindowVisible(hwnd) or hwnd in session.we_hid:
                if not active or self._area(hwnd) > self._area(active):
                    active = hwnd
        if not active:
            candidate = self._candidate(session, live)
            if candidate:
                outcome = self._adopt(session, candidate)
                if outcome == "held":
                    active = candidate
                elif outcome == "refused":
                    session.refused.add(candidate)
                    session.state = "floating"
                    session.reason = ("It runs with administrator rights, so Windows keeps it "
                                      "in a window of its own. Turn on “Start with "
                                      "administrator rights” in Settings to keep installers "
                                      "inside the app.")
        session.active = active
        if not active:
            if session.state != "floating":
                session.state = "starting"
            return

        session.state = "embedded" if self.visible else "hidden"
        session.reason = ""
        session.window_title = _text(active) or session.window_title
        _x, _y, width, height = _rect(active)
        session.size = (width, height)
        self._apply(session, active)

    @staticmethod
    def _area(hwnd) -> int:
        _x, _y, width, height = _rect(hwnd)
        return width * height

    def _candidate(self, session: Session, live: set) -> int:
        best, best_area = 0, 0
        for hwnd in _top_level_windows():
            if hwnd in session.refused or hwnd in session.held:
                continue
            if _pid(hwnd) not in live or not _IsWindowVisible(hwnd):
                continue
            if _class(hwnd) in NOT_A_WINDOW:
                continue
            if session.kind == "quiet":
                # A silent install has nothing to ask. What it shows is a
                # console running a script, or something flashing past;
                # only a window that stays up (an error to read, a question)
                # is brought into the app.
                if _class(hwnd) in CONSOLE_CLASSES:
                    continue
                seen = session.first_seen.setdefault(hwnd, time.time())
                if time.time() - seen < QUIET_SETTLE:
                    continue
            owner = _GetWindow(hwnd, GW_OWNER)
            # A visible owner means a dialog or message box sitting on the
            # main window; those float. Delphi programs such as Inno Setup
            # own every form by a hidden application window, so an
            # invisible owner does not count.
            if owner and _IsWindowVisible(owner):
                continue
            _x, _y, width, height = _rect(hwnd)
            if width < 160 or height < 100:
                continue
            if width * height > best_area:
                best, best_area = hwnd, width * height
        return best

    def _adopt(self, session: Session, hwnd: int) -> str:
        style = _GetLong(hwnd, GWL_STYLE) & 0xFFFFFFFF
        ex = _GetLong(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
        outer = _rect(hwnd)
        _cx, _cy, client_w, client_h = _rect(hwnd, client=True)

        # The style changes before the parent does, as SetParent expects.
        ctypes.set_last_error(0)
        _SetLong(hwnd, GWL_STYLE, _signed((style & ~FRAME) | WS_CHILD | WS_CLIPSIBLINGS))
        if ctypes.get_last_error() == ERROR_ACCESS_DENIED:
            return "refused"
        ctypes.set_last_error(0)
        _SetParent(hwnd, self.host)
        # SetParent's return value is ambiguous for a top-level window, so
        # the outcome is read back instead.
        if int(_GetAncestor(hwnd, GA_PARENT) or 0) != self.host:
            error = ctypes.get_last_error()
            _SetLong(hwnd, GWL_STYLE, _signed(style))
            return "refused" if error in (0, ERROR_ACCESS_DENIED) else "failed"
        _SetLong(hwnd, GWL_EXSTYLE, _signed(ex & ~FRAME_EX))
        # Without its frame the window would keep its old outer size and
        # leave a blank strip where the title bar was; shrinking it to its
        # client area keeps the content exactly as it was laid out.
        _SetWindowPos(hwnd, None, 0, 0, max(client_w, 1), max(client_h, 1),
                      SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        session.held[hwnd] = (style, ex, outer)
        session.placed.pop(hwnd, None)
        return "held"

    def _apply(self, session: Session, hwnd: int) -> None:
        if not self.visible or not self.rect or _IsIconic(self.host):
            if hwnd not in session.we_hid:
                _ShowWindow(hwnd, SW_HIDE)
                session.we_hid.add(hwnd)
                session.placed.pop(hwnd, None)
            return
        left, top, width, height = self.rect
        _x, _y, w, h = _rect(hwnd)
        x = left + max(0, (width - w) // 2)
        y = top + max(0, (height - h) // 2)
        wanted = (x, y, w, h)
        if session.placed.get(hwnd) == wanted and hwnd not in session.we_hid:
            return
        _SetWindowPos(hwnd, None, x, y, 0, 0,
                      SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_ASYNCWINDOWPOS)
        session.we_hid.discard(hwnd)
        session.placed[hwnd] = wanted

    def _release(self, session: Session, hwnd: int) -> None:
        saved = session.held.pop(hwnd, None)
        session.we_hid.discard(hwnd)
        session.placed.pop(hwnd, None)
        if not saved or not _IsWindow(hwnd):
            return
        style, ex, outer = saved
        _SetParent(hwnd, None)
        _SetLong(hwnd, GWL_STYLE, _signed(style | (WS_VISIBLE if not style & WS_VISIBLE else 0)))
        _SetLong(hwnd, GWL_EXSTYLE, _signed(ex))
        width, height = outer[2], outer[3]
        x, y = outer[0], outer[1]
        if self.host and _IsWindow(self.host):
            hx, hy, hw, hh = _rect(self.host)
            x, y = hx + max(0, (hw - width) // 2), hy + max(0, (hh - height) // 2)
        _SetWindowPos(hwnd, None, x, y, width, height,
                      SWP_NOZORDER | SWP_FRAMECHANGED | SWP_SHOWWINDOW)

    def _front(self, session: Session) -> None:
        live = session.tree.pids()
        for hwnd in _top_level_windows():
            if _pid(hwnd) in live and _IsWindowVisible(hwnd):
                if _IsIconic(hwnd):
                    _ShowWindow(hwnd, SW_RESTORE)
                _SetForegroundWindow(hwnd)


    # -- the controller ---------------------------------------------------------

    def remote(self, action: str, text: str = "") -> dict:
        """Work the held installer's screens: press keys, fill in a field.

        Keystrokes are real input, sent with SendInput, so modifiers and
        dialog navigation behave exactly as on a keyboard. That needs the
        installer to hold the keyboard focus, which is possible because a
        window adopted from another program shares its input with ours.
        """
        if not IS_WINDOWS:
            return {"ok": False, "error": "Installers only run on Windows."}
        with self.lock:
            session = self.session
            hwnd = session.active if session else 0
            if not session or not hwnd or hwnd not in session.held or not _IsWindow(hwnd):
                return {"ok": False, "error": "No installer is held in the app right now."}
            if not self.visible:
                return {"ok": False, "error": "The installer is hidden."}
            if action == "describe":
                focus = self._focus_in(hwnd)
                if focus:
                    session.last_focus = focus
                return {"ok": True, "focus": self._describe(focus) if focus else None}
            if action == "type":
                return self._type_text(session, hwnd, text)
            keys = REMOTE_KEYS.get(action)
            if not keys:
                return {"ok": False, "error": f"Unknown action {action!r}."}
            focus = self._take_focus(session, hwnd)
            if not focus:
                return {"ok": False, "error": "The installer would not take the keyboard."}
            self._press(*keys)
        time.sleep(0.08)                    # let it move its focus before reading it
        with self.lock:
            focus = self._focus_in(hwnd)
            if focus:
                session.last_focus = focus
            return {"ok": True, "focus": self._describe(focus) if focus else None}

    @staticmethod
    def _focus_in(hwnd) -> int:
        """The installer's focused control, if the focus is inside it."""
        thread = _GetWindowThreadProcessId(hwnd, None)
        info = _GUITHREADINFO()
        info.cbSize = ctypes.sizeof(_GUITHREADINFO)
        if not _GetGUIThreadInfo(thread, ctypes.byref(info)):
            return 0
        focus = int(info.hwndFocus or 0)
        if focus and (focus == hwnd or _IsChild(hwnd, focus)):
            return focus
        return 0

    def _take_focus(self, session: Session, hwnd) -> int:
        focus = self._focus_in(hwnd)
        if focus:
            return focus
        thread = _GetWindowThreadProcessId(hwnd, None)
        me = _GetCurrentThreadId()
        attached = bool(_AttachThreadInput(me, thread, True))
        try:
            if self.host:
                _SetForegroundWindow(self.host)
            last = session.last_focus
            target = last if last and _IsWindow(last) and _IsChild(hwnd, last) else hwnd
            # Focusing the installer's own window hands the focus on to the
            # control it last had, as dialogs and Delphi forms both do.
            _SetFocus(target)
        finally:
            if attached:
                _AttachThreadInput(me, thread, False)
        time.sleep(0.03)
        return self._focus_in(hwnd)

    @staticmethod
    def _key(vk: int, up: bool = False, char: int = 0) -> "_INPUT":
        event = _INPUT()
        event.type = INPUT_KEYBOARD
        if char:
            event.u.ki = _KEYBDINPUT(0, char, KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0), 0, 0)
        else:
            flags = (KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_KEYS else 0) | (KEYEVENTF_KEYUP if up else 0)
            event.u.ki = _KEYBDINPUT(vk, 0, flags, 0, 0)
        return event

    def _press(self, modifier: int, vk: int) -> None:
        events = []
        if modifier:
            events.append(self._key(modifier))
        events += [self._key(vk), self._key(vk, up=True)]
        if modifier:
            events.append(self._key(modifier, up=True))
        array = (_INPUT * len(events))(*events)
        _SendInput(len(events), array, ctypes.sizeof(_INPUT))

    @staticmethod
    def _message(hwnd, msg: int, wparam: int = 0, lparam: int = 0) -> int:
        result = _ULONG_PTR(0)
        if not _SendMessageTimeoutW(hwnd, msg, wparam, lparam, SMTO_ABORTIFHUNG, 500,
                                    ctypes.byref(result)):
            return -1
        return int(result.value)

    def _control_text(self, hwnd) -> str:
        buffer = ctypes.create_unicode_buffer(1024)
        if self._message(hwnd, WM_GETTEXT, 1024, ctypes.addressof(buffer)) < 0:
            return ""
        return buffer.value

    def _describe(self, hwnd) -> dict:
        """What the selected control is, for the store to say out loud."""
        name = _class(hwnd).lower()
        style = _GetLong(hwnd, GWL_STYLE) & 0xFFFFFFFF
        text = self._control_text(hwnd)
        if "edit" in name:
            kind = "edit"
        elif "listbox" in name or "listview" in name or "tree" in name:
            kind = "list"
        elif "combo" in name:
            kind = "choice"
        elif "check" in name:
            kind = "checkbox"
        elif "radio" in name:
            kind = "radio"
        elif "button" in name:
            kind = {2: "checkbox", 3: "checkbox", 5: "checkbox", 6: "checkbox",
                    4: "radio", 9: "radio"}.get(style & 0xF, "button")
        else:
            kind = "other"
        label = text if kind == "edit" else text.replace("&&", "\0").replace("&", "").replace("\0", "&")
        checked = None
        if kind in ("checkbox", "radio"):
            checked = self._message(hwnd, BM_GETCHECK) == 1
        return {"kind": kind, "label": label.strip()[:200], "checked": checked,
                "enabled": bool(_IsWindowEnabled(hwnd))}

    def _type_text(self, session: Session, hwnd, text: str) -> dict:
        target = session.last_focus
        if not (target and _IsWindow(target) and _IsChild(hwnd, target)):
            target = self._focus_in(hwnd)
        if not target:
            return {"ok": False, "error": "Select a text box in the installer first."}
        if "edit" in _class(target).lower():
            # Setting the text outright replaces what was there, and the
            # control tells its installer it changed, as typing would.
            buffer = ctypes.create_unicode_buffer(text[:1000])
            if self._message(target, WM_SETTEXT, 0, ctypes.addressof(buffer)) < 0:
                return {"ok": False, "error": "The installer did not answer."}
        else:
            if not self._take_focus(session, hwnd):
                return {"ok": False, "error": "The installer would not take the keyboard."}
            events = []
            for ch in text[:1000]:
                code = ord(ch)
                if code > 0xFFFF:
                    continue
                events += [self._key(0, char=code), self._key(0, up=True, char=code)]
            if events:
                array = (_INPUT * len(events))(*events)
                _SendInput(len(events), array, ctypes.sizeof(_INPUT))
        return {"ok": True, "focus": self._describe(target)}


MANAGER = Manager()
