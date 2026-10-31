"""The whole of ccdesk against a real Hyprland. It opens windows and clicks in them, so it only runs
when asked: from inside Hyprland, or from elsewhere with its session variables set, with
CCDESK_TEST_HYPRLAND=1. Needs grim, wtype, wl-clipboard and a system Python with PyGObject + GTK 3.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from ccdesk.backends import WaylandBackend
from ccdesk.desktop import Desktop
from ccdesk.errors import CcdeskError, PolicyError
from ccdesk.parsing import png_size
from ccdesk.safety import AuditLog, Policy
from tests.pngutil import read_png

APP = Path(__file__).with_name("gtk_app.py")
PY = os.environ.get("CCDESK_A11Y_PYTHON", "/usr/bin/python3")


def _skip_reason() -> str | None:
    if os.environ.get("CCDESK_TEST_HYPRLAND") != "1":
        return "set CCDESK_TEST_HYPRLAND=1 to drive a real Hyprland"
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "HYPRLAND_INSTANCE_SIGNATURE is not set"
    missing = [t for t in ("hyprctl", "grim", "wtype", "wl-copy") if not shutil.which(t)]
    return f"missing {', '.join(missing)}" if missing else None


pytestmark = pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")


def wait(pred, what, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(0.05)
    pytest.fail(f"timed out waiting for {what}")


class App:
    def __init__(self, tmp_path, name):
        self.state_path = tmp_path / f"{name}.json"
        env = dict(os.environ, GDK_BACKEND="wayland", GNOME_ACCESSIBILITY="1")
        self.proc = subprocess.Popen([PY, str(APP), str(self.state_path)], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait(lambda: self.state().get("ready"), "the app")

    def state(self):
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def wait(self, pred):
        return wait(lambda: (lambda s: s if s and pred(s) else None)(self.state()), f"app state; last {self.state()}")

    def address(self):
        def find():
            clients = json.loads(subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True).stdout)
            return next((c["address"] for c in clients if c["pid"] == self.proc.pid), None)

        return wait(find, "its window")

    def close(self):
        self.proc.terminate()
        self.proc.wait(5)


@pytest.fixture
def app(tmp_path):
    a = App(tmp_path, "a")
    yield a
    a.close()


@pytest.fixture
def desk(tmp_path):
    return Desktop(backend=WaylandBackend(), policy=Policy(), audit=AuditLog(tmp_path / "audit.log"),
                   shots_dir=tmp_path / "shots")


def box(tree, pattern):
    return tuple(map(int, re.search(rf'{pattern}.*?@(-?\d+),(-?\d+) (\d+)x(\d+)', tree).groups()))


def ref(tree, pattern):
    return re.search(rf'^\s*(e\d+) {pattern}', tree, re.M).group(1)


def geometry(desk, wid):
    return next(w["geometry"] for w in desk.list_windows()["windows"] if w["id"] == wid)


def test_screenshot_coordinates_are_logical_pixels(app, desk):
    monitors = desk.list_monitors()["monitors"]
    size = (max(m["x"] + m["width"] for m in monitors), max(m["y"] + m["height"] for m in monitors))
    assert desk.screen_size() == size
    shot = desk.screenshot()
    assert (shot["width"], shot["height"]) == size
    assert png_size(desk.screenshot(region="10,20,300,200")["path"]) == (300, 200)


def test_a_window_is_where_its_coordinates_say_in_the_screenshot(app, desk):
    """Full screenshot pixels are the coordinates clicks and windows use, on any monitor scale."""
    a = app.address()
    desk.manage_window(a, "resize", width=400, height=300)
    desk.manage_window(a, "move", x=150, y=100)
    time.sleep(0.5)
    full = read_png(desk.screenshot()["path"])
    crop = read_png(desk.screenshot(window=a)["path"])
    assert (crop.width, crop.height) == (400, 300)
    same = sum(full.pixel(150 + x, 100 + y) == crop.pixel(x, y) for x in range(0, 400, 7) for y in range(0, 300, 7))
    assert same / ((400 // 7 + 1) * (300 // 7 + 1)) > 0.98


def test_focus_and_the_active_window(app, tmp_path, desk):
    other = App(tmp_path, "b")
    try:
        a, b = app.address(), other.address()
        desk.activate_window(a)
        assert desk.backend.active_window() == a
        assert next(w for w in desk.list_windows()["windows"] if w["id"] == a)["active"]
        desk.activate_window(b)
        assert desk.backend.active_window() == b
        assert desk.list_windows()["current_desktop"] is not None
    finally:
        other.close()


def test_input_guard_refuses_another_window(app, tmp_path, desk):
    other = App(tmp_path, "b")
    try:
        a, b = app.address(), other.address()
        desk.activate_window(a)
        g = geometry(desk, b)
        with pytest.raises(PolicyError, match="not 'ccdesk-gtk'"):
            desk.click(g["x"] + g["width"] // 2, g["y"] + g["height"] // 2)
        assert other.state()["clicks"] == 0
        assert desk.type_text("ok")["window"]["id"] == a
    finally:
        other.close()


def test_clicks_land_with_borders_and_gaps(app, desk):
    """Default-looking decoration, so the window's position has to be taken into account."""
    subprocess.run(["hyprctl", "eval", "hl.config({ general = { border_size = 3, gaps_out = 12, gaps_in = 6 } })"],
                   check=True, capture_output=True)
    try:
        time.sleep(0.5)
        desk.activate_window(app.address())
        r = desk.ui_tree(window="ccdesk-gtk")
        assert "origin" in r, r
        tree = r["tree"]
        desk.click_element(ref(tree, r'button "Apply settings"'))
        app.wait(lambda s: s["clicks"] == 1)  # the first click counts: none is lost
        desk.click_element(ref(tree, r'check box "I agree"'))
        app.wait(lambda s: s["agree"])
        x, y, w, h = box(tree, r'button "Apply settings"')
        desk.click(x + w // 2, y + h // 2, count=2)
        app.wait(lambda s: s["clicks"] == 3)
        time.sleep(0.5)
        assert app.state()["clicks"] == 3  # exactly
    finally:
        subprocess.run(["hyprctl", "eval", "hl.config({ general = { border_size = 0, gaps_out = 0, gaps_in = 0 } })"],
                       capture_output=True)


def test_typing_and_keys(app, desk):
    desk.activate_window(app.address())
    tree = desk.ui_tree(window="ccdesk-gtk")["tree"]
    desk.click_element(ref(tree, r'text "Display name"'))
    desk.type_text("abcdef")
    app.wait(lambda s: s["entry"] == "abcdef")
    desk.press_key("BackSpace")
    app.wait(lambda s: s["entry"] == "abcde")
    desk.press_key("Left")
    desk.type_text("X")
    app.wait(lambda s: s["entry"] == "abcdXe")
    desk.type_text("new", replace=True)
    app.wait(lambda s: s["entry"] == "new")
    desk.press_key("ctrl+a")
    desk.type_text("sent", submit=True)
    app.wait(lambda s: s.get("submitted") == "sent")


def test_scroll_drag_and_hover_position(app, desk):
    a = app.address()
    desk.activate_window(a)
    tree = desk.ui_tree(window="ccdesk-gtk")["tree"]
    x, y, w, h = box(tree, r'"Level"')
    desk.scroll(x + w // 2, y + h // 2, dy=-3)
    level = app.wait(lambda s: s["level"] != 50)["level"]
    desk.drag(x + w // 2, y + h // 2, x + w - 2, y + h // 2, duration_ms=300)
    app.wait(lambda s: s["level"] > level)
    desk.move(321, 234)
    pos = json.loads(subprocess.run(["hyprctl", "-j", "cursorpos"], capture_output=True, text=True).stdout)
    assert (pos["x"], pos["y"]) == (321, 234)


def test_window_control(app, tmp_path, desk):
    other = App(tmp_path, "b")  # so the first window is tiled beside it
    try:
        a = app.address()
        out = desk.manage_window(a, "resize", width=700, height=500)
        assert out["changed"] and out["after"]["geometry"]["width"] == 700
        out = desk.manage_window(a, "move", x=100, y=120)
        assert out["changed"] and out["after"]["geometry"]["x"] == 100 and out["after"]["geometry"]["y"] == 120

        shot = desk.screenshot(window=a)
        assert (shot["width"], shot["height"]) == (700, 500)
        assert shot["origin"] == {"x": 100, "y": 120}

        out = desk.manage_window(a, "maximize")
        assert out["changed"] and out["after"]["geometry"]["width"] > 700
        out = desk.manage_window(a, "minimize")
        assert out["changed"] and out["on_show"] is False
        with pytest.raises(CcdeskError, match="minimized or on another desktop"):
            desk.screenshot(window=a)
        out = desk.manage_window(a, "restore")
        assert out["changed"] and out["on_show"] is True
        assert desk.backend.active_window() == a
        client = json.loads(subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True).stdout)
        mine = next(c for c in client if c["address"] == a)
        assert mine["fullscreen"] == 0 and mine["floating"] is False  # back in the layout, as ccdesk found it
    finally:
        other.close()


def test_clipboard_and_doctor(app, desk):
    desk.clipboard(action="set", text="from ccdesk")
    assert desk.clipboard()["text"] == "from ccdesk"
    report = desk.doctor()
    assert report["ready"] and report["monitors"]
