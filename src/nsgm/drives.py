"""What kind of disk a folder lives on, and how much room is left on it.

Placing a game needs two facts per library folder: the free space, which
the standard library answers directly, and whether the disk underneath
is solid state, which every system answers differently:

Windows
    The storage driver is asked whether the volume "incurs a seek
    penalty". Spinning disks do and solid state ones do not. This is the
    same query Windows' own defragmenter uses to tell them apart, and it
    needs no administrator rights.
Linux
    ``/sys/block/<disk>/queue/rotational`` says it outright.
macOS
    ``diskutil info`` reports ``SolidState``.

Detection can fail (network shares, unusual RAID or USB bridges), so a
person can always override what was detected, and "unknown" is a real
answer rather than a guess.
"""

from __future__ import annotations

import os
import platform
import plistlib
import re
import shutil
import time
from pathlib import Path

from . import proc

SYSTEM = platform.system()

# Detection is a system call or a subprocess per volume; results do not
# change while the app runs, so they are kept for the session.
_KIND_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_SECONDS = 600


def nearest_existing(path: str | Path) -> Path | None:
    """The closest folder that exists, walking up from ``path``.

    A library folder that has not been created yet still tells us which
    disk it is on, as long as something above it exists.
    """
    try:
        current = Path(path).expanduser()
    except (TypeError, ValueError):
        return None
    for candidate in [current, *current.parents]:
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return None


def space(path: str | Path) -> dict:
    """Free and total bytes on the disk holding ``path``."""
    where = nearest_existing(path)
    if where is None:
        return {"free": 0, "total": 0, "ok": False}
    try:
        usage = shutil.disk_usage(where)
    except OSError:
        return {"free": 0, "total": 0, "ok": False}
    return {"free": usage.free, "total": usage.total, "ok": True}


def volume_key(path: str | Path) -> str:
    """An identifier shared by every folder on the same disk.

    Two library folders on one drive share its free space, which matters
    when deciding whether a game fits.
    """
    where = nearest_existing(path)
    if where is None:
        return str(path)
    if SYSTEM == "Windows":
        drive = os.path.splitdrive(str(where.resolve()))[0]
        return drive.upper() or str(where)
    try:
        return f"dev:{os.stat(where).st_dev}"
    except OSError:
        return str(where)


# --------------------------------------------------------------------------
# solid state or spinning
# --------------------------------------------------------------------------

def _windows_kind(path: Path) -> dict:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    drive = os.path.splitdrive(str(path.resolve()))[0]
    if not re.fullmatch(r"[A-Za-z]:", drive):
        return {"kind": "unknown", "detail": "not on a lettered drive"}

    INVALID = ctypes.c_void_p(-1).value
    # Access 0 is enough to ask the driver about the device, which is what
    # keeps this working without administrator rights.
    handle = kernel32.CreateFileW(
        f"\\\\.\\{drive}", 0, 0x1 | 0x2, None, 3, 0, None,
    )
    if not handle or handle == INVALID:
        return {"kind": "unknown", "detail": "the drive could not be queried"}

    IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400

    class Query(ctypes.Structure):
        _fields_ = [("PropertyId", wintypes.DWORD), ("QueryType", wintypes.DWORD),
                    ("AdditionalParameters", ctypes.c_ubyte * 4)]

    class SeekPenalty(ctypes.Structure):
        _fields_ = [("Version", wintypes.DWORD), ("Size", wintypes.DWORD),
                    ("IncursSeekPenalty", wintypes.BOOLEAN)]

    class DeviceHeader(ctypes.Structure):
        _fields_ = [("Version", wintypes.DWORD), ("Size", wintypes.DWORD),
                    ("DeviceType", ctypes.c_ubyte), ("DeviceTypeModifier", ctypes.c_ubyte),
                    ("RemovableMedia", wintypes.BOOLEAN), ("CommandQueueing", wintypes.BOOLEAN),
                    ("VendorIdOffset", wintypes.DWORD), ("ProductIdOffset", wintypes.DWORD),
                    ("ProductRevisionOffset", wintypes.DWORD),
                    ("SerialNumberOffset", wintypes.DWORD), ("BusType", wintypes.DWORD),
                    ("RawPropertiesLength", wintypes.DWORD)]

    BUS = {1: "SCSI", 3: "ATA", 7: "USB", 8: "RAID", 10: "SAS", 11: "SATA",
           12: "SD card", 13: "MMC", 14: "virtual", 15: "virtual", 16: "Storage Spaces",
           17: "NVMe", 18: "SCM", 19: "UFS"}

    try:
        returned = wintypes.DWORD(0)
        bus = ""
        query = Query(0, 0)            # StorageDeviceProperty, standard query
        # The descriptor is followed by variable-length strings, so the
        # buffer is generous rather than exactly the size of the header.
        buffer = ctypes.create_string_buffer(1024)
        if kernel32.DeviceIoControl(handle, IOCTL_STORAGE_QUERY_PROPERTY,
                                    ctypes.byref(query), ctypes.sizeof(query),
                                    buffer, ctypes.sizeof(buffer),
                                    ctypes.byref(returned), None) \
                and returned.value >= ctypes.sizeof(DeviceHeader):
            header = DeviceHeader.from_buffer_copy(buffer.raw[:ctypes.sizeof(DeviceHeader)])
            bus = BUS.get(int(header.BusType), "")

        query = Query(7, 0)            # StorageDeviceSeekPenaltyProperty
        penalty = SeekPenalty()
        ok = kernel32.DeviceIoControl(handle, IOCTL_STORAGE_QUERY_PROPERTY,
                                      ctypes.byref(query), ctypes.sizeof(query),
                                      ctypes.byref(penalty), ctypes.sizeof(penalty),
                                      ctypes.byref(returned), None)
    finally:
        kernel32.CloseHandle(handle)

    if bus == "NVMe":
        return {"kind": "ssd", "detail": "NVMe"}
    if not ok:
        return {"kind": "unknown", "detail": bus or "the drive did not say"}
    if penalty.IncursSeekPenalty:
        return {"kind": "hdd", "detail": bus}
    return {"kind": "ssd", "detail": bus}


