"""End-to-end test on a real X server: run with `xvfb-run -a pytest -q`.

Spawns a Tk window, finds widgets by colour in a ccdesk screenshot, then clicks, types,
presses keys, drags and scrolls through the real X11 backend and checks the app's state.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ccdesk.backends import X11Backend
from ccdesk.desktop import Desktop
from ccdesk.errors import PolicyError
from ccdesk.safety import AuditLog, Policy
from tests.pngutil import read_png

APP = Path(__file__).with_name("tk_app.py")
MAGENTA, RED, GREEN, YELLOW, BLUE = (255, 0, 255), (255, 0, 0), (0, 255, 0), (255, 255, 0), (0, 0, 255)


def _skip_reason() -> str | None:
    if not os.environ.get("DISPLAY"):
        return "no DISPLAY (run under xvfb-run)"
    for tool in ("xdotool",):
        if not shutil.which(tool):
            return f"{tool} not installed"
    if not (shutil.which("scrot") or shutil.which("import")):
        return "neither scrot nor import installed"
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return "tkinter not available"
    if subprocess.run(["xdotool", "getdisplaygeometry"], capture_output=True).returncode != 0:
        return "cannot connect to the X display"
    return None


pytestmark = [pytest.mark.display, pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")]


def center(box):
    x0, y0, x1, y1 = box
    return (x0 + x1) // 2, (y0 + y1) // 2


class App:
    def __init__(self, tmp_path: Path):
        self.state_path = tmp_path / "state.json"
        self.proc = subprocess.Popen([sys.executable, str(APP), str(self.state_path)])

    def state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def wait_for(self, predicate, timeout: float = 8.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = self.state()
            if st and predicate(st):
                return st
            if self.proc.poll() is not None:
                pytest.fail(f"Tk app exited with {self.proc.returncode}")
            time.sleep(0.05)
        pytest.fail(f"timed out waiting; last state: {self.state()}")

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture
def app(tmp_path):
    a = App(tmp_path)
    a.wait_for(lambda s: s.get("ready"))
    time.sleep(0.3)  # let the first frame paint
    yield a
    a.close()


@pytest.fixture
def desk(tmp_path):
    env = dict(os.environ)
    env.pop("WAYLAND_DISPLAY", None)
    return Desktop(backend=X11Backend(env=env), policy=Policy(), audit=AuditLog(tmp_path / "audit.log"),
                   shots_dir=tmp_path / "shots")


def shot(desk, **kw):
    result = desk.screenshot(**kw)
    return result, read_png(result["path"])


def test_click_type_and_verify(app, desk, tmp_path):
    w, h = desk.screen_size()
    result, img = shot(desk)
    assert (result["width"], result["height"]) == (w, h) == (img.width, img.height)

    # Find the button purely from pixels, then cross-check with Tk's own geometry.
    button = img.find_color(MAGENTA)
    assert button, "button not visible in screenshot"
    bx, by = center(button)
    tx, ty, tw, th = app.state()["button"]
    assert tx <= bx <= tx + tw and ty <= by <= ty + th
    assert img.find_color(RED), "label should start red"

    windows = desk.list_windows()["windows"]
    mine = [win for win in windows if win["title"] == "ccdesk-test"]
    assert mine, windows
    if shutil.which("xprop") or shutil.which("wmctrl"):
        assert "Tk" in mine[0]["class"]
    desk.activate_window("ccdesk-test")

    desk.click(bx, by)
    app.wait_for(lambda s: s["label"] == "clicked")
    _, img = shot(desk)
    assert img.find_color(GREEN), "label should turn green after the click"
    assert not img.find_color(RED)

    entry = img.find_color(YELLOW)
    assert entry, "entry not visible"
    desk.click(*center(entry))
    desk.type_text("hello ccdesk")
    app.wait_for(lambda s: s["entry"] == "hello ccdesk")

    desk.press_key("Return")
    app.wait_for(lambda s: s["label"] == "submitted:hello ccdesk")

    desk.press_key("BackSpace")
    app.wait_for(lambda s: s["entry"] == "hello ccdes")

    log = [json.loads(line) for line in (tmp_path / "audit.log").read_text().splitlines()]
    tools = [e["tool"] for e in log]
    assert {"screenshot", "click", "type_text", "press_key", "list_windows", "activate_window"} <= set(tools)
    assert all(e["status"] == "ok" for e in log)


def test_region_screenshot_and_offsets(app, desk):
    _, full = shot(desk)
    x0, y0, x1, y1 = full.find_color(MAGENTA)
    region = f"{x0 - 10},{y0 - 10},{x1 - x0 + 21},{y1 - y0 + 21}"
    result, img = shot(desk, region=region)
    assert (img.width, img.height) == (x1 - x0 + 21, y1 - y0 + 21)
    assert result["origin"] == {"x": x0 - 10, "y": y0 - 10}
    # Inside the region the button sits 10px from the corner.
    rx0, ry0, _, _ = img.find_color(MAGENTA)
    assert (rx0, ry0) == (10, 10)


def test_drag_and_scroll(app, desk):
    _, img = shot(desk)
    x0, y0, x1, y1 = img.find_color(BLUE)
    cy = (y0 + y1) // 2
    desk.drag(x0 + 20, cy, x1 - 20, cy, duration_ms=300)
    st = app.wait_for(lambda s: s["drag"]["release"] is not None)
    assert st["drag"]["press"] == [x0 + 20, cy]
    assert st["drag"]["release"] == [x1 - 20, cy]
    assert st["drag"]["motion"] > 0

    lx, ly, lw, lh = app.state()["listbox"]
    desk.scroll(lx + lw // 2, ly + lh // 2, dy=5)
    app.wait_for(lambda s: s["list_top"] > 0)
    top = app.state()["list_top"]
    desk.scroll(lx + lw // 2, ly + lh // 2, dy=-5)
    app.wait_for(lambda s: s["list_top"] < top)


def test_clipboard_roundtrip(desk):
    if not (shutil.which("xclip") or shutil.which("xsel")):
        pytest.skip("no xclip/xsel")
    desk.clipboard("set", "ccdesk clipboard ✓")
    assert desk.clipboard("get")["text"] == "ccdesk clipboard ✓"


def test_destructive_key_refused_on_real_display(app, desk):
    with pytest.raises(PolicyError):
        desk.press_key("q", modifiers=["ctrl"])
    with pytest.raises(PolicyError):
        desk.press_key("alt+F4")
    assert app.proc.poll() is None, "app must still be running"


def test_read_only_blocks_input_but_allows_screenshots(app, tmp_path):
    env = dict(os.environ)
    env.pop("WAYLAND_DISPLAY", None)
    ro = Desktop(backend=X11Backend(env=env), policy=Policy(read_only=True),
                 audit=AuditLog(tmp_path / "ro.log"), shots_dir=tmp_path / "ro")
    assert Path(ro.screenshot()["path"]).exists()
    with pytest.raises(PolicyError):
        ro.click(10, 10)
    with pytest.raises(PolicyError):
        ro.type_text("nope")
    assert app.state()["label"] == "waiting"


@pytest.mark.skipif(not shutil.which("xprintidle"), reason="xprintidle not installed")
def test_yield_to_the_user(app, tmp_path):
    """ccdesk's own events reset X's idle counter; input from anyone else makes it wait."""
    env = dict(os.environ)
    env.pop("WAYLAND_DISPLAY", None)
    desk = Desktop(backend=X11Backend(env=env), policy=Policy(yield_ms=800), audit=AuditLog(tmp_path / "y.log"),
                   shots_dir=tmp_path / "y", screen_diff=False)
    time.sleep(0.9)
    assert "yielded_ms" not in desk.move(10, 10)
    assert "yielded_ms" not in desk.move(20, 20)  # right after our own input: not the user
    time.sleep(0.4)  # input within OWN_INPUT_SLACK_MS of ours would count as ours
    subprocess.run(["xdotool", "mousemove", "30", "30"], check=True, env=env)  # "the user"
    r = desk.move(40, 40)
    assert 300 <= r.get("yielded_ms", 0) <= 1500, r


@pytest.mark.skipif(not (shutil.which("magick") or shutil.which("convert")), reason="ImageMagick not installed")
def test_screen_changed_without_a_tree(app, desk):
    """Tk publishes no accessibility tree: actions compare pixels instead."""
    _, img = shot(desk)
    bx, by = center(img.find_color(MAGENTA))
    w, h = desk.screen_size()
    nothing = desk.click(w - 5, h - 5)  # the empty root window
    assert nothing["screen_changed"] is False
    start = time.monotonic()
    r = desk.click(bx, by)
    overhead = time.monotonic() - start
    assert r["screen_changed"] is True  # the label turned from red to green
    lx, ly, lw, lh = r["changed_area"].values()
    _, after = shot(desk, region=f"{lx},{ly},{lw},{lh}")
    assert after.find_color(GREEN), "changed_area should contain the label"
    assert overhead < 2.0, f"click with screen comparison took {overhead:.2f}s"
    entry = img.find_color(YELLOW)
    desk.click(*center(entry))
    # A blinking caret alone is not a change, so an idle wait times out.
    assert desk.wait_for_change(region=f"{entry[0]},{entry[1]},{entry[2] - entry[0]},{entry[3] - entry[1]}",
                                timeout=1.5)["changed"] is False


@pytest.mark.skipif(not (shutil.which("magick") or shutil.which("convert")), reason="ImageMagick not installed")
def test_scaled_screenshot_click(app, desk):
    w, h = desk.screen_size()
    result, img = shot(desk, scale=0.5)
    assert (img.width, img.height) == (round(w * 0.5), round(h * 0.5)) and result["scale"] == 0.5
    button = img.find_color(MAGENTA)
    assert button, "button not visible in the half-size screenshot"
    px, py = center(button)
    desk.click(round(px / result["scale"]), round(py / result["scale"]))
    assert app.wait_for(lambda s: s["label"] == "clicked")["label"] == "clicked"
