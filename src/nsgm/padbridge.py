"""Read Xbox-style controllers directly, for when the window's own reading fails.

The store normally reads the controller through the browser engine inside
its window (the Gamepad API). That engine gets Xbox-type pads from
Windows.Gaming.Input, which can stop delivering them to a program running
as administrator - exactly how the store runs when Sunshine starts it
elevated, and then the controller does nothing.

XInput has no such limit. This polls it about sixty times a second and
hands the state to the page, which uses it whenever the engine reports no
controller. Sunshine's emulated Xbox 360 pad is an XInput pad; so are Xbox
pads and most others.

Only while the store's window is in front: a game started from the store
must not also be driving the store behind it.
"""

from __future__ import annotations

import json
import os
import threading

IS_WINDOWS = os.name == "nt"

# XInput button bits, in the order of the browser's standard gamepad layout.
STANDARD = [0x1000, 0x2000, 0x4000, 0x8000,     # A B X Y
            0x0100, 0x0200, None, None,          # LB RB LT RT (triggers are analogue)
            0x0020, 0x0010, 0x0040, 0x0080,      # Back Start LS RS
            0x0001, 0x0002, 0x0004, 0x0008]      # up down left right

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


def snapshot(pad) -> dict:
    """One XInput pad as the page's gamepad shape: buttons and four axes."""
    bits = pad.wButtons
    buttons = [bool(bits & bit) if bit else False for bit in STANDARD]
    buttons[6] = pad.bLeftTrigger > 30
    buttons[7] = pad.bRightTrigger > 30

    def axis(value: int, flip: bool = False) -> float:
        number = max(-1.0, min(1.0, value / 32767.0))
        number = -number if flip else number
        return 0.0 if abs(number) < 0.2 else round(number, 2)

    return {"buttons": buttons,
            "axes": [axis(pad.sThumbLX), axis(pad.sThumbLY, True),
                     axis(pad.sThumbRX), axis(pad.sThumbRY, True)]}


def merge(states: list) -> dict | None:
    """Several pads as one, the way the page treats them."""
    if not states:
        return None
    buttons = [any(s["buttons"][i] for s in states) for i in range(16)]
    axes = []
    for i in range(4):
        values = [s["axes"][i] for s in states]
        axes.append(max(values, key=abs))
    return {"buttons": buttons, "axes": axes}


class Bridge:
    INTERVAL = 1 / 60

    def __init__(self, window, in_front):
        self.window = window
        self.in_front = in_front
        self.stopping = threading.Event()
        self.last = None

    def start(self) -> "Bridge":
        if IS_WINDOWS:
            threading.Thread(target=self._run, daemon=True, name="pad-bridge").start()
        return self

    def _send(self, state) -> None:
        script = f"window.nativePad && window.nativePad({json.dumps(state)})"
        runner = getattr(self.window, "run_js", None) or self.window.evaluate_js
        try:
            runner(script)
        except Exception:
            pass

    def _run(self) -> None:
        dll = _library()
        if dll is None:
            return
        state = XINPUT_STATE()
        idle_checks = 0
        while not self.stopping.is_set():
            pads = []
            for slot in range(4):
                if dll.XInputGetState(slot, ctypes.byref(state)) == 0:
                    pads.append(snapshot(state.Gamepad))
            current = merge(pads)
            if current is not None and not self.in_front():
                current = {"buttons": [False] * 16, "axes": [0.0] * 4}
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


def start(window) -> Bridge | None:
    if not IS_WINDOWS:
        return None
    from . import embed

    def in_front() -> bool:
        host = embed.find_host()
        if not host:
            return False
        front = int(embed._GetForegroundWindow() or 0)
        return front == host or bool(front and embed._IsChild(host, front))

    return Bridge(window, in_front).start()
