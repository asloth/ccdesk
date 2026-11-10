"""In-process screenshots (xshot, xpixman, MIT-SHM frames in xgrab) and how X11Backend uses them.
The Xvfb tests start their own private server; they never touch $DISPLAY."""

import os
import random
import shutil
import subprocess
import ctypes as C

import pytest

from ccdesk.backends import X11Backend, xpixman, xshot
from ccdesk.backends.xgrab import XGrab, average_cells
from ccdesk.errors import CommandError
from ccdesk.parsing import Region
from tests.pngutil import read_png

# -- PNG writing ---------------------------------------------------------------


def rgb_shot(w, h, seed=1):
    rnd = random.Random(seed)
    data = bytes(rnd.randrange(256) for _ in range(w * h * 3))
    return xshot.Shot(data, w * 3, w, h)


def test_png_roundtrip(tmp_path):
    shot = rgb_shot(37, 23)
    path = tmp_path / "a.png"
    xshot.write_png(str(path), shot)
    img = read_png(path)
    assert (img.width, img.height, img.channels) == (37, 23, 3)
    for y in range(23):
        assert img.rows[y] == shot.data[y * 111:(y + 1) * 111]


def test_png_honours_row_stride(tmp_path):
    w, h, stride = 5, 4, 20  # 5 bytes of padding per row
    rows = [bytes([y * 10 + i % 7 for i in range(w * 3)]) + b"\xee" * 5 for y in range(h)]
    path = tmp_path / "a.png"
    xshot.write_png(str(path), xshot.Shot(b"".join(rows), stride, w, h))
    assert read_png(path).rows == [r[:15] for r in rows]


def test_python_packing_swaps_to_rgb_and_drops_row_padding():
    w, h, bpl = 3, 2, 3 * 4 + 8
    rows = [b"".join(bytes((10 * x + y, 20 * x + y, 30 * x + y, 99)) for x in range(w)) + b"\x77" * 8 for y in range(h)]
    out = xshot._pack_rgb_python(memoryview(b"".join(rows)), w, h, bpl)
    want = b"".join(bytes((30 * x + y, 20 * x + y, 10 * x + y)) for y in range(h) for x in range(w))
    assert out == want


def test_average_cells_accepts_a_memoryview():
    w, h = 40, 24
    rnd = random.Random(4)
    buf = bytes(rnd.randrange(256) for _ in range(w * h * 4))
    assert average_cells(memoryview(buf), w * 4, w, h, 8) == average_cells(buf, w * 4, w, h, 8)


# -- libpixman -------------------------------------------------------------------

needs_pixman = pytest.mark.skipif(xpixman.load() is None, reason="libpixman is not installed")


def bgrx(w, h, seed=2):
    rnd = random.Random(seed)
    return bytes(b if i % 4 != 3 else 0 for i, b in enumerate(rnd.randrange(256) for _ in range(w * h * 4)))


def address(buf):
    return C.cast(C.c_char_p(buf), C.c_void_p).value


@needs_pixman
def test_pixman_converts_bgrx_to_rgb():
    w, h = 6, 5
    buf = bgrx(w, h)
    out, stride = xpixman.load().resample(address(buf), xpixman.X8R8G8B8, w, h, w * 4, w, h)
    for y in range(h):
        for x in range(w):
            b, g, r = buf[(y * w + x) * 4:(y * w + x) * 4 + 3]
            assert out[y * stride + x * 3:y * stride + x * 3 + 3] == bytes((r, g, b))


