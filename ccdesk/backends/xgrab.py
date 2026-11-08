"""A fast grayscale grid of part of the X screen, for the before/after comparison of an action.

`import -window root` copies the whole root window, crops it, encodes a PNG and ImageMagick then
shrinks it: about 220 ms on a 3840x1080 screen, twice per action. XGetImage of just the region and
averaging 8x8 blocks here takes about 40 ms. Only the comparison uses this; screenshots the agent
sees still go through the PNG tools.

libX11 through ctypes (stdlib only), with argtypes on every call, one Display per process and
one lock around it: tool calls run on worker threads.
"""

from __future__ import annotations

import ctypes as C
import ctypes.util
import sys
import threading

from ccdesk.backends import xerror

ZPIXMAP = 2
ALL_PLANES = 0xFFFFFFFF


class _XImage(C.Structure):
    _fields_ = [("width", C.c_int), ("height", C.c_int), ("xoffset", C.c_int), ("format", C.c_int),
                ("data", C.c_void_p), ("byte_order", C.c_int), ("bitmap_unit", C.c_int),
                ("bitmap_bit_order", C.c_int), ("bitmap_pad", C.c_int), ("depth", C.c_int),
                ("bytes_per_line", C.c_int), ("bits_per_pixel", C.c_int)]


_WEIGHTS = (1, 4, 2)  # B, G, R: (2R + 4G + B) / 7, close to Rec.601 luma
_LO = [bytes((v * w) & 0xFF for v in range(256)) for w in _WEIGHTS]
_HI = [bytes((v * w) >> 8 for v in range(256)) for w in _WEIGHTS]


def average_cells(buf: bytes, bpl: int, width: int, height: int, cell: int) -> tuple[int, int, bytes]:
    """Gray mean of each cell x cell block of a 32-bit little-endian ZPixmap (bytes B, G, R, pad),
    weighted (2R + 4G + B) / 7 so red, green and blue all differ, as in ImageMagick's gray.
    Every other pixel row is read: a change under two rows is below what the comparison looks for.

    Pure Python, so the per-pixel work stays in C: each row's weighted channels go into 16-bit
    lanes of one bytearray (slice assignment and bytes.translate), the rows of a cell are added
    as one big int, and only the per-cell sums loop in Python (about 30 ms for 1912x1016)."""
    cols, rows = max(1, width // cell), max(1, height // cell)
    step = 2 if cell >= 4 else 1
    n = len(range(0, cell, step)) * cell  # pixels read per cell
    used = cols * cell * 4  # bytes of a row inside whole cells
    lanes = bytearray(used * 2)  # one 16-bit lane per byte; the pad byte's lane stays 0
    out = bytearray(cols * rows)
    div, k = 7 * n, cell * 4
    for cy in range(rows):
        total = 0
        for y in range(cy * cell, cy * cell + cell, step):
            row = buf[y * bpl:y * bpl + used]
            for c in range(3):
                ch = row[c::4]
                lanes[2 * c::8] = ch.translate(_LO[c])
                lanes[2 * c + 1::8] = ch.translate(_HI[c])
            total += int.from_bytes(lanes, "little")  # lanes never carry: at most 4 * 4 * 255 * 2
        mv = memoryview(total.to_bytes(used * 2, "little")).cast("H")
        base = cy * cols
        for cx in range(cols):
            v = sum(mv[cx * k:cx * k + k]) // div
            out[base + cx] = 255 if v > 255 else v
    return cols, rows, bytes(out)


class XGrab:
    def __init__(self, display: str):
        self.display = display
        self._lock = threading.Lock()
        self._x = None
        self._dpy = None
        self._error: str | None = None
        self.broken: str | None = None  # why it cannot be used; callers fall back to the PNG path

    def _open(self) -> None:
        name = ctypes.util.find_library("X11") or "libX11.so.6"
        x = C.CDLL(name)
        v, i, u, ul, ci = C.c_void_p, C.c_int, C.c_uint, C.c_ulong, C.c_char_p

        def sig(fn, restype, *argtypes):
            fn.restype, fn.argtypes = restype, list(argtypes)

        sig(x.XOpenDisplay, v, ci)
        sig(x.XDefaultRootWindow, ul, v)
        sig(x.XDefaultScreen, i, v)
        sig(x.XDisplayWidth, i, v, i)
        sig(x.XDisplayHeight, i, v, i)
        sig(x.XGetImage, v, v, ul, i, i, u, u, ul, i)
        sig(x.XFree, i, v)
        sig(x.XSync, i, v, i)
        sig(x.XSetErrorHandler, v, v)
        dpy = x.XOpenDisplay(self.display.encode())
        if not dpy:
            raise OSError(f"cannot open display {self.display}")
        xerror.register(x, dpy, self._on_error)
        self._x, self._dpy = x, dpy
        self._root = x.XDefaultRootWindow(dpy)

    def _on_error(self, message: str) -> None:
        self._error = message

    def gray(self, region, cell: int) -> tuple[int, int, bytes, tuple[int, int]] | None:
        """(cols, rows, gray, (x, y)) for region (None: the whole screen), or None when it cannot
        be read this way (then the caller uses the PNG path)."""
        if self.broken:
            return None
        with self._lock:
            try:
                if self._dpy is None:
                    self._open()
                x, dpy = self._x, self._dpy
                scr = x.XDefaultScreen(dpy)
                sw, sh = x.XDisplayWidth(dpy, scr), x.XDisplayHeight(dpy, scr)
                if region is None:
                    rx, ry, rw, rh = 0, 0, sw, sh
                else:
                    rx, ry = max(0, region.x), max(0, region.y)
                    rw = min(region.x + region.width, sw) - rx
                    rh = min(region.y + region.height, sh) - ry
                if rw < cell or rh < cell:
                    return None
                self._error = None
                p = x.XGetImage(dpy, self._root, rx, ry, rw, rh, ALL_PLANES, ZPIXMAP)
                if not p:
                    x.XSync(dpy, 0)
                    return None
                im = _XImage.from_address(p)
                data = im.data
                try:
                    if im.bits_per_pixel != 32 or im.depth not in (24, 32) or im.byte_order != 0 \
                            or sys.byteorder != "little":
                        self.broken = f"unusual pixel format ({im.bits_per_pixel} bpp, depth {im.depth})"
                        return None
                    bpl, width, height = im.bytes_per_line, im.width, im.height
                    buf = C.string_at(data, bpl * height)
                finally:
                    # XDestroyImage is a macro in Xlib.h: free the pixels, then the struct, by hand.
                    x.XFree(C.c_void_p(data))
                    x.XFree(C.c_void_p(p))
                cols, rows, gray = average_cells(buf, bpl, width, height, cell)
                return cols, rows, gray, (rx, ry)
            except Exception as exc:  # ctypes.ArgumentError too: never break an action, use the PNG path
                self.broken = f"{type(exc).__name__}: {exc}"
                return None
