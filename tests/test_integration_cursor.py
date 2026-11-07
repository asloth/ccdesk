"""An agent's drawn cursor on a private X screen, next to a real GTK window.

The overlay must show up near the click, must never be what a click lands on, must not appear in
list_windows, and must leave the click itself working. Safe on a real desktop: never uses $DISPLAY.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from ccdesk.backends import X11Backend
from ccdesk.cursor import OFFSET, AgentCursor
from ccdesk.desktop import Desktop
from ccdesk.errors import CcdeskError
from ccdesk.headless import Headless
from ccdesk.safety import AuditLog

APP = Path(__file__).with_name("gtk_text_app.py")
PY = os.environ.get("CCDESK_A11Y_PYTHON", "/usr/bin/python3")
SHOT = "/tmp/claude-1000/-home-sabera-Projects-ccdesk/7a21dc52-7be7-4d5a-afb6-cec3320cec53/scratchpad/cursor.png"


def _skip_reason() -> str | None:
    for tool in ("Xvfb", "dbus-daemon", "xdotool"):
        if not shutil.which(tool):
            return f"{tool} not on PATH"
    probe = "import gi, cairo; gi.require_version('Gtk', '3.0')"
    if not os.path.exists(PY) or subprocess.run([PY, "-c", probe], capture_output=True).returncode:
        return f"{PY} has no PyGObject with GTK 3 and cairo"
    return None


pytestmark = [pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")]


def wait_for(read, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if value:
            return value
        time.sleep(0.1)
    pytest.fail("timed out")


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


def overlay_ids(env) -> list[str]:
    out = subprocess.run(["xdotool", "search", "--name", "ccdesk-cursor"], env=env, capture_output=True, text=True)
    return out.stdout.split()


def geometry(env, wid: str) -> dict:
    out = subprocess.run(["xdotool", "getwindowgeometry", "--shell", wid], env=env, capture_output=True, text=True)
    return dict(line.split("=") for line in out.stdout.split())


def test_the_cursor_follows_the_click_and_never_takes_it(screen, tmp_path):
    hl, env, procs = screen
    backend = X11Backend(env=env)
    state = tmp_path / "text.txt"
    procs.append(subprocess.Popen([PY, str(APP), str(state)], env=dict(env, GDK_BACKEND="x11")))
    cursor = AgentCursor("bot-1", env=env, python=PY)
    desk = Desktop(backend=backend, audit=AuditLog(tmp_path / "audit.log"), shots_dir=tmp_path / "shots",
                   screen_diff=False, agent="bot-1", cursor=cursor, headless=hl)
    try:
        win = wait_for(lambda: next((w for w in desk.list_windows()["windows"] if w["title"] == "ccdesk-text"), None))
        desk.activate_window(win["id"])
        g = win["geometry"]
        x, y = g["x"] + g["width"] // 2, g["y"] + g["height"] // 2
        desk.click(x, y)
        assert cursor.enabled, cursor.status()
        overlay = wait_for(lambda: overlay_ids(env))[0]
        wait_for(lambda: abs(int(geometry(env, overlay)["X"]) - (x + OFFSET)) <= 1)
        geo = geometry(env, overlay)
        assert (int(geo["X"]), int(geo["Y"])) == (x + OFFSET, y + OFFSET)

        # The click lands on the app, not on the overlay.
        assert backend.window_at(x, y) == win["id"]
        assert int(backend.window_at(x, y), 16) != int(overlay)
        assert backend.window_at(x + OFFSET + 3, y + OFFSET + 3) != f"0x{int(overlay):08x}"
        assert all(not w["title"].startswith("ccdesk-cursor:") for w in desk.list_windows()["windows"])

        # The click reached the app: its text box has the focus and takes the typing.
        desk.type_text("hello")
        wait_for(lambda: state.exists() and state.read_text() == "hello")

        desk.screenshot(path=SHOT)
    finally:
        cursor.close()
