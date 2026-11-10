"""One window's own pixels (XComposite): XGrab.window_frame / window_gray and the X11Backend methods
on top of them, with a window covered by another, repainted, resized, hidden and reparented.

The Xvfb tests start their own private server and never touch $DISPLAY. There is no window manager
on it: a reparenting one is emulated by hand (a frame window with a border and a title bar, the
client reparented into it), the same X requests a real one makes.
"""

from __future__ import annotations

import ctypes as C
import ctypes.util
import os
import shutil
import subprocess

import pytest

from ccdesk.backends import X11Backend, xgrab
from ccdesk.backends.base import Backend
from ccdesk.backends.xgrab import XGrab
from ccdesk.cursor import TITLE_PREFIX
from ccdesk.parsing import Region
from tests.pngutil import read_png

# -- no display ------------------------------------------------------------------------


def test_base_backend_says_not_supported(tmp_path):
    b = Backend()
    assert b.window_screenshot(str(tmp_path / "a.png"), "0x1") is False
    assert "not supported" in b.window_note
    assert b.window_gray_grid("0x1", None, 8) is None
    assert b.window_covered("0x1") is None


def test_injected_runner_reports_not_supported(tmp_path):
    b = X11Backend(env={"DISPLAY": ":0"}, runner=lambda argv, **kw: "", which=lambda n: f"/usr/bin/{n}")
    assert b.window_screenshot(str(tmp_path / "a.png"), "0x1") is False
    assert "not supported" in b.window_note
    assert b.window_gray_grid("0x1", None, 8) is None
    assert "not supported" in b.window_note
    assert b.window_covered("0x1") is None
    assert not (tmp_path / "a.png").exists()


def test_a_bad_id_and_a_missing_display_never_raise(tmp_path):
    b = X11Backend(env={"DISPLAY": ":65000"})
    assert b.window_screenshot(str(tmp_path / "a.png"), "nope") is False
    assert "not a window id" in b.window_note
    assert b.window_screenshot(str(tmp_path / "a.png"), "0x10") is False
    assert b.window_note
    assert b.window_gray_grid("0x10", None, 8) is None and b.window_note
    assert b.window_covered("0x10") is None


# -- a real X server -------------------------------------------------------------------


def _start_xvfb(*extra):
    if not shutil.which("Xvfb"):
        pytest.skip("Xvfb not on PATH")
    r, w = os.pipe()
    proc = subprocess.Popen(["Xvfb", "-screen", "0", "800x600x24", "-nolisten", "tcp", "-displayfd", str(w), *extra],
                            pass_fds=(w,), stderr=subprocess.DEVNULL)
    os.close(w)
    number = os.read(r, 16).split(b"\n")[0].decode()
    os.close(r)
    if not number:
        proc.terminate()
        pytest.skip("Xvfb did not start")
    return proc, f":{number}"


@pytest.fixture
def xvfb():
    proc, display = _start_xvfb()
    try:
        yield display
    finally:
        proc.terminate()
        proc.wait(5)


@pytest.fixture
def xvfb_without_composite():
    proc, display = _start_xvfb("-extension", "Composite")
    try:
        yield display
    finally:
        proc.terminate()
        proc.wait(5)