def _linux_kind(path: Path) -> dict:
    try:
        device = os.stat(path).st_dev
    except OSError:
        return {"kind": "unknown", "detail": ""}
    node = Path(f"/sys/dev/block/{os.major(device)}:{os.minor(device)}")
    try:
        real = node.resolve()
    except OSError:
        return {"kind": "unknown", "detail": ""}
    # A partition's own folder has no queue/; its parent disk does. Device
    # mapper and similar stack several layers, so a slave is followed too.
    for candidate in (real, real.parent):
        flag = candidate / "queue" / "rotational"
        if flag.is_file():
            try:
                rotational = flag.read_text().strip() == "1"
            except OSError:
                break
            name = candidate.name
            return {"kind": "hdd" if rotational else "ssd",
                    "detail": "NVMe" if name.startswith("nvme") else ""}
    slaves = real / "slaves"
    if slaves.is_dir():
        for slave in slaves.iterdir():
            flag = Path("/sys/block") / slave.name / "queue" / "rotational"
            if flag.is_file():
                try:
                    return {"kind": "hdd" if flag.read_text().strip() == "1" else "ssd",
                            "detail": ""}
                except OSError:
                    break
    return {"kind": "unknown", "detail": "the disk did not say"}


def _mac_kind(path: Path) -> dict:
    try:
        result = proc.run(["diskutil", "info", "-plist", str(path)],
                          capture_output=True, timeout=10)
    except (OSError, ValueError):
        return {"kind": "unknown", "detail": ""}
    if result.returncode != 0:
        return {"kind": "unknown", "detail": ""}
    try:
        info = plistlib.loads(result.stdout)
    except Exception:
        return {"kind": "unknown", "detail": ""}
    solid = info.get("SolidState")
    protocol = str(info.get("BusProtocol") or info.get("DeviceProtocol") or "")
    if solid is True:
        return {"kind": "ssd", "detail": protocol}
    if solid is False:
        return {"kind": "hdd", "detail": protocol}
    return {"kind": "unknown", "detail": protocol}


def detect_kind(path: str | Path) -> dict:
    """``{"kind": "ssd" | "hdd" | "unknown", "detail": "NVMe"}`` for a folder."""
    where = nearest_existing(path)
    if where is None:
        return {"kind": "unknown", "detail": "that drive is not connected"}
    key = volume_key(where)
    cached = _KIND_CACHE.get(key)
    if cached and time.time() - cached[0] < _CACHE_SECONDS:
        return dict(cached[1])
    try:
        if SYSTEM == "Windows":
            found = _windows_kind(where)
        elif SYSTEM == "Darwin":
            found = _mac_kind(where)
        else:
            found = _linux_kind(where)
    except Exception as exc:          # a query that fails is just unknown
        found = {"kind": "unknown", "detail": str(exc)[:60]}
    _KIND_CACHE[key] = (time.time(), found)
    return dict(found)


def describe(folder: dict) -> dict:
    """A library folder with everything placement needs filled in."""
    path = str(folder.get("path") or "")
    detected = detect_kind(path) if path else {"kind": "unknown", "detail": ""}
    chosen = str(folder.get("kind") or "auto").lower()
    kind = detected["kind"] if chosen == "auto" else chosen
    room = space(path) if path else {"free": 0, "total": 0, "ok": False}
    try:
        reserve_gb = max(0.0, float(folder.get("reserve_gb", 10)))
    except (TypeError, ValueError):
        reserve_gb = 10.0
    return {
        "path": path,
        "kind_setting": chosen if chosen in ("auto", "ssd", "hdd") else "auto",
        "kind": kind if kind in ("ssd", "hdd") else "unknown",
        "detected": detected["kind"],
        "detail": detected.get("detail", ""),
        "free": room["free"],
        "total": room["total"],
        "connected": room["ok"],
        "exists": Path(path).expanduser().is_dir() if path else False,
        "reserve_gb": reserve_gb,
        "enabled": folder.get("enabled", True) is not False,
        "volume": volume_key(path) if path else "",
    }
