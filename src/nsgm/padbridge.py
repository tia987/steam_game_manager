"""Read controllers directly, for when the window's own reading fails.

The store normally reads the controller through the browser engine inside
its window (the Gamepad API). Running as administrator - how the store runs
when Sunshine starts it with its Elevated option - that reading can go
quiet: Xbox-type pads come through Windows.Gaming.Input, which does not
serve elevated programs, and the engine may even keep listing a pad that
never changes. Then the controller does nothing.

So the app reads pads itself, about sixty times a second, and hands the
state to the page, which combines it with whatever the engine reports:

- Xbox-type pads through XInput. Sunshine's emulated Xbox 360 pad is one;
  so are Xbox pads and most others.
- PlayStation pads through Windows' joystick API (WinMM), which XInput
  cannot see. With a PlayStation controller in Moonlight, Sunshine's
  automatic setting emulates a DualShock 4, so this is what a PS4/PS5 pad
  over Moonlight is.

Outside a stream, only while the store's window is in front: a game
started from the store must not also be driving the store behind it. In a
stream the store is the whole session, so it always listens.
"""

from __future__ import annotations

import json
import os
import threading
import time

IS_WINDOWS = os.name == "nt"

# XInput button bits, in the order of the browser's standard gamepad layout.
STANDARD = [0x1000, 0x2000, 0x4000, 0x8000,     # A B X Y
            0x0100, 0x0200, None, None,          # LB RB LT RT (triggers are analogue)
            0x0020, 0x0010, 0x0040, 0x0080,      # Back Start LS RS
            0x0001, 0x0002, 0x0004, 0x0008]      # up down left right

# PlayStation pads through WinMM: their HID buttons are Square, Cross,
# Circle, Triangle, L1, R1, L2, R2, Share/Create, Options, L3, R3, PS,
# touchpad - the same on a DualShock 4, a DualSense and Sunshine's
# emulated DualShock 4. Standard position -> WinMM button bit.
SONY_BUTTONS = [1 << 1, 1 << 2, 1 << 0, 1 << 3,     # A=Cross B=Circle X=Square Y=Triangle
                1 << 4, 1 << 5, 1 << 6, 1 << 7,     # L1 R1 L2 R2
                1 << 8, 1 << 9, 1 << 10, 1 << 11]   # Share Options L3 R3
SONY = 0x054C
MICROSOFT = 0x045E          # Xbox pads, real or emulated: XInput has them already
DUALSENSE = (0x0CE6, 0x0DF2)

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class XINPUT_GAMEPAD(ctypes.Structure):
        _fields_ = [("wButtons", wintypes.WORD), ("bLeftTrigger", ctypes.c_ubyte),
                    ("bRightTrigger", ctypes.c_ubyte), ("sThumbLX", ctypes.c_short),
                    ("sThumbLY", ctypes.c_short), ("sThumbRX", ctypes.c_short),
                    ("sThumbRY", ctypes.c_short)]

    class XINPUT_STATE(ctypes.Structure):
        _fields_ = [("dwPacketNumber", wintypes.DWORD), ("Gamepad", XINPUT_GAMEPAD)]

    class JOYINFOEX(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("dwXpos", wintypes.DWORD), ("dwYpos", wintypes.DWORD),
                    ("dwZpos", wintypes.DWORD), ("dwRpos", wintypes.DWORD),
                    ("dwUpos", wintypes.DWORD), ("dwVpos", wintypes.DWORD),
                    ("dwButtons", wintypes.DWORD), ("dwButtonNumber", wintypes.DWORD),
                    ("dwPOV", wintypes.DWORD), ("dwReserved1", wintypes.DWORD),
                    ("dwReserved2", wintypes.DWORD)]

    class JOYCAPSW(ctypes.Structure):
        _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD),
                    ("szPname", wintypes.WCHAR * 32),
                    ("wXmin", wintypes.UINT), ("wXmax", wintypes.UINT),
                    ("wYmin", wintypes.UINT), ("wYmax", wintypes.UINT),
                    ("wZmin", wintypes.UINT), ("wZmax", wintypes.UINT),
                    ("wNumButtons", wintypes.UINT),
                    ("wPeriodMin", wintypes.UINT), ("wPeriodMax", wintypes.UINT),
                    ("wRmin", wintypes.UINT), ("wRmax", wintypes.UINT),
                    ("wUmin", wintypes.UINT), ("wUmax", wintypes.UINT),
                    ("wVmin", wintypes.UINT), ("wVmax", wintypes.UINT),
                    ("wCaps", wintypes.UINT), ("wMaxAxes", wintypes.UINT),
                    ("wNumAxes", wintypes.UINT), ("wMaxButtons", wintypes.UINT),
                    ("szRegKey", wintypes.WCHAR * 32),
                    ("szOEMVxD", wintypes.WCHAR * 260)]

