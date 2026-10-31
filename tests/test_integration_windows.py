"""Window control against a real bspwm on a private Xvfb, the case where a tiling window manager
ignores plain geometry requests. Safe on a real desktop: it never uses $DISPLAY.
Needs Xvfb, dbus-daemon, bspwm, xdotool, xwininfo and tkinter.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time

import pytest

from ccdesk.backends import X11Backend
from ccdesk.desktop import Desktop
from ccdesk.errors import CcdeskError
from ccdesk.headless import BSPWMRC, Headless
from ccdesk.safety import AuditLog, Policy
from tests.pngutil import read_png
from tests.test_integration_tk import APP, MAGENTA


def _skip_reason() -> str | None:
    for tool in ("Xvfb", "dbus-daemon", "bspwm", "bspc", "xdotool", "xwininfo", "xrandr"):
        if not shutil.which(tool):
            return f"{tool} not on PATH"
    if subprocess.run([sys.executable, "-c", "import tkinter"], capture_output=True).returncode:
        return "tkinter not available"
    return None


pytestmark = pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")


@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    hl = Headless(size="1280x800", wm=f"bspwm -c {BSPWMRC}")
    env = hl.start()
    state = tmp_path / "state.json"
    app = subprocess.Popen([sys.executable, str(APP), str(state)], env=env)
    desktop = Desktop(backend=X11Backend(env=env), policy=Policy(), audit=AuditLog(tmp_path / "audit.log"),
                      shots_dir=tmp_path / "shots", headless=hl)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not desktop.backend.list_windows():
        time.sleep(0.1)
    time.sleep(0.5)
    yield desktop
    app.terminate()
    app.wait(5)
    hl.stop()


def box(result):
    a = result["after"]["geometry"]
    return a["x"], a["y"], a["width"], a["height"]


def test_window_control_on_bspwm(desk):
    tiled = desk.find_window("ccdesk-test")
    assert (tiled.width, tiled.height) == (1280, 800)  # bspwm tiled it, whatever the app asked for

    out = desk.manage_window("ccdesk-test", "resize", width=500, height=400)
    assert out["changed"] and box(out)[2:] == (500, 400)
    out = desk.manage_window("ccdesk-test", "move", x=100, y=120)
    assert out["changed"] and box(out) == (100, 120, 500, 400)

    shot = desk.screenshot(window="ccdesk-test")
    assert (shot["width"], shot["height"]) == (500, 400)
    assert shot["origin"] == {"x": 100, "y": 120}
    assert read_png(shot["path"]).find_color(MAGENTA)  # the app's button, so it is really that window

    out = desk.manage_window("ccdesk-test", "maximize")
    assert out["changed"] and box(out) == (0, 0, 1280, 800)

    out = desk.manage_window("ccdesk-test", "minimize")
    assert out["changed"] and out["on_show"] is False
    with pytest.raises(CcdeskError, match="minimized or on another desktop"):
        desk.screenshot(window="ccdesk-test")

    out = desk.manage_window("ccdesk-test", "restore")
    assert out["changed"] and out["on_show"] is True and box(out) == (0, 0, 1280, 800)
    assert desk.backend.active_window() == tiled.id


def test_monitors_on_xvfb(desk):
    out = desk.list_monitors()
    assert out["screen"] == {"width": 1280, "height": 800}
    assert [(m["width"], m["height"]) for m in out["monitors"]] == [(1280, 800)]


def test_a_window_on_another_desktop_is_refused(desk):
    window = desk.find_window("ccdesk-test")
    desk.backend.run(["bspc", "monitor", "-d", "1", "2"])
    desk.backend.run(["bspc", "node", window.id, "-d", "^2"])
    time.sleep(0.3)
    with pytest.raises(CcdeskError, match="minimized or on another desktop"):
        desk.screenshot(window="ccdesk-test")
