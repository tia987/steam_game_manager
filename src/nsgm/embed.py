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
import threading
import time
import uuid

IS_WINDOWS = os.name == "nt"
HOST_TITLE = "Non-Steam Game Manager"

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


def find_host() -> int:
    """The app's own window, or 0 when there is none to hold anything in."""
    if not IS_WINDOWS:
        return 0
    me = os.getpid()
    for hwnd in _top_level_windows():
        if _pid(hwnd) == me and _IsWindowVisible(hwnd) and _text(hwnd) == HOST_TITLE:
            return hwnd
    return 0


def available() -> bool:
    return IS_WINDOWS and ENABLED


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
