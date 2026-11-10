"""Pixels of the X screen read in-process: a fast grayscale grid for the before/after comparison of an
action, and raw frames for xshot.py's screenshots.

`import -window root` copies the whole root window, crops it, encodes a PNG and ImageMagick then
shrinks it: about 220 ms on a 3840x1080 screen, twice per action. Here the region is read with
MIT-SHM (XShmGetImage into a shared segment: 2 ms for 1920x1080, against 15-20 ms for XGetImage,
which sends every pixel through the socket) and 8x8 blocks are averaged in about 10 ms. When the
extension or the segment is not there (a remote display, a server without it, no libXext) it falls
back to XGetImage, and when even that fails callers use the PNG tools.

libX11 and libXext through ctypes (stdlib only), with argtypes on every call, one Display per
process and one lock around it: tool calls run on worker threads.
"""

from __future__ import annotations

import contextlib
import ctypes as C
import ctypes.util
import sys
import threading
from dataclasses import dataclass

from ccdesk.backends import xerror

ZPIXMAP = 2
ALL_PLANES = 0xFFFFFFFF


class _XImage(C.Structure):
    _fields_ = [("width", C.c_int), ("height", C.c_int), ("xoffset", C.c_int), ("format", C.c_int),
                ("data", C.c_void_p), ("byte_order", C.c_int), ("bitmap_unit", C.c_int),
                ("bitmap_bit_order", C.c_int), ("bitmap_pad", C.c_int), ("depth", C.c_int),
                ("bytes_per_line", C.c_int), ("bits_per_pixel", C.c_int), ("red_mask", C.c_ulong),
                ("green_mask", C.c_ulong), ("blue_mask", C.c_ulong)]


class _ShmSegment(C.Structure):  # XShmSegmentInfo
    _fields_ = [("shmseg", C.c_ulong), ("shmid", C.c_int), ("shmaddr", C.c_void_p), ("readOnly", C.c_int)]


@dataclass
class Frame:
    """Pixels of a region as 32-bit little-endian ZPixmap rows (bytes B, G, R, pad). `view` is valid
    only inside the `with grab.frame(...)` block: it may be the shared segment, reused by the next
    capture."""
    view: memoryview
    address: int  # of the first byte, for libpixman
    width: int
    height: int
    bpl: int
    x: int
    y: int


_WEIGHTS = (1, 4, 2)  # B, G, R: (2R + 4G + B) / 7, close to Rec.601 luma
_LO = [bytes((v * w) & 0xFF for v in range(256)) for w in _WEIGHTS]
_HI = [bytes((v * w) >> 8 for v in range(256)) for w in _WEIGHTS]


def _average_cells_rows(buf, bpl: int, width: int, height: int, cell: int) -> tuple[int, int, bytes]:
    """Gray mean of each cell x cell block of a 32-bit little-endian ZPixmap (bytes B, G, R, pad),
    weighted (2R + 4G + B) / 7 so red, green and blue all differ, as in ImageMagick's gray.
    Every other pixel row is read: a change under two rows is below what the comparison looks for.

    Pure Python, so the per-pixel work stays in C: each row's weighted channels go into 16-bit
    lanes of one bytearray (slice assignment and bytes.translate), the rows of a cell are added
    as one big int, and only the per-cell sums loop in Python (about 30 ms for 1912x1016)."""
    buf = bytes(buf)
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


_MASKS: dict[int, int] = {}


def _mask(words: int) -> int:
    """0x00FF00FF repeated over `words` 32-bit words: keeps bytes 0 and 2 of each pixel."""
    m = _MASKS.get(words)
    if m is None:
        if len(_MASKS) > 8:
            _MASKS.clear()
        m = _MASKS[words] = int.from_bytes(b"\xff\x00\xff\x00" * words, "little")
    return m


def _hsum(t: int, cell: int) -> int:
    """Word x of the result holds the sum of words x .. x+cell-1 (32-bit words, little-endian int)."""
    if cell & (cell - 1) == 0:
        k = 1
        while k < cell:
            t += t >> (32 * k)
            k *= 2
        return t
    return sum(t >> (32 * k) for k in range(cell))


