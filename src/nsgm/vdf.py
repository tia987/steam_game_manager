"""Read and write Valve's VDF formats.

Two dialects are needed:

* Binary VDF - ``shortcuts.vdf``, the file that holds non-Steam shortcuts.
* Text VDF   - ``loginusers.vdf`` and ``libraryfolders.vdf``.

Both are implemented here with no third-party dependencies.

Binary layout
-------------
A stream of typed nodes. Every node starts with a one byte type marker,
followed by a NUL terminated key, followed by a payload that depends on
the type::

    0x00  nested map, contents follow, terminated by 0x08
    0x01  string, NUL terminated
    0x02  signed 32 bit little endian integer
    0x07  unsigned 64 bit little endian integer
    0x08  end of the current map

``shortcuts.vdf`` is a single root map named ``shortcuts`` whose keys are
the decimal indices "0", "1", "2" ... so the file ends with two 0x08
bytes: one closing ``shortcuts`` and one closing the implicit root.
"""

from __future__ import annotations

import struct
from typing import Any

MAP = 0x00
STR = 0x01
INT32 = 0x02
UINT64 = 0x07
END = 0x08


class VDFError(ValueError):
    """Raised when a VDF stream cannot be understood."""


# --------------------------------------------------------------------------
# binary
# --------------------------------------------------------------------------

def _read_cstr(data: bytes, i: int) -> tuple[str, int]:
    end = data.find(b"\x00", i)
    if end < 0:
        raise VDFError(f"unterminated string starting at byte {i}")
    return data[i:end].decode("utf-8", "replace"), end + 1


def _read_map(data: bytes, i: int) -> tuple[dict, int]:
    out: dict[str, Any] = {}
    while True:
        if i >= len(data):
            raise VDFError("stream ended inside a map")
        kind = data[i]
        i += 1
        if kind == END:
            return out, i
        key, i = _read_cstr(data, i)
        if kind == MAP:
            out[key], i = _read_map(data, i)
        elif kind == STR:
            out[key], i = _read_cstr(data, i)
        elif kind == INT32:
            (out[key],) = struct.unpack_from("<i", data, i)
            i += 4
        elif kind == UINT64:
            (out[key],) = struct.unpack_from("<Q", data, i)
            i += 8
        else:
            raise VDFError(f"unknown type marker 0x{kind:02x} at byte {i - 1}")


def binary_loads(data: bytes) -> dict:
    """Parse a binary VDF blob into nested dicts."""
    if not data:
        return {}
    root, _ = _read_map(data, 0)
    return root


def _write_value(buf: bytearray, key: str, value: Any) -> None:
    kb = key.encode("utf-8") + b"\x00"
    if isinstance(value, dict):
        buf += bytes([MAP]) + kb
        for k, v in value.items():
            _write_value(buf, k, v)
        buf += bytes([END])
    elif isinstance(value, bool):
        buf += bytes([INT32]) + kb + struct.pack("<i", 1 if value else 0)
    elif isinstance(value, int):
        if -(2 ** 31) <= value < 2 ** 31:
            buf += bytes([INT32]) + kb + struct.pack("<i", value)
        else:
            buf += bytes([UINT64]) + kb + struct.pack("<Q", value & (2 ** 64 - 1))
    else:
        buf += bytes([STR]) + kb + str(value).encode("utf-8") + b"\x00"


def binary_dumps(obj: dict) -> bytes:
    """Serialise nested dicts back into a binary VDF blob."""
    buf = bytearray()
    for k, v in obj.items():
        _write_value(buf, k, v)
    buf += bytes([END])
    return bytes(buf)


# --------------------------------------------------------------------------
# text
# --------------------------------------------------------------------------

def _tokenize(text: str):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = n if nl < 0 else nl + 1
            continue
        if c == '"':
            i += 1
            out = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    out.append({"n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(nxt, nxt))
                    i += 2
                else:
                    out.append(text[i])
                    i += 1
            i += 1
            yield "".join(out)
            continue
        if c in "{}":
            i += 1
            yield c
            continue
        start = i
        while i < n and text[i] not in ' \t\r\n"{}':
            i += 1
        yield text[start:i]


def text_loads(text: str) -> dict:
    """Parse a text VDF document into nested dicts. Duplicate keys are merged."""
    tokens = list(_tokenize(text))
    pos = 0

    def parse_map() -> dict:
        nonlocal pos
        out: dict[str, Any] = {}
        while pos < len(tokens):
            tok = tokens[pos]
            if tok == "}":
                pos += 1
                return out
            pos += 1
            if pos >= len(tokens):
                break
            nxt = tokens[pos]
            if nxt == "{":
                pos += 1
                child = parse_map()
                if isinstance(out.get(tok), dict):
                    out[tok].update(child)
                else:
                    out[tok] = child
            else:
                pos += 1
                out[tok] = nxt
        return out

    return parse_map()