JOY_RETURNALL = 0x000000FF
JOY_POVCENTERED = 0xFFFF
JOYERR_NOERROR = 0


def _library():
    for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            dll = ctypes.WinDLL(name)
        except OSError:
            continue
        dll.XInputGetState.argtypes = [wintypes.DWORD, ctypes.POINTER(XINPUT_STATE)]
        dll.XInputGetState.restype = wintypes.DWORD
        return dll
    return None


def _winmm():
    try:
        dll = ctypes.WinDLL("winmm")
    except OSError:
        return None
    dll.joyGetNumDevs.restype = wintypes.UINT
    dll.joyGetPosEx.argtypes = [wintypes.UINT, ctypes.POINTER(JOYINFOEX)]
    dll.joyGetPosEx.restype = wintypes.UINT
    dll.joyGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(JOYCAPSW), wintypes.UINT]
    dll.joyGetDevCapsW.restype = wintypes.UINT
    try:
        dll.joyConfigChanged.argtypes = [wintypes.DWORD]
        dll.joyConfigChanged.restype = wintypes.UINT
    except AttributeError:
        pass
    return dll


def _axis(value: float, flip: bool = False) -> float:
    number = max(-1.0, min(1.0, value))
    number = -number if flip else number
    return 0.0 if abs(number) < 0.2 else round(number, 2)


def snapshot(pad) -> dict:
    """One XInput pad as the page's gamepad shape: buttons and four axes."""
    bits = pad.wButtons
    buttons = [bool(bits & bit) if bit else False for bit in STANDARD]
    buttons[6] = pad.bLeftTrigger > 30
    buttons[7] = pad.bRightTrigger > 30
    return {"buttons": buttons, "family": "xbox",
            "axes": [_axis(pad.sThumbLX / 32767.0), _axis(pad.sThumbLY / 32767.0, True),
                     _axis(pad.sThumbRX / 32767.0), _axis(pad.sThumbRY / 32767.0, True)]}


def _scale(value: int, low: int, high: int) -> float:
    """A WinMM axis reading (low..high, usually 0..65535) as -1..1."""
    if high <= low:
        low, high = 0, 65535
    return (value - low) * 2.0 / (high - low) - 1.0


def sony_snapshot(info, caps, pid: int = 0) -> dict:
    """One PlayStation pad read through WinMM, in the standard layout.

    Left stick on X/Y (certain on every Sony pad); right stick on Z (across)
    and Rz, which WinMM reports as R (up and down); the d-pad as the POV hat
    in hundredths of a degree, 0 being up.
    """
    bits = info.dwButtons
    buttons = [bool(bits & bit) for bit in SONY_BUTTONS] + [False] * 4
    pov = info.dwPOV
    if pov != JOY_POVCENTERED and pov <= 36000:
        angle = pov / 100.0
        buttons[12] = angle >= 315 or angle <= 45        # up
        buttons[15] = 45 <= angle <= 135                  # right
        buttons[13] = 135 <= angle <= 225                 # down
        buttons[14] = 225 <= angle <= 315                 # left
    return {"buttons": buttons, "family": "ps5" if pid in DUALSENSE else "ps4",
            "axes": [_axis(_scale(info.dwXpos, caps.wXmin, caps.wXmax)),
                     _axis(_scale(info.dwYpos, caps.wYmin, caps.wYmax)),
                     _axis(_scale(info.dwZpos, caps.wZmin, caps.wZmax)),
                     _axis(_scale(info.dwRpos, caps.wRmin, caps.wRmax))]}


def merge(states: list) -> dict | None:
    """Several pads as one, the way the page treats them."""
    if not states:
        return None
    buttons = [any(s["buttons"][i] for s in states) for i in range(16)]
    axes = []
    for i in range(4):
        values = [s["axes"][i] for s in states]
        axes.append(max(values, key=abs))
    # The pad last used decides the button symbols; any PlayStation pad wins
    # a tie, since an Xbox pad is also what a missing one looks like.
    families = [s.get("family", "xbox") for s in states]
    family = next((f for f in families if f != "xbox"), "xbox")
    return {"buttons": buttons, "axes": axes, "family": family}


