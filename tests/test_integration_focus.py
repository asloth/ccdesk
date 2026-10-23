"""The wrong-window guard on a real X server with a window manager: a private headless screen.

Two Tk windows; the test activates one through ccdesk, then moves focus to the other behind
ccdesk's back (as a user switching to their terminal would). Typing must be refused.
Safe on a real desktop: it never uses $DISPLAY.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ccdesk.backends import X11Backend
from ccdesk.desktop import Desktop
from ccdesk.errors import CcdeskError, PolicyError
from ccdesk.headless import Headless
from ccdesk.safety import AuditLog, Policy

APP = Path(__file__).with_name("tk_app.py")


def _skip_reason() -> str | None:
    for tool in ("Xvfb", "dbus-daemon", "xdotool"):
        if not shutil.which(tool):
            return f"{tool} not on PATH"
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return "tkinter not available"
    return None


pytestmark = pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")


def wait_for(read, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if value:
            return value
        time.sleep(0.1)
    pytest.fail("timed out")


def state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


@pytest.fixture
def screen(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    hl = Headless(size="1280x800")
    try:
        env = hl.start()
    except CcdeskError as exc:
        pytest.skip(f"no headless screen: {exc}")
    procs = []
    yield hl, env, procs
    for p in procs:
        p.terminate()
        p.wait(5)
    hl.stop()


def test_input_follows_activate_window_only(screen, tmp_path):
    hl, env, procs = screen
    if not hl.info().get("window_manager"):
        pytest.skip("no window manager on the headless screen: focus is not tracked")
    backend = X11Backend(env=env)
    desk = Desktop(backend=backend, policy=Policy(), audit=AuditLog(tmp_path / "audit.log"),
                   shots_dir=tmp_path / "shots", headless=hl)
    states = [tmp_path / "a.json", tmp_path / "b.json"]
    for s in states:
        procs.append(subprocess.Popen([sys.executable, str(APP), str(s)], env=env))
        wait_for(lambda s=s: state(s).get("ready"))
    def both():
        mine = [w for w in desk.list_windows()["windows"] if w["title"] == "ccdesk-test"]
        return mine if len(mine) == 2 else None

    windows = wait_for(both)
    a, b = windows[0]["id"], windows[1]["id"]

    desk.activate_window(a)
    wait_for(lambda: backend.active_window() == a)
    assert desk.press_key("End")["window"]["id"] == a

    # The "user" focuses the other window.
    backend.run(["xdotool", "windowactivate", "--sync", str(int(b, 16))])
    wait_for(lambda: backend.active_window() == b)
    with pytest.raises(PolicyError, match=f"keyboard focus is on 'ccdesk-test' \\({b}\\)"):
        desk.type_text("oops")
    geo = windows[1]["geometry"]
    with pytest.raises(PolicyError, match="pointer"):
        desk.click(geo["x"] + geo["width"] // 2, geo["y"] + geo["height"] // 2)
    time.sleep(0.3)
    assert state(states[0])["entry"] == "" and state(states[1])["entry"] == ""

    desk.activate_window(a)
    wait_for(lambda: backend.active_window() == a)
    assert desk.type_text("ok")["window"]["id"] == a
