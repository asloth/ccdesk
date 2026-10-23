"""--headless end to end: a private Xvfb screen and bus, a GTK app launched there, driven by ui_tree.

Safe on a real desktop: it never uses $DISPLAY or your session bus. Needs Xvfb, dbus-daemon,
at-spi2-core and a system Python with PyGObject + GTK 3.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time

import pytest

from ccdesk.a11y import A11y
from ccdesk.backends import X11Backend
from ccdesk.desktop import Desktop
from ccdesk.errors import CcdeskError
from ccdesk.headless import Headless, find_libexec, running
from ccdesk.safety import AuditLog, Policy
from tests.test_integration_a11y import APP, PY, ref_of


def _skip_reason() -> str | None:
    for tool in ("Xvfb", "dbus-daemon", "xdotool"):
        if not shutil.which(tool):
            return f"{tool} not on PATH"
    if not find_libexec("at-spi2-registryd"):
        return "at-spi2-core not installed"
    probe = "import gi; gi.require_version('Gtk', '3.0'); gi.require_version('Atspi', '2.0')"
    if not os.path.exists(PY) or subprocess.run([PY, "-c", probe], capture_output=True).returncode:
        return f"{PY} has no PyGObject with GTK 3 and Atspi"
    return None


pytestmark = pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")


@pytest.fixture
def screen(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    hl = Headless(size="1280x800")
    env = hl.start()
    yield hl, env
    hl.stop()


def wait_for(read, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if value:
            return value
        time.sleep(0.1)
    pytest.fail("timed out")


def test_agent_works_on_its_own_screen(screen, tmp_path):
    hl, env = screen
    assert env["DISPLAY"] == hl.display and env["DISPLAY"] != os.environ.get("DISPLAY")
    assert hl.private_bus and "WAYLAND_DISPLAY" not in env
    assert [s["display"] for s in running()] == [hl.display]

    desk = Desktop(backend=X11Backend(env=env), policy=Policy(allow_launch=True),
                   audit=AuditLog(tmp_path / "audit.log"), shots_dir=tmp_path / "shots", headless=hl)
    report = desk.doctor()
    assert report["screen"] == {"width": 1280, "height": 800}
    assert report["headless"]["display"] == hl.display

    state = tmp_path / "state.json"
    desk.launch(f"{PY} {shlex.quote(str(APP))} {shlex.quote(str(state))}")

    def read_state():
        try:
            return json.loads(state.read_text())
        except (OSError, ValueError):
            return {}

    wait_for(lambda: read_state().get("ready"))
    tree = wait_for(lambda: _tree(desk))["tree"]

    desk.click_element(ref_of(tree, r'(text|entry) "Display name"'))
    out = desk.type_text("Sara")
    assert '="Sara"' in out["changes"]
    desk.click_element(ref_of(tree, r'(push )?button "Apply settings"'))
    assert wait_for(lambda: read_state().get("clicks") == 1 and read_state())["entry"] == "Sara"

    # Replace the field's contents and submit it, with a trailing newline sent as a real Return.
    desk.click_element(ref_of(tree, r'(text|entry) "Display name"'))
    out = desk.type_text("Ana\n", replace=True)
    assert out["replaced"] and out["submitted"]
    assert wait_for(lambda: read_state().get("submitted"))
    assert read_state()["entry"] == read_state()["submitted"] == "Ana"

    # Your own session bus never sees the agent's app.
    try:
        mine = A11y().apps()
    except CcdeskError:
        mine = []
    assert not any("ccdesk-gtk" in a["windows"] for a in mine)

    desk.clipboard("set", "agent-only")
    assert desk.clipboard()["text"] == "agent-only"


def _tree(desk):
    try:
        out = desk.ui_tree()
    except CcdeskError:
        return None
    return out if re.search(r'"Apply settings"', out["tree"]) else None


def test_stop_leaves_nothing_behind(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    hl = Headless(size="800x600")
    hl.start()
    pids = [p.pid for p in hl._procs]
    socket = f"/tmp/.X11-unix/X{hl.display.lstrip(':')}"
    assert os.path.exists(socket)
    hl.stop()
    for pid in pids:
        assert not os.path.exists(f"/proc/{pid}") or "zombie" in open(f"/proc/{pid}/status").read()
    assert not os.path.exists(socket)
    assert running() == []
