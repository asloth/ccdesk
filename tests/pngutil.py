"""Minimal stdlib PNG reader/writer for tests (8-bit RGB/RGBA, non-interlaced)."""

from __future__ import annotations

import struct
import zlib

SIG = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def write_png(path, width: int, height: int, color=(0, 0, 0), rects=()) -> None:
    """A solid image, with rects (x, y, w, h, rgb) painted on top."""
    rows = [bytearray(bytes(color) * width) for _ in range(height)]
    for x, y, w, h, rgb in rects:
        for yy in range(max(0, y), min(height, y + h)):
            for xx in range(max(0, x), min(width, x + w)):
                rows[yy][xx * 3:xx * 3 + 3] = bytes(rgb)
    write_rows(path, width, height, rows)


def write_rows(path, width: int, height: int, rows) -> None:
    """8-bit RGB rows, filter type 0."""
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    data = SIG + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(data)


class Image:
    def __init__(self, width: int, height: int, channels: int, rows: list[bytes]):
        self.width, self.height, self.channels, self.rows = width, height, channels, rows

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        i = x * self.channels
        return tuple(self.rows[y][i:i + 3])

    def find_color(self, rgb, tolerance: int = 8):
        """Bounding box (x0, y0, x1, y1) of pixels close to rgb, or None."""
        x0 = y0 = 10**9
        x1 = y1 = -1
        for y, row in enumerate(self.rows):
            for x in range(self.width):
                i = x * self.channels
                if all(abs(row[i + c] - rgb[c]) <= tolerance for c in range(3)):
                    x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x), max(y1, y)
        return None if x1 < 0 else (x0, y0, x1, y1)


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def read_png(path) -> Image:
    with open(path, "rb") as fh:
        data = fh.read()
    assert data[:8] == SIG, "not a PNG"
    pos, idat, ihdr = 8, b"", None
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    width, height, depth, ctype, _, _, interlace = ihdr
    assert depth == 8 and ctype in (2, 6) and interlace == 0, f"unsupported PNG {ihdr}"
    ch = 3 if ctype == 2 else 4
    raw = zlib.decompress(idat)
    stride = width * ch
    rows, prev = [], bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        ftype, line = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        for i in range(stride):
            a = line[i - ch] if i >= ch else 0
            b = prev[i]
            c = prev[i - ch] if i >= ch else 0
            if ftype == 1:
                line[i] = (line[i] + a) & 0xFF
            elif ftype == 2:
                line[i] = (line[i] + b) & 0xFF
            elif ftype == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 0xFF
            elif ftype == 4:
                line[i] = (line[i] + _paeth(a, b, c)) & 0xFF
        rows.append(bytes(line))
        prev = line
    return Image(width, height, ch, rows)