class SonyPads:
    """PlayStation pads through WinMM's joystick functions."""

    RESCAN = 3.0            # the first half minute, then every RESCAN_LATER
    RESCAN_LATER = 15.0

    def __init__(self, dll):
        self.dll = dll
        self.found: dict[int, tuple] = {}     # joystick id -> (caps, product id)
        self.scanned = 0.0
        self.scans = 0

    def _scan(self) -> None:
        self.scanned = time.time()
        self.scans += 1
        # Windows only notices pads plugged in after a program first asked
        # once told the configuration changed.
        changed = getattr(self.dll, "joyConfigChanged", None)
        if changed is not None:
            try:
                changed(0)
            except OSError:
                pass
        found = {}
        info = JOYINFOEX()
        info.dwSize = ctypes.sizeof(JOYINFOEX)
        info.dwFlags = JOY_RETURNALL
        for joy in range(min(int(self.dll.joyGetNumDevs()), 16)):
            if self.dll.joyGetPosEx(joy, ctypes.byref(info)) != JOYERR_NOERROR:
                continue
            caps = JOYCAPSW()
            if self.dll.joyGetDevCapsW(joy, ctypes.byref(caps), ctypes.sizeof(JOYCAPSW)) \
                    != JOYERR_NOERROR:
                continue
            if caps.wMid == SONY:
                found[joy] = (caps, int(caps.wPid))
        self.found = found

    def read(self) -> list:
        wait = self.RESCAN if self.scans < 10 else self.RESCAN_LATER
        if not self.found and time.time() - self.scanned > wait:
            self._scan()
        states = []
        info = JOYINFOEX()
        for joy, (caps, pid) in list(self.found.items()):
            info.dwSize = ctypes.sizeof(JOYINFOEX)
            info.dwFlags = JOY_RETURNALL
            if self.dll.joyGetPosEx(joy, ctypes.byref(info)) != JOYERR_NOERROR:
                self.found.pop(joy, None)        # unplugged: look again later
                continue
            states.append(sony_snapshot(info, caps, pid))
        return states


class Bridge:
    INTERVAL = 1 / 60

    def __init__(self, window, in_front, streaming: bool = False):
        self.window = window
        self.in_front = in_front
        self.streaming = streaming
        self.stopping = threading.Event()
        self.last = None
        self.status = {"xinput": 0, "playstation": 0, "in_front": False, "sent": 0,
                       "error": "", "readers": []}

    def start(self) -> "Bridge":
        if IS_WINDOWS:
            threading.Thread(target=self._run, daemon=True, name="pad-bridge").start()
        return self

    def _send(self, state) -> None:
        script = f"window.nativePad && window.nativePad({json.dumps(state)})"
        runner = getattr(self.window, "run_js", None) or self.window.evaluate_js
        try:
            runner(script)
            self.status["sent"] += 1
        except Exception as exc:
            self.status["error"] = f"{type(exc).__name__}: {exc}"[:200]

    def outgoing(self, pads: list) -> dict | None:
        """What the page is given: the pads, or nothing pressed when it must not listen."""
        current = merge(pads)
        in_front = bool(self.in_front())
        self.status["in_front"] = in_front
        if current is not None and not (self.streaming or in_front):
            current = {"buttons": [False] * 16, "axes": [0.0] * 4, "family": current["family"]}
        return current

    def _run(self) -> None:
        xinput = _library()
        winmm = _winmm()
        sony = SonyPads(winmm) if winmm is not None else None
        self.status["readers"] = [name for name, dll in (("XInput", xinput), ("WinMM", winmm))
                                  if dll is not None]
        if xinput is None and sony is None:
            self.status["error"] = "Neither XInput nor WinMM could be loaded."
            return
        state = XINPUT_STATE() if xinput is not None else None
        idle_checks = 0
        while not self.stopping.is_set():
            pads = []
            if xinput is not None:
                for slot in range(4):
                    if xinput.XInputGetState(slot, ctypes.byref(state)) == 0:
                        pads.append(snapshot(state.Gamepad))
            self.status["xinput"] = len(pads)
            if sony is not None:
                try:
                    found = sony.read()
                except OSError as exc:
                    found = []
                    self.status["error"] = f"WinMM: {exc}"[:200]
                self.status["playstation"] = len(found)
                pads += found
            current = self.outgoing(pads)
            if current != self.last:
                self.last = current
                self._send(current)
            # Nobody plugged in: look again now and then rather than sixty
            # times a second.
            if not pads:
                idle_checks += 1
                self.stopping.wait(1.0 if idle_checks > 5 else self.INTERVAL)
            else:
                idle_checks = 0
                self.stopping.wait(self.INTERVAL)


BRIDGE: Bridge | None = None


def status() -> dict:
    return dict(BRIDGE.status) if BRIDGE is not None else {}


def start(window, streaming: bool = False) -> Bridge | None:
    global BRIDGE
    if not IS_WINDOWS:
        return None
    from . import embed

    def in_front() -> bool:
        host = embed.find_host()
        if not host:
            return False
        front = int(embed._GetForegroundWindow() or 0)
        return front == host or bool(front and embed._IsChild(host, front))

    BRIDGE = Bridge(window, in_front, streaming).start()
    return BRIDGE