@needs_pixman
def test_pixman_half_size_is_the_mean_of_each_2x2():
    w, h = 16, 10
    buf = bgrx(w, h)
    out, stride = xpixman.load().resample(address(buf), xpixman.X8R8G8B8, w, h, w * 4, w // 2, h // 2)
    for y in range(h // 2):
        for x in range(w // 2):
            for c, plane in enumerate((2, 1, 0)):  # R, G, B out of B, G, R in
                total = sum(buf[((2 * y + dy) * w + 2 * x + dx) * 4 + plane] for dy in (0, 1) for dx in (0, 1))
                assert abs(out[y * stride + x * 3 + c] - total / 4) <= 1


@needs_pixman
def test_pixman_other_factors_average_the_whole_area():
    """A stripe of one source pixel must still show in the output (nearest-neighbour would skip it)."""
    w, h = 50, 50
    buf = bytearray(b"\x00\x00\x00\x00" * w * h)
    for y in range(h):
        buf[(y * w + 24) * 4:(y * w + 24) * 4 + 3] = b"\xff\xff\xff"  # one white column
    src = bytes(buf)
    out, stride = xpixman.load().resample(address(src), xpixman.X8R8G8B8, w, h, w * 4, 30, 30)  # 0.6
    column = [out[10 * stride + x * 3] for x in range(30)]
    assert 100 <= sum(column) <= 160 and max(column) < 255  # a share of the white, not all of it, not none


@needs_pixman
def test_pixman_refuses_a_stride_it_would_log_a_bug_for():
    with pytest.raises(OSError):
        xpixman.load().resample(address(b"\x00" * 64), xpixman.R8G8B8_BYTES, 5, 4, 15, 5, 4)


# -- X11Backend wiring (no display) -----------------------------------------------


def test_injected_runner_never_takes_the_in_process_path(tmp_path):
    calls = []
    b = X11Backend(env={"DISPLAY": ":0"}, runner=lambda argv, **kw: calls.append(argv) or "",
                   which=lambda n: f"/usr/bin/{n}")
    assert b._grab() is None
    b.screenshot(str(tmp_path / "a.png"), None)
    assert calls and calls[0][0] == "scrot"


def test_in_process_failure_falls_back_to_the_tools(tmp_path):
    calls = []
    b = X11Backend(env={"DISPLAY": ":65000"}, which=lambda n: f"/usr/bin/{n}" if n in ("scrot", "import") else None)
    b._runner = lambda argv, **kw: calls.append(argv) or ""
    b.screenshot(str(tmp_path / "a.png"), Region(1, 2, 3, 4))  # no such display: XGrab fails
    assert calls == [["scrot", "-o", "-z", "-a", "1,2,3,4", str(tmp_path / "a.png")]]
    assert b._last_shot is None


def test_scale_png_without_a_cached_shot_uses_imagemagick():
    calls = []
    b = X11Backend(env={"DISPLAY": ":0"}, runner=lambda argv, **kw: calls.append(argv) or "",
                   which=lambda n: f"/usr/bin/{n}" if n == "magick" else None)
    b.scale_png("/tmp/a.png", 10, 5)
    assert calls == [["magick", "/tmp/a.png", "-resize", "10x5!", "PNG24:/tmp/a.png"]]


# -- a real X server ----------------------------------------------------------------


def _xvfb_reason():
    return None if shutil.which("Xvfb") else "Xvfb not on PATH"


@pytest.fixture
def xvfb():
    reason = _xvfb_reason()
    if reason:
        pytest.skip(reason)
    r, w = os.pipe()
    proc = subprocess.Popen(["Xvfb", "-screen", "0", "400x300x24", "-nolisten", "tcp", "-displayfd", str(w)],
                            pass_fds=(w,), stderr=subprocess.DEVNULL)
    os.close(w)
    try:
        number = os.read(r, 16).split(b"\n")[0].decode()
        os.close(r)
        if not number:
            pytest.skip("Xvfb did not start")
        yield f":{number}"
    finally:
        proc.terminate()
        proc.wait(5)


class Painter:
    """A full-screen override-redirect window with a solid background: root-window paint tools
    (xsetroot) draw nothing readable on a bare Xvfb."""

    def __init__(self, display):
        import ctypes.util
        self.x = x = C.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        v, i, ul = C.c_void_p, C.c_int, C.c_ulong
        for name, res, args in (("XOpenDisplay", v, [C.c_char_p]), ("XDefaultRootWindow", ul, [v]),
                                ("XCreateSimpleWindow", ul, [v, ul, i, i, C.c_uint, C.c_uint, C.c_uint, ul, ul]),
                                ("XChangeWindowAttributes", i, [v, ul, ul, v]), ("XMapWindow", i, [v, ul]),
                                ("XSetWindowBackground", i, [v, ul, ul]), ("XClearWindow", i, [v, ul]),
                                ("XSync", i, [v, i]), ("XCloseDisplay", i, [v])):
            fn = getattr(x, name)
            fn.restype, fn.argtypes = res, args
        self.d = x.XOpenDisplay(display.encode())
        self.win = x.XCreateSimpleWindow(self.d, x.XDefaultRootWindow(self.d), 0, 0, 400, 300, 0, 0, 0)
        attrs = (C.c_ulong * 15)()
        attrs[11] = 1  # override_redirect (12th word of XSetWindowAttributes on 64-bit)
        x.XChangeWindowAttributes(self.d, self.win, 1 << 9, attrs)
        x.XMapWindow(self.d, self.win)

    def solid(self, rgb):
        self.x.XSetWindowBackground(self.d, self.win, rgb)
        self.x.XClearWindow(self.d, self.win)
        self.x.XSync(self.d, 0)

    def close(self):
        self.x.XCloseDisplay(self.d)


@pytest.fixture
def painter(xvfb):
    p = Painter(xvfb)
    p.solid(0x336699)
    yield p
    p.close()


def test_frames_are_the_same_with_and_without_shm(xvfb, painter):
    shm, plain = XGrab(xvfb), XGrab(xvfb, shm=False)
    for region in (None, Region(10, 20, 100, 50), Region(-5, -5, 50, 50), Region(350, 250, 200, 200)):
        with shm.frame(region) as a, plain.frame(region) as b:
            assert a and b
            assert (a.width, a.height, a.x, a.y) == (b.width, b.height, b.x, b.y)
            assert bytes(a.view) == bytes(b.view)
    assert shm.shm_note is None or "MIT-SHM" in shm.shm_note or "segment" in shm.shm_note
    assert plain.shm_note == "disabled"
    with shm.frame(None) as f:
        assert bytes(f.view[:3]) == b"\x99\x66\x33"  # B, G, R of #336699


def test_gray_grid_is_the_same_with_and_without_shm(xvfb, painter):
    assert XGrab(xvfb).gray(None, 8) == XGrab(xvfb, shm=False).gray(None, 8)
    assert XGrab(xvfb).gray(Region(0, 0, 100, 100), 8)[:2] == (12, 12)


def test_region_outside_the_screen_gives_no_frame(xvfb, painter):
    g = XGrab(xvfb)
    with g.frame(Region(500, 500, 10, 10)) as f:
        assert f is None


def test_in_process_png_matches_import_and_scales(xvfb, painter, tmp_path):
    b = X11Backend(env={**os.environ, "DISPLAY": xvfb})
    ours = str(tmp_path / "ours.png")
    b.screenshot(ours, None)
    assert b._last_shot is not None, "the in-process path was not taken"
    img = read_png(ours)
    assert (img.width, img.height) == (400, 300)
    assert img.pixel(0, 0) == img.pixel(399, 299) == (0x33, 0x66, 0x99)
    if shutil.which("import"):
        ref = str(tmp_path / "import.png")
        subprocess.run(["import", "-silent", "-window", "root", f"PNG24:{ref}"], env={**os.environ, "DISPLAY": xvfb}, check=True)
        assert read_png(ref).rows == img.rows
    b.screenshot(ours, Region(10, 10, 100, 60))
    assert (read_png(ours).width, read_png(ours).height) == (100, 60)
    if xpixman.load() is not None:
        b.scale_png(ours, 50, 30)
        small = read_png(ours)
        assert (small.width, small.height) == (50, 30)
        assert small.pixel(10, 10) == (0x33, 0x66, 0x99)
        assert b._last_shot is None  # used up: the file is not the shot any more


def test_scale_png_ignores_a_cache_for_another_file(xvfb, painter, tmp_path):
    calls = []
    b = X11Backend(env={**os.environ, "DISPLAY": xvfb}, which=lambda n: f"/usr/bin/{n}" if n == "magick" else None)
    b.screenshot(str(tmp_path / "a.png"), None)
    b._runner = lambda argv, **kw: calls.append(argv) or ""
    b.scale_png(str(tmp_path / "other.png"), 10, 10)
    assert calls and calls[0][0] == "magick"
    # and a file changed after the shot is not trusted either
    b.screenshot(str(tmp_path / "a.png"), None)
    with open(tmp_path / "a.png", "ab") as fh:
        fh.write(b"x")
    calls.clear()
    b.scale_png(str(tmp_path / "a.png"), 10, 10)
    assert calls and calls[0][0] == "magick"


def test_without_libpixman_png_is_packed_in_python_and_scaling_uses_imagemagick(xvfb, painter, tmp_path, monkeypatch):
    monkeypatch.setattr(xpixman, "load", lambda: None)
    calls = []
    b = X11Backend(env={**os.environ, "DISPLAY": xvfb}, which=lambda n: f"/usr/bin/{n}" if n == "magick" else None)
    path = str(tmp_path / "a.png")
    b.screenshot(path, None)
    assert read_png(path).pixel(7, 7) == (0x33, 0x66, 0x99)
    b._runner = lambda argv, **kw: calls.append(argv) or ""
    b.scale_png(path, 200, 150)
    assert calls and calls[0][0] == "magick"