def average_cells(buf, bpl: int, width: int, height: int, cell: int) -> tuple[int, int, bytes]:
    """Gray mean of each cell x cell block of a 32-bit little-endian ZPixmap (bytes B, G, R, pad),
    weighted (2R + 4G + B) / 7 so red, green and blue all differ, as in ImageMagick's gray.
    Every other pixel row is read: a change under two rows is below what the comparison looks for.

    Pure Python with the per-pixel work in big-int arithmetic. The rows a cell reads are joined
    into one int per row phase; B and R, and G and the pad byte, go into separate 16-bit lanes
    (a lane holds the sum of up to 257 samples), so the vertical sum is a few int additions and
    the horizontal sum is shift-and-add. Only the cell corners are read back, so the Python loop
    runs once per cell, not per pixel (about 8 ms for 1912x1016, from 30 ms with a loop per row)."""
    cols, rows = max(1, width // cell), max(1, height // cell)
    step = 2 if cell >= 4 else 1
    phases = list(range(0, cell, step))
    n = len(phases) * cell  # pixels read per cell
    if n > 257 or cols * cell > width or rows * cell > height:
        return _average_cells_rows(buf, bpl, width, height, cell)
    used = cols * cell * 4  # bytes of a row inside whole cells
    words = cols * cell
    mask = _mask(rows * words)
    e_sum = o_sum = 0
    for ph in phases:
        part = b"".join([buf[(cy * cell + ph) * bpl:(cy * cell + ph) * bpl + used] for cy in range(rows)])
        v = int.from_bytes(part, "little")
        e_sum += v & mask  # B and R of each pixel
        o_sum += (v >> 8) & mask  # G (the pad byte lands in the lane above it)
    e_sum, o_sum = _hsum(e_sum, cell), _hsum(o_sum, cell)
    size = rows * words * 4
    ev = memoryview(e_sum.to_bytes(size, "little")).cast("I")
    ov = memoryview(o_sum.to_bytes(size, "little")).cast("I")
    ce = b"".join([ev[cy * words:(cy + 1) * words:cell].tobytes() for cy in range(rows)])
    co = b"".join([ov[cy * words:(cy + 1) * words:cell].tobytes() for cy in range(rows)])
    m16 = int.from_bytes(b"\xff\xff\x00\x00" * (cols * rows), "little")
    ei, oi = int.from_bytes(ce, "little"), int.from_bytes(co, "little")
    gray = (ei & m16) + 4 * (oi & m16) + 2 * (ei >> 16 & m16)  # B + 4G + 2R in 32-bit lanes
    mv = memoryview(gray.to_bytes(cols * rows * 4, "little")).cast("I")
    div = 7 * n
    return cols, rows, bytes(map(div.__rfloordiv__, mv))


class XGrab:
    def __init__(self, display: str, shm: bool = True):
        self.display = display
        self._lock = threading.Lock()
        self._x = None
        self._dpy = None
        self._error: str | None = None
        self.broken: str | None = None  # why it cannot be used; callers fall back to the PNG path
        self.use_shm = shm
        self.shm_note: str | None = None  # why MIT-SHM is not in use, when it is not
        self._shm = None  # (image, segment, address, capacity in bytes, view)

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
        sig(x.XDefaultVisual, v, v, i)
        sig(x.XDefaultDepth, i, v, i)
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
        self._open_shm()

    def _open_shm(self) -> None:
        if not self.use_shm:
            self.shm_note = "disabled"
            return
        try:
            e = C.CDLL(ctypes.util.find_library("Xext") or "libXext.so.6")
            libc = C.CDLL(None)
            v, i, u, ul = C.c_void_p, C.c_int, C.c_uint, C.c_ulong
            e.XShmQueryExtension.restype, e.XShmQueryExtension.argtypes = i, [v]
            e.XShmCreateImage.restype = v
            e.XShmCreateImage.argtypes = [v, v, u, i, C.c_char_p, v, u, u]
            e.XShmAttach.restype, e.XShmAttach.argtypes = i, [v, v]
            e.XShmDetach.restype, e.XShmDetach.argtypes = i, [v, v]
            e.XShmGetImage.restype, e.XShmGetImage.argtypes = i, [v, ul, v, i, i, ul]
            libc.shmget.restype, libc.shmget.argtypes = i, [i, C.c_size_t, i]
            libc.shmat.restype, libc.shmat.argtypes = v, [i, v, i]
            libc.shmdt.restype, libc.shmdt.argtypes = i, [v]
            libc.shmctl.restype, libc.shmctl.argtypes = i, [i, i, v]
            if not e.XShmQueryExtension(self._dpy):
                self.shm_note = "the X server has no MIT-SHM"
                return
            self._e, self._libc = e, libc
        except (OSError, AttributeError) as exc:
            self.shm_note = f"{type(exc).__name__}: {exc}"

    def _on_error(self, message: str) -> None:
        self._error = message

    def _drop_shm(self) -> None:
        shm, self._shm = self._shm, None
        if shm:
            image, seg, address, _cap, view = shm
            try:
                view.release()
                self._e.XShmDetach(self._dpy, C.byref(seg))
                self._x.XSync(self._dpy, 0)
                self._libc.shmdt(address)
                self._x.XFree(C.c_void_p(image))
            except Exception:
                pass

    def _shm_for(self, capacity: int):
        """The shared segment, at least `capacity` bytes, or None (and shm_note says why)."""
        if self.shm_note:
            return None
        if self._shm and self._shm[3] >= capacity:
            return self._shm
        self._drop_shm()
        x, e, libc, dpy = self._x, self._e, self._libc, self._dpy
        scr = x.XDefaultScreen(dpy)
        seg = _ShmSegment()
        image = e.XShmCreateImage(dpy, x.XDefaultVisual(dpy, scr), x.XDefaultDepth(dpy, scr), ZPIXMAP, None,
                                  C.byref(seg), 1, 1)
        if not image:
            self.shm_note = "XShmCreateImage failed"
            return None
        shmid = libc.shmget(0, capacity, 0o1600)  # IPC_PRIVATE, IPC_CREAT | owner read/write
        address = libc.shmat(shmid, None, 0) if shmid >= 0 else None
        if shmid < 0 or address in (None, 2 ** 64 - 1):
            if shmid >= 0:
                libc.shmctl(shmid, 0, None)
            x.XFree(C.c_void_p(image))
            self.shm_note = "no shared memory segment"
            return None
        seg.shmid, seg.shmaddr, seg.readOnly = shmid, address, 0
        _XImage.from_address(image).data = address
        self._error = None
        attached = e.XShmAttach(dpy, C.byref(seg))
        x.XSync(dpy, 0)
        libc.shmctl(shmid, 0, None)  # IPC_RMID: the segment goes away with the last detach, even after a crash
        if not attached or self._error:
            libc.shmdt(address)
            x.XFree(C.c_void_p(image))
            self.shm_note = self._error or "the X server cannot attach the segment (remote display?)"
            return None
        view = memoryview((C.c_ubyte * capacity).from_address(address)).cast("B")
        self._shm = (image, seg, address, capacity, view)
        return self._shm

    def _clip(self, region):
        x, dpy = self._x, self._dpy
        scr = x.XDefaultScreen(dpy)
        sw, sh = x.XDisplayWidth(dpy, scr), x.XDisplayHeight(dpy, scr)
        if region is None:
            return 0, 0, sw, sh, sw, sh
        rx, ry = max(0, region.x), max(0, region.y)
        return rx, ry, min(region.x + region.width, sw) - rx, min(region.y + region.height, sh) - ry, sw, sh

    @contextlib.contextmanager
    def frame(self, region):
        """Frame of region (None: the whole screen) clipped to the screen, or None when it cannot be read
        this way (an empty region, an unusual pixel format, no display; callers then use the PNG tools).
        Holds the display lock until the block ends: copy what you need out of `view` before leaving."""
        with self._lock:
            yield self._capture(region)

    def _capture(self, region) -> Frame | None:
        if self.broken:
            return None
        try:
            if self._dpy is None:
                self._open()
            x, dpy = self._x, self._dpy
            rx, ry, rw, rh, sw, sh = self._clip(region)
            if rw < 1 or rh < 1:
                return None
            self._error = None
            shm = self._shm_for(max(sw * sh * 4, rw * rh * 4))
            if shm:
                image, _seg, address, _cap, view = shm
                im = _XImage.from_address(image)
                im.width, im.height, im.bytes_per_line = rw, rh, rw * 4
                if self._e.XShmGetImage(dpy, self._root, image, rx, ry, ALL_PLANES) and not self._error:
                    if self._unusual(im):
                        return None
                    return Frame(view[:rw * rh * 4], address, rw, rh, rw * 4, rx, ry)
                self._error = None  # this once: XGetImage below
            p = x.XGetImage(dpy, self._root, rx, ry, rw, rh, ALL_PLANES, ZPIXMAP)
            if not p:
                x.XSync(dpy, 0)
                return None
            im = _XImage.from_address(p)
            data = im.data
            try:
                if self._unusual(im):
                    return None
                bpl, width, height = im.bytes_per_line, im.width, im.height
                buf = C.string_at(data, bpl * height)
            finally:
                # XDestroyImage is a macro in Xlib.h: free the pixels, then the struct, by hand.
                x.XFree(C.c_void_p(data))
                x.XFree(C.c_void_p(p))
            return Frame(memoryview(buf), C.cast(C.c_char_p(buf), C.c_void_p).value, width, height, bpl, rx, ry)
        except Exception as exc:  # ctypes.ArgumentError too: never break an action, use the PNG path
            self.broken = f"{type(exc).__name__}: {exc}"
            return None

    def _unusual(self, im) -> bool:
        if im.bits_per_pixel != 32 or im.depth not in (24, 32) or im.byte_order != 0 \
                or (im.red_mask, im.green_mask, im.blue_mask) != (0xFF0000, 0xFF00, 0xFF) \
                or sys.byteorder != "little":
            self.broken = f"unusual pixel format ({im.bits_per_pixel} bpp, depth {im.depth})"
            return True
        return False

    def gray(self, region, cell: int) -> tuple[int, int, bytes, tuple[int, int]] | None:
        """(cols, rows, gray, (x, y)) for region (None: the whole screen), or None when it cannot
        be read this way (then the caller uses the PNG path)."""
        if self.broken:
            return None
        with self.frame(region) as f:
            if f is None or f.width < cell or f.height < cell:
                return None
            try:
                cols, rows, gray = average_cells(f.view, f.bpl, f.width, f.height, cell)
            except Exception as exc:
                self.broken = f"{type(exc).__name__}: {exc}"
                return None
            return cols, rows, gray, (f.x, f.y)
