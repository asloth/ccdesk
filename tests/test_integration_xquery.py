"""The in-process window questions (ccdesk/backends/xquery.py) against the subprocess path they
replace, on private Xvfb screens: with a window manager (bspwm) and on a bare Xvfb.
Safe on a real desktop: it never uses $DISPLAY.
Needs Xvfb, bspwm, xdotool, xprop, xwininfo, wmctrl and tkinter.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import shutil
import subprocess
import sys
import time

import pytest

from ccdesk.backends import X11Backend
from ccdesk.backends.x11_own import OwnPointerBackend
from ccdesk.cursor import TITLE_PREFIX
from ccdesk.desktop import Desktop
from ccdesk.headless import BSPWMRC, Headless
from ccdesk.parsing import parse_shell_vars
from ccdesk.safety import AuditLog, Policy
from tests.test_integration_tk import APP


def _skip_reason() -> str | None:
    for tool in ("Xvfb", "bspwm", "xdotool", "xprop", "xwininfo", "wmctrl", "xprintidle"):
        if not shutil.which(tool):
            return f"{tool} not on PATH"
    if subprocess.run([sys.executable, "-c", "import tkinter"], capture_output=True).returncode:
        return "tkinter not available"
    if not ctypes.util.find_library("X11"):
        return "libX11 not found"
    return None


pytestmark = pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")


class Subprocess(X11Backend):
    """The path the in-process one replaces."""

    def _xq(self):
        return None


class RawX:
    """A few plain top-level windows made over Xlib, with properties a toolkit would set."""

    def __init__(self, display: str):
        self.x = x = ctypes.CDLL(ctypes.util.find_library("X11"))
        v, ul, i, u = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_uint
        x.XOpenDisplay.restype, x.XOpenDisplay.argtypes = v, [ctypes.c_char_p]
        x.XDefaultRootWindow.restype, x.XDefaultRootWindow.argtypes = ul, [v]
        x.XCreateSimpleWindow.restype = ul
        x.XCreateSimpleWindow.argtypes = [v, ul, i, i, u, u, u, ul, ul]
        x.XInternAtom.restype, x.XInternAtom.argtypes = ul, [v, ctypes.c_char_p, i]
        x.XChangeProperty.argtypes = [v, ul, ul, ul, i, i, ctypes.c_char_p, i]
        x.XMapWindow.argtypes = [v, ul]
        x.XFlush.argtypes = [v]
        self.dpy = x.XOpenDisplay(display.encode())
        self.root = x.XDefaultRootWindow(self.dpy)

    def atom(self, name: str) -> int:
        return self.x.XInternAtom(self.dpy, name.encode(), 0)

    def prop(self, win: int, name: str, typ: str, fmt: int, data: bytes, n: int) -> None:
        self.x.XChangeProperty(self.dpy, win, self.atom(name), self.atom(typ), fmt, 0, data, n)

    def window(self, x: int, y: int, w: int, h: int, title: str | None, role: str | None = None,
               transient_for: int | None = None) -> int:
        win = self.x.XCreateSimpleWindow(self.dpy, self.root, x, y, w, h, 0, 0, 0x336699)
        if title is None:  # no name and no class at all
            self.x.XMapWindow(self.dpy, win)
            self.x.XFlush(self.dpy)
            return win
        self.prop(win, "_NET_WM_NAME", "UTF8_STRING", 8, title.encode(), len(title.encode()))
        self.prop(win, "WM_NAME", "STRING", 8, title.encode("latin-1", "replace"), len(title))
        self.prop(win, "WM_CLASS", "STRING", 8, b"raw\0Raw\0", 8)
        if role:
            self.prop(win, "WM_WINDOW_ROLE", "STRING", 8, role.encode(), len(role))
        if transient_for:
            self.prop(win, "WM_TRANSIENT_FOR", "WINDOW", 32, bytes(ctypes.c_ulong(transient_for)), 1)
        self.x.XMapWindow(self.dpy, win)
        self.x.XFlush(self.dpy)
        return win


@pytest.fixture(params=["bspwm", "bare"])
def screen(request, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    hl = Headless(size="1280x800", wm=f"bspwm -c {BSPWMRC}" if request.param == "bspwm" else "none")
    env = hl.start()
    apps = [subprocess.Popen([sys.executable, str(APP), str(tmp_path / f"state{n}.json")], env=env)
            for n in range(2)]
    fast, slow = X11Backend(env=env), Subprocess(env=env)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and len(fast.list_windows()) < 2:
        time.sleep(0.1)
    raw = RawX(env["DISPLAY"])
    parent = int(slow.list_windows()[0].id, 16) if slow.list_windows() else None
    raw.window(700, 500, 200, 150, "raw dialog ñ", role="dialog", transient_for=parent)
    raw.window(1000, 100, 150, 100, "raw second")
    raw.window(100, 600, 120, 80, None)
    time.sleep(0.6)
    yield request.param, env, fast, slow, raw
    if not getattr(fast, "_expect_broken", False):
        assert fast._xquery.broken is None, fast._xquery.broken  # it answered; no silent fallback
    for app in apps:
        app.terminate()
        app.wait(5)
    hl.stop()


def pointer(env) -> tuple[str, str]:
    out = parse_shell_vars(subprocess.run(["xdotool", "getmouselocation", "--shell"], env=env, text=True,
                                          capture_output=True).stdout)
    return out["X"], out["Y"]


def test_list_windows_match_the_subprocess_path(screen):
    kind, env, fast, slow, _ = screen
    got, want = fast.list_windows(), slow.list_windows()
    assert len(want) >= 3
    assert got == want
    assert fast._all_windows() == slow._all_windows()


def test_single_answers_match(screen):
    kind, env, fast, slow, _ = screen
    assert fast.active_window() == slow.active_window()
    assert fast.current_desktop() == slow.current_desktop()
    assert fast.screen_size() == slow.screen_size() == (1280, 800)
    ids = [w.id for w in slow.list_windows()] + ["0x00000001", "0x0"]
    for wid in ids:
        assert fast.transient_for(wid) == slow.transient_for(wid), wid
        assert fast.window_on_show(wid) == slow.window_on_show(wid), wid
        assert fast.window_role(wid) == slow.window_role(wid), wid
    assert any(slow.window_role(i) == "dialog" for i in ids)
    assert any(slow.transient_for(i) for i in ids)


def test_idle_agrees_within_a_moment(screen):
    kind, env, fast, slow, _ = screen
    a, b = fast.idle_ms(), slow.idle_ms()
    assert a is not None and b is not None and abs(a - b) < 1500


def test_window_at_matches_and_does_not_move_the_pointer(screen):
    kind, env, fast, slow, _ = screen
    windows = slow.list_windows()
    points = [(w.x + w.width // 2, w.y + w.height // 2) for w in windows] + [(5, 790)]
    before = pointer(env)
    answers = [fast.window_at(x, y) for x, y in points]
    assert pointer(env) == before  # the old way warped it to each point
    expected = [slow.window_at(x, y) for x, y in points]
    assert pointer(env) != before
    for point, got, want in zip(points, answers, expected):
        # A bare screen has no stacking oracle other than the server's own pointer window.
        assert got == want, point


def test_window_at_skips_the_agents_drawn_cursor(screen):
    kind, env, fast, slow, raw = screen
    win = slow.list_windows()[0]
    cx, cy = win.x + win.width // 2, win.y + win.height // 2
    below = fast.window_at(cx, cy)
    raw.window(cx - 10, cy - 10, 40, 30, TITLE_PREFIX + "agent")
    time.sleep(0.3)
    assert fast.window_at(cx, cy) == below
    assert TITLE_PREFIX not in " ".join(w.title for w in fast.list_windows())


def test_own_pointer_backend_still_works(screen):
    kind, env, fast, slow, _ = screen
    own = OwnPointerBackend(mpx=None, env=env)
    assert own.list_windows() == slow.list_windows()
    assert own.window_on_show(slow.list_windows()[0].id) is True
    assert own.current_desktop() == slow.current_desktop()


def test_a_guarded_click_asks_no_subprocess(screen, tmp_path):
    kind, env, fast, slow, _ = screen
    desk = Desktop(backend=fast, policy=Policy(), audit=AuditLog(tmp_path / "audit.log"),
                   shots_dir=tmp_path / "shots")
    calls: list[list[str]] = []
    original = fast._runner

    def counting(argv, **kw):
        calls.append(list(argv))
        return original(argv, **kw)

    fast._runner = counting
    win = fast.list_windows()[0]
    desk._guard(False, (win.x + 20, win.y + 20))
    assert calls == []


def test_a_broken_connection_falls_back(screen):
    kind, env, fast, slow, _ = screen
    fast._expect_broken = True
    fast._xquery.broken = "test"
    assert fast.list_windows() == slow.list_windows()
    assert fast.window_at(10, 10) == slow.window_at(10, 10)


def test_without_wmctrl_matches(screen):
    kind, env, fast, slow, _ = screen

    def no_wmctrl(name):
        return None if name == "wmctrl" else shutil.which(name)

    a, b = X11Backend(env=env, which=no_wmctrl), Subprocess(env=env, which=no_wmctrl)
    assert len(b.list_windows()) >= 3
    assert a.list_windows() == b.list_windows()
