"""Did the screen change? Coarse grayscale grids of two captures, compared.

Without an accessibility tree there is no text to diff, so an action compares a small copy of
the window from before and after it. ImageMagick does the shrinking (Backend.scale_png); this
module only decodes the small PNG and compares, with the standard library.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

THRESHOLD = 6  # mean gray difference (0-255) of a cell that counts as a change


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def read_gray(path) -> tuple[int, int, bytes]:
    """Width, height and one gray byte per pixel of an 8-bit, non-interlaced PNG."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} is not a PNG file")
    pos, idat, ihdr = 8, [], None
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    if ihdr is None:
        raise ValueError(f"{path} has no IHDR")
    width, height, depth, ctype, _, _, interlace = ihdr
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(ctype)
    if depth != 8 or channels is None or interlace:
        raise ValueError(f"unsupported PNG (depth {depth}, colour type {ctype}, interlace {interlace})")
    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    gray = bytearray(width * height)
    prev = bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        ftype, line = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        if ftype == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:
            line = bytearray((a + b) & 0xFF for a, b in zip(line, prev))
        elif ftype == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                up_left = prev[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(left, prev[i], up_left)) & 0xFF
        row = y * width
        if channels >= 3:
            for x in range(width):
                i = x * channels
                gray[row + x] = (line[i] * 299 + line[i + 1] * 587 + line[i + 2] * 114) // 1000
        else:
            gray[row:row + width] = line[::channels]
        prev = line
    return width, height, bytes(gray)


@dataclass(frozen=True)
class Grid:
    width: int
    height: int
    gray: bytes
    origin: tuple[int, int] = (0, 0)  # screen position of the captured region
    scale: tuple[float, float] = (1.0, 1.0)  # screen pixels per grid cell

    @classmethod
    def from_png(cls, path, origin=(0, 0), cell: int = 1, scale: tuple[float, float] | None = None) -> "Grid":
        """Read a capture; cell > 1 averages cell x cell blocks here (slow on big images: shrink first)."""
        width, height, gray = read_gray(path)
        if cell > 1:
            cols, rows = max(1, width // cell), max(1, height // cell)
            out = bytearray(cols * rows)
            for cy in range(rows):
                for cx in range(cols):
                    total = sum(sum(gray[y * width + cx * cell:y * width + cx * cell + cell])
                                for y in range(cy * cell, min(height, cy * cell + cell)))
                    out[cy * cols + cx] = total // (cell * cell)
            width, height, gray = cols, rows, bytes(out)
        return cls(width, height, gray, tuple(origin), scale or (float(cell), float(cell)))


def changed_box(before: Grid, after: Grid, threshold: int = THRESHOLD) -> tuple[int, int, int, int] | None:
    """Screen box (x, y, width, height) around what changed, or None.

    A changed cell counts only next to another changed cell in its row: a blinking text caret is
    one cell wide and would otherwise make every action look like it changed something."""
    sx, sy = after.scale
    ox, oy = after.origin
    if (before.width, before.height, before.origin) != (after.width, after.height, after.origin):
        width = max(before.width * before.scale[0], after.width * sx)
        height = max(before.height * before.scale[1], after.height * sy)
        return ox, oy, round(width), round(height)  # the window moved or resized
    w = after.width
    hot = [abs(p - q) > threshold for p, q in zip(before.gray, after.gray)]
    x0 = y0 = 1 << 30
    x1 = y1 = -1
    for i, on in enumerate(hot):
        if not on:
            continue
        x, y = i % w, i // w
        if not ((x > 0 and hot[i - 1]) or (x < w - 1 and hot[i + 1])):
            continue
        x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x), max(y1, y)
    if x1 < 0:
        return None
    return ox + round(x0 * sx), oy + round(y0 * sy), round((x1 - x0 + 1) * sx), round((y1 - y0 + 1) * sy)