class Screen:
    """Plain windows made over Xlib. Their pixels are child windows with a background colour: the
    server paints those itself, so a window shows the same after it was covered and uncovered."""

    def __init__(self, display):
        self.x = x = C.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        v, i, u, ul, ci = C.c_void_p, C.c_int, C.c_uint, C.c_ulong, C.c_char_p
        for name, res, args in (
                ("XOpenDisplay", v, [ci]), ("XDefaultRootWindow", ul, [v]), ("XSync", i, [v, i]),
                ("XCreateSimpleWindow", ul, [v, ul, i, i, u, u, u, ul, ul]),
                ("XMapWindow", i, [v, ul]), ("XUnmapWindow", i, [v, ul]), ("XMapRaised", i, [v, ul]),
                ("XResizeWindow", i, [v, ul, u, u]), ("XMoveWindow", i, [v, ul, i, i]),
                ("XReparentWindow", i, [v, ul, ul, i, i]), ("XSetWindowBackground", i, [v, ul, ul]),
                ("XClearWindow", i, [v, ul]), ("XStoreName", i, [v, ul, ci]), ("XCloseDisplay", i, [v])):
            fn = getattr(x, name)
            fn.restype, fn.argtypes = res, args
        self.d = x.XOpenDisplay(display.encode())
        self.root = x.XDefaultRootWindow(self.d)

    def window(self, x, y, w, h, bg=0, border=0, parent=None, border_pixel=0, map=True):
        win = self.x.XCreateSimpleWindow(self.d, parent or self.root, x, y, w, h, border, border_pixel, bg)
        if map:
            self.x.XMapWindow(self.d, win)
        self.x.XSync(self.d, 0)
        return win

    def striped(self, x, y, w, h, colours=(0xCC3333, 0x33CC33, 0x3333CC), title=None):
        """A window with a vertical stripe per colour, and its stripes (children)."""
        top = self.window(x, y, w, h, bg=0x808080, map=False)
        if title:
            self.x.XStoreName(self.d, top, title.encode())
        n = len(colours)
        stripes = [self.window(k * w // n, 0, w // n if k < n - 1 else w - k * (w // n), h, bg=c, parent=top)
                   for k, c in enumerate(colours)]
        self.x.XMapWindow(self.d, top)
        self.x.XSync(self.d, 0)
        return top, stripes

    def paint(self, win, rgb):
        self.x.XSetWindowBackground(self.d, win, rgb)
        self.x.XClearWindow(self.d, win)
        self.x.XSync(self.d, 0)

    def sync(self):
        self.x.XSync(self.d, 0)

    def close(self):
        self.x.XCloseDisplay(self.d)


@pytest.fixture
def screen(xvfb):
    s = Screen(xvfb)
    yield s
    s.close()


@pytest.fixture
def backend(xvfb):
    b = X11Backend(env={**os.environ, "DISPLAY": xvfb})
    yield b
    if getattr(b, "_xgrab", None):
        b._xgrab.close()


def shot(backend, path, wid, region=None):
    assert backend.window_screenshot(str(path), hex(wid), region), backend.window_note
    img = read_png(path)
    return img.width, img.height, img.rows


def root_shot(backend, path, region):
    backend.screenshot(str(path), region)
    img = read_png(path)
    return img.width, img.height, img.rows


def test_uncovered_window_equals_the_screen_crop(screen, backend, tmp_path):
    a, _ = screen.striped(50, 40, 300, 200)
    base = root_shot(backend, tmp_path / "r.png", Region(50, 40, 300, 200))
    assert shot(backend, tmp_path / "w.png", a) == base
    assert backend._last_shot is not None  # the in-process writer produced it
    assert backend.window_covered(hex(a)) is False


def test_covered_window_shows_its_own_pixels(screen, backend, tmp_path):
    a, _ = screen.striped(50, 40, 300, 200)
    region = Region(50, 40, 300, 200)
    base = root_shot(backend, tmp_path / "r.png", region)
    assert shot(backend, tmp_path / "w0.png", a) == base  # redirects it while it is uncovered
    # B covers all of A
    b, _ = screen.striped(0, 0, 500, 400, colours=(0xFFFF00,))
    assert backend.window_covered(hex(a)) is True
    assert backend.window_covered(hex(b)) is False
    assert root_shot(backend, tmp_path / "rc.png", region) != base  # the screen shows B
    assert shot(backend, tmp_path / "w1.png", a) == base
    # B covers half of A
    screen.x.XMoveWindow(screen.d, b, 200, 0)
    screen.x.XResizeWindow(screen.d, b, 300, 400)
    screen.sync()
    assert backend.window_covered(hex(a)) is True
    assert shot(backend, tmp_path / "w2.png", a) == base
    # B beside A
    screen.x.XMoveWindow(screen.d, b, 400, 300)
    screen.sync()
    assert backend.window_covered(hex(a)) is False
    assert shot(backend, tmp_path / "w3.png", a) == base


def test_covered_window_that_was_never_redirected(screen, backend, tmp_path):
    a, _ = screen.striped(50, 40, 300, 200)
    base = root_shot(backend, tmp_path / "r.png", Region(50, 40, 300, 200))
    screen.striped(0, 0, 500, 400, colours=(0xFFFF00,))
    assert shot(backend, tmp_path / "w.png", a) == base


def test_a_repaint_while_covered_shows_in_the_next_capture(screen, backend, tmp_path):
    a, stripes = screen.striped(50, 40, 300, 200)
    shot(backend, tmp_path / "w0.png", a)
    screen.striped(0, 0, 500, 400, colours=(0xFFFF00,))
    screen.paint(stripes[1], 0x112233)
    w, h, rows = shot(backend, tmp_path / "w1.png", a)
    assert tuple(rows[100][150 * 3:150 * 3 + 3]) == (0x11, 0x22, 0x33)


def test_a_resize_while_covered_gives_the_new_size_and_content(screen, backend, tmp_path):
    a, stripes = screen.striped(50, 40, 300, 200)
    shot(backend, tmp_path / "w0.png", a)
    screen.striped(0, 0, 500, 400, colours=(0xFFFF00,))
    for w, h in ((200, 150), (450, 380), (320, 210)):
        screen.x.XResizeWindow(screen.d, a, w, h)
        screen.x.XResizeWindow(screen.d, stripes[0], 90, h)
        screen.paint(stripes[0], 0x112233)
        sw, sh, rows = shot(backend, tmp_path / "w.png", a)
        assert (sw, sh) == (w, h)
        assert tuple(rows[h - 1][5 * 3:5 * 3 + 3]) == (0x11, 0x22, 0x33)  # the new height has current content


def test_a_hidden_window_gives_a_reason_and_works_again_when_shown(screen, backend, tmp_path):
    a, _ = screen.striped(50, 40, 300, 200)
    base = shot(backend, tmp_path / "w0.png", a)
    screen.x.XUnmapWindow(screen.d, a)
    screen.sync()
    assert backend.window_screenshot(str(tmp_path / "h.png"), hex(a)) is False
    assert "not shown" in backend.window_note
    assert backend.window_gray_grid(hex(a), None, 8) is None
    assert backend.window_note
    assert backend.window_covered(hex(a)) is None
    assert not (tmp_path / "h.png").exists()
    screen.x.XMapWindow(screen.d, a)
    screen.sync()
    assert shot(backend, tmp_path / "w1.png", a) == base
    assert backend.window_note is None


def test_a_destroyed_window_gives_a_reason(screen, backend, tmp_path):
    assert backend.window_screenshot(str(tmp_path / "x.png"), "0x00fffff0") is False
    assert "gone" in backend.window_note


def test_the_root_is_not_a_window(screen, backend, tmp_path):
    assert backend.window_screenshot(str(tmp_path / "x.png"), hex(screen.root)) is False
    assert backend.window_note


def test_region_in_screen_coordinates_is_clipped_to_the_window(screen, backend, tmp_path):
    a, _ = screen.striped(50, 40, 300, 200)
    base = root_shot(backend, tmp_path / "r.png", Region(50, 40, 300, 200))
    screen.striped(0, 0, 500, 400, colours=(0xFFFF00,))
    w, h, rows = shot(backend, tmp_path / "s.png", a, Region(50 + 120, 40 + 30, 100, 60))
    assert (w, h) == (100, 60)
    assert rows == [r[120 * 3:220 * 3] for r in base[2][30:90]]
    # larger than the window, partly outside the screen: only the window's part
    assert shot(backend, tmp_path / "b.png", a, Region(-100, -100, 1500, 1500)) == base
    # a region that misses the window
    assert backend.window_screenshot(str(tmp_path / "m.png"), hex(a), Region(500, 500, 50, 50)) is False
    assert "does not overlap" in backend.window_note


def test_gray_grid_of_a_covered_window_equals_the_uncovered_one(screen, backend, tmp_path):
    a, _ = screen.striped(50, 40, 300, 200)
    want = backend.gray_grid(Region(50, 40, 300, 200), 8)
    assert want is not None
    assert backend.window_gray_grid(hex(a), None, 8) == want
    screen.striped(0, 0, 500, 400, colours=(0xFFFF00,))
    assert backend.gray_grid(Region(50, 40, 300, 200), 8)[2] != want[2]
    assert backend.window_gray_grid(hex(a), None, 8) == want
    sub = backend.window_gray_grid(hex(a), Region(60, 50, 80, 80), 8)
    assert sub[:2] == (10, 10) and sub[3] == (60, 50)
    assert backend.window_gray_grid(hex(a), Region(60, 50, 4, 4), 8) is None
    assert "smaller" in backend.window_note


def test_a_reparented_client_gives_exactly_its_own_box(screen, backend, tmp_path):
    """A window manager's frame has a border and a title bar: the picture is the client only."""
    frame = screen.window(100, 60, 308, 234, bg=0xFF0000, border=3, border_pixel=0x00FF00, map=False)
    top = 24
    client = screen.window(0, 0, 300, 200, bg=0x808080, map=False)
    screen.x.XStoreName(screen.d, client, b"client")
    stripes = [screen.window(k * 100, 0, 100, 200, bg=c, parent=client)
               for k, c in enumerate((0xCC3333, 0x33CC33, 0x3333CC))]
    assert stripes
    screen.x.XReparentWindow(screen.d, client, frame, 4, top)
    screen.x.XMapWindow(screen.d, client)
    screen.x.XMapWindow(screen.d, frame)
    screen.sync()
    listed = [w for w in backend.list_windows() if w.title == "client"]
    assert listed, "the client is not listed"
    box = listed[0]
    assert (box.x, box.y, box.width, box.height) == (100 + 3 + 4, 60 + 3 + top, 300, 200)
    base = root_shot(backend, tmp_path / "r.png", Region(box.x, box.y, box.width, box.height))
    for step in range(2):  # uncovered, then covered
        if step:
            screen.striped(0, 0, 700, 500, colours=(0xFFFF00,))
            assert backend.window_covered(box.id) is True
        w, h, rows = shot(backend, tmp_path / f"c{step}.png", int(box.id, 16))
        assert (w, h) == (300, 200)
        assert rows == base[2]
        # no frame colour (red) and no border colour (green) in the picture's first row
        assert tuple(rows[0][:3]) == (0xCC, 0x33, 0x33)
    sub = Region(box.x + 90, box.y + 10, 30, 5)  # across two stripes
    w, h, rows = shot(backend, tmp_path / "sub.png", int(box.id, 16), sub)
    assert (w, h) == (30, 5)
    assert tuple(rows[0][:3]) == (0xCC, 0x33, 0x33) and tuple(rows[0][-3:]) == (0x33, 0xCC, 0x33)
    gray = backend.window_gray_grid(box.id, None, 8)
    assert gray[3] == (box.x, box.y) and gray[:2] == (37, 25)


def test_the_frame_decorations_are_never_in_a_capture_of_the_frame_itself_either(screen, backend, tmp_path):
    frame = screen.window(100, 60, 308, 234, bg=0xFF0000, border=3, border_pixel=0x00FF00)
    w, h, rows = shot(backend, tmp_path / "f.png", frame)
    assert (w, h) == (308, 234)  # the inside of the window: its border is not part of its box
    assert tuple(rows[0][:3]) == (0xFF, 0x00, 0x00)


def test_a_cursor_window_does_not_cover(screen, backend, tmp_path):
    a, _ = screen.striped(50, 40, 300, 200)
    screen.striped(60, 50, 20, 20, title=TITLE_PREFIX + "desk-1")
    assert backend.window_covered(hex(a)) is False
    screen.striped(60, 50, 20, 20, title="something else")
    assert backend.window_covered(hex(a)) is True


def test_scaled_screenshot_of_a_window_capture(screen, backend, tmp_path):
    from ccdesk.backends import xpixman
    if xpixman.load() is None:
        pytest.skip("no libpixman")
    a, _ = screen.striped(50, 40, 300, 200, colours=(0x336699,))
    screen.striped(0, 0, 500, 400, colours=(0xFFFF00,))
    path = str(tmp_path / "a.png")
    assert backend.window_screenshot(path, hex(a))
    assert backend._last_shot is not None
    backend.scale_png(path, 150, 100)
    img = read_png(path)
    assert (img.width, img.height) == (150, 100)
    assert tuple(img.pixel(10, 10)) == (0x33, 0x66, 0x99)
    assert backend._last_shot is None  # used up


def test_without_the_composite_extension_it_says_so(xvfb_without_composite, tmp_path):
    s = Screen(xvfb_without_composite)
    try:
        a, _ = s.striped(50, 40, 300, 200)
        b = X11Backend(env={**os.environ, "DISPLAY": xvfb_without_composite})
        assert b.window_screenshot(str(tmp_path / "a.png"), hex(a)) is False
        assert "Composite" in b.window_note
        assert b.window_gray_grid(hex(a), None, 8) is None
        assert "Composite" in b.window_note
        # the screen path is not affected
        assert b.gray_grid(Region(50, 40, 100, 100), 8) is not None
        b.screenshot(str(tmp_path / "s.png"), Region(50, 40, 100, 100))
        assert (read_png(tmp_path / "s.png").width) == 100
    finally:
        s.close()


def test_without_libxcomposite_it_says_so(xvfb, screen, monkeypatch, tmp_path):
    a, _ = screen.striped(50, 40, 300, 200)
    real = ctypes.util.find_library
    monkeypatch.setattr(xgrab.ctypes.util, "find_library",
                        lambda n: "libnot-there-xcomposite.so.9" if n == "Xcomposite" else real(n))
    b = X11Backend(env={**os.environ, "DISPLAY": xvfb})
    assert b.window_screenshot(str(tmp_path / "a.png"), hex(a)) is False
    assert "libXcomposite" in b.window_note
    assert b.window_gray_grid(hex(a), None, 8) is None


def test_close_gives_the_redirects_back(screen, backend, tmp_path):
    a, _ = screen.striped(50, 40, 300, 200)
    shot(backend, tmp_path / "a.png", a)
    grab = backend._xgrab
    assert grab._redirected
    grab.close()
    assert not grab._redirected
    # and it can be used again: it redirects again
    assert grab.window_gray(a, None, 8) is not None
    assert grab._redirected


def test_a_hundred_captures_leave_no_redirect_or_segment_behind(screen, backend, tmp_path):
    a, _ = screen.striped(50, 40, 300, 200)
    grab = XGrab(backend.env["DISPLAY"])
    for _ in range(100):
        with grab.window_frame(a) as f:
            assert f is not None
    assert len(grab._redirected) == 1
    grab.close()
