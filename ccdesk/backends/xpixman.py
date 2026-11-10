"""Colour conversion and area-averaged downscaling of a captured frame, with libpixman through ctypes.

libpixman is the compositing library under cairo and the X server, so it is on nearly every
desktop. Nothing depends on it: `load()` returns None when it is missing and the callers keep the
slower paths (Python slice conversion, ImageMagick for scaling).

One call turns an x8r8g8b8 frame (what Xlib hands out: bytes B, G, R, pad) into packed RGB and, when
asked, shrinks it:
  - exactly half size: the bilinear filter, which at that ratio samples the middle of four source
    pixels, so it is the 2x2 mean (1 ms for 1920x1080);
  - any other factor: a separable convolution with a box kernel, the mean of the source area under
    each output pixel with fractional weights at the edges (about 15-25 ms for 1920x1080).
Both are area averages: thin strokes of small text keep their weight instead of being skipped, which
nearest-neighbour sampling would do.
"""

from __future__ import annotations

import ctypes as C
import ctypes.util
import threading

_BOX, _IMPULSE = 1, 0
_FILTER_BILINEAR, _FILTER_SEPARABLE = 4, 6
_REPEAT_PAD = 3
_OP_SRC = 1
_SUBSAMPLE_BITS = 6  # sub-pixel phases of the box kernel: 64 (3 bits: a visible shift on noise at odd sizes)


def _format(bpp: int, kind: int, a: int, r: int, g: int, b: int) -> int:
    return (bpp << 24) | (kind << 16) | (a << 12) | (r << 8) | (g << 4) | b


X8R8G8B8 = _format(32, 2, 0, 8, 8, 8)  # a little-endian word 0x00RRGGBB: bytes B, G, R, pad
R8G8B8_BYTES = _format(24, 3, 0, 8, 8, 8)  # PIXMAN_b8g8r8: on little-endian the bytes are R, G, B


class Pixman:
    def __init__(self):
        name = ctypes.util.find_library("pixman-1") or "libpixman-1.so.0"
        p = C.CDLL(name)
        v, i = C.c_void_p, C.c_int
        p.pixman_image_create_bits.restype = v
        p.pixman_image_create_bits.argtypes = [i, i, i, v, i]
        p.pixman_image_set_transform.argtypes = [v, v]
        p.pixman_image_set_transform.restype = i
        p.pixman_image_set_filter.argtypes = [v, i, v, i]
        p.pixman_image_set_filter.restype = i
        p.pixman_image_set_repeat.argtypes = [v, i]
        p.pixman_image_unref.argtypes = [v]
        p.pixman_image_unref.restype = i
        p.pixman_transform_init_scale.argtypes = [v, i, i]
        p.pixman_transform_init_scale.restype = i
        p.pixman_filter_create_separable_convolution.restype = v
        p.pixman_filter_create_separable_convolution.argtypes = [C.POINTER(i), i, i, i, i, i, i, i, i]
        p.pixman_image_composite32.argtypes = [i, v, v, v, i, i, i, i, i, i, i, i]
        free = C.CDLL(None).free
        free.argtypes = [v]
        self._p, self._free = p, free
        self._lock = threading.Lock()

    def resample(self, src: int, fmt: int, width: int, height: int, stride: int,
                 out_width: int, out_height: int) -> tuple[bytes, int]:
        """Packed RGB (rows of out_width*3 bytes, `stride` apart) of the frame at address src, shrunk
        to out_width x out_height; returns (bytes, stride). Raises OSError when pixman refuses."""
        p = self._p
        if stride % 4 or width < 1 or height < 1 or out_width < 1 or out_height < 1:
            raise OSError("pixman needs rows of a multiple of 4 bytes")  # it logs "BUG" to stderr otherwise
        out_stride = (out_width * 3 + 3) & ~3
        dst = C.create_string_buffer(out_stride * out_height)
        images = []
        params = None
        try:
            s = p.pixman_image_create_bits(fmt, width, height, src, stride)
            if not s:
                raise OSError("pixman: cannot wrap the frame")
            images.append(s)
            d = p.pixman_image_create_bits(R8G8B8_BYTES, out_width, out_height, C.cast(dst, C.c_void_p), out_stride)
            if not d:
                raise OSError("pixman: cannot create the output image")
            images.append(d)
            if (out_width, out_height) != (width, height):
                fx = max(1, round(width / out_width * 65536))
                fy = max(1, round(height / out_height * 65536))
                t = (C.c_int32 * 9)()
                p.pixman_transform_init_scale(t, fx, fy)
                ok = p.pixman_image_set_transform(s, t)
                p.pixman_image_set_repeat(s, _REPEAT_PAD)  # the last row of boxes reads past the edge
                if ok and (width, height) == (out_width * 2, out_height * 2):
                    ok = p.pixman_image_set_filter(s, _FILTER_BILINEAR, None, 0)
                elif ok:
                    n = C.c_int()
                    params = p.pixman_filter_create_separable_convolution(
                        C.byref(n), fx, fy, _IMPULSE, _IMPULSE, _BOX, _BOX, _SUBSAMPLE_BITS, _SUBSAMPLE_BITS)
                    ok = bool(params) and p.pixman_image_set_filter(s, _FILTER_SEPARABLE, params, n.value)
                if not ok:
                    raise OSError("pixman: cannot set the scaling filter")
            p.pixman_image_composite32(_OP_SRC, s, None, d, 0, 0, 0, 0, 0, 0, out_width, out_height)
        finally:
            for img in images:
                p.pixman_image_unref(img)
            if params:
                self._free(params)
        return dst.raw, out_stride


_lock = threading.Lock()
_loaded = False
_instance: Pixman | None = None


def load() -> Pixman | None:
    """The shared Pixman, or None when libpixman cannot be used here."""
    global _loaded, _instance
    with _lock:
        if not _loaded:
            _loaded = True
            try:
                _instance = Pixman()
            except (OSError, AttributeError):
                _instance = None
        return _instance
