"""Run helper commands without a console window flashing on screen.

A windowed program on Windows has no console, so starting a console
command creates one for it. That window appears, blinks and closes, which
is what made the app look like it launched a terminal at startup.

Everything internal goes through here. Commands meant to be seen - an
installer the person asked to run - deliberately do not.
"""

from __future__ import annotations

import os
import subprocess

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _hidden(kwargs: dict) -> dict:
    if os.name == "nt":
        kwargs.setdefault("creationflags", NO_WINDOW)
        if "startupinfo" not in kwargs:
            info = subprocess.STARTUPINFO()
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            info.wShowWindow = subprocess.SW_HIDE
            kwargs["startupinfo"] = info
    return kwargs


def run(*args, **kwargs):
    """subprocess.run, with no console window on Windows."""
    return subprocess.run(*args, **_hidden(kwargs))


def popen(*args, **kwargs):
    """subprocess.Popen, with no console window on Windows."""
    return subprocess.Popen(*args, **_hidden(kwargs))


# Callers written against subprocess keep its spelling.
Popen = popen
