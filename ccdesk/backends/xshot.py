"""Screenshots without a subprocess: a frame read with XGrab, packed to RGB and written as a PNG with
stdlib zlib and struct.

`import -window root` takes 360 ms for 1920x1080 (it starts, connects, reads the root window, encodes);
here the capture is 2 ms (MIT-SHM), the RGB packing 3 ms (libpixman) or 15 ms (slice assignment when
libpixman is missing) and the encoding 20-25 ms.

Rows are written with filter type 0 (None) and zlib level 1: levels 2 to 6 save 1-15% of the file for
twice the time, and the filters need a per-pixel pass that has no C-speed equivalent in the stdlib.
Flat screens compress to a few hundred KB; a full-screen photo is the worst case, about the raw size
of the picture, as for any PNG without filters.
"""

from __future__ import annotations

import ctypes as C
import struct
import zlib
from dataclasses import dataclass

from ccdesk.backends import xpixman

ZLIB_LEVEL = 1
_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass
class Shot:
    """A picture as packed RGB: `rows` of width*3 bytes, `stride` bytes apart. Whoever scales it later
    (X11Backend.scale_png) reads it again from here instead of decoding the PNG."""
    data: bytes
    stride: int
    width: int
    height: int
    origin: tuple[int, int] = (0, 0)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))


def encode_png(shot: Shot, level: int = ZLIB_LEVEL) -> bytes:
    w, h, stride = shot.width, shot.height, shot.stride
    row = w * 3
    view = memoryview(shot.data)
    co = zlib.compressobj(level)
    zero = b"\x00"
    parts = []
    for y in range(h):
        parts.append(co.compress(zero + view[y * stride:y * stride + row]))
    parts.append(co.flush())
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return _SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", b"".join(parts)) + _chunk(b"IEND", b"")


def write_png(path: str, shot: Shot, level: int = ZLIB_LEVEL) -> None:
    with open(path, "wb") as fh:
        fh.write(encode_png(shot, level))


def _pack_rgb_python(view, width: int, height: int, bpl: int) -> bytes:
    """BGRX rows to RGB with strided slice assignment (about 15 ms for 1920x1080)."""
    raw = bytes(view[:bpl * height]) if bpl != width * 4 else bytes(view)
    if bpl != width * 4:  # drop the row padding
        raw = b"".join([raw[y * bpl:y * bpl + width * 4] for y in range(height)])
    rgb = bytearray(width * height * 3)
    rgb[0::3] = raw[2::4]
    rgb[1::3] = raw[1::4]
    rgb[2::3] = raw[0::4]
    return bytes(rgb)


def shot_of_frame(frame, scale_to: tuple[int, int] | None = None) -> Shot:
    """Pack (and, with scale_to, area-average down) a XGrab Frame into a Shot. Must run while the
    frame is valid, that is inside its `with` block."""
    out_w, out_h = scale_to or (frame.width, frame.height)
    pix = xpixman.load()
    if pix is not None:
        try:
            data, stride = pix.resample(frame.address, xpixman.X8R8G8B8, frame.width, frame.height, frame.bpl,
                                        out_w, out_h)
            return Shot(data, stride, out_w, out_h, (frame.x, frame.y))
        except OSError:
            pass
    if (out_w, out_h) != (frame.width, frame.height):
        raise OSError("no libpixman to scale with")
    return Shot(_pack_rgb_python(frame.view, frame.width, frame.height, frame.bpl), frame.width * 3,
                frame.width, frame.height, (frame.x, frame.y))


def rescale(shot: Shot, width: int, height: int) -> Shot:
    """The same picture area-averaged to width x height; needs libpixman (OSError without it)."""
    pix = xpixman.load()
    if pix is None:
        raise OSError("no libpixman to scale with")
    src = C.cast(C.c_char_p(shot.data), C.c_void_p).value
    data, stride = pix.resample(src, xpixman.R8G8B8_BYTES, shot.width, shot.height, shot.stride, width, height)
    return Shot(data, stride, width, height, shot.origin)
