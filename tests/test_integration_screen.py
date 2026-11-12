"""A named screen end to end: `ccdesk screen start` as a subprocess, two servers joined to it.

Safe on a real desktop: it never uses $DISPLAY or your session bus.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys

import pytest

from ccdesk import screen
from ccdesk.backends import X11Backend
from ccdesk.desktop import Desktop
from ccdesk.safety import AuditLog, Policy
from tests.test_integration_a11y import PY
from tests.test_integration_headless import _skip_reason, wait_for
from tests.test_integration_multiagent import APP

pytestmark = pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")


def desk_for(name, tmp_path):
    joined = screen.join("t1")  # what `ccdesk mcp --screen t1` does
    return Desktop(backend=X11Backend(env=joined.env), policy=Policy(allow_launch=True),
                   audit=AuditLog(tmp_path / f"{name}-audit.log"), shots_dir=tmp_path / "shots",
                   headless=joined, agent=name)


def test_two_servers_share_one_named_screen(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    proc = subprocess.Popen([sys.executable, "-m", "ccdesk", "screen", "start", "t1", "--size", "1280x800"],
                            stdout=subprocess.PIPE, text=True)
    try:
        info = wait_for(lambda: screen.read_state("t1"), 20)
        assert info["pid"] == proc.pid
        assert "ccdesk mcp --screen t1" in proc.stdout.readline()
        second = subprocess.run([sys.executable, "-m", "ccdesk", "screen", "start", "t1"],
                                capture_output=True, text=True)
        assert second.returncode == 1 and "already running" in second.stderr

        a = desk_for("a", tmp_path)
        b = desk_for("b", tmp_path)
        assert a.backend.env["DISPLAY"] == b.backend.env["DISPLAY"] == info["display"]
        state = tmp_path / "state.txt"
        a.launch(f"{PY} {shlex.quote(str(APP))} {shlex.quote(str(state))}")

        wait_for(lambda: any(w["title"] == "ccdesk-text" for w in b.list_windows()["windows"]))

        def tree_of_b():
            try:
                out = b.ui_tree(window="ccdesk-text")  # an agent names its window
            except Exception:
                return None
            return out if "ccdesk-text" in out["tree"] else None

        assert wait_for(tree_of_b)
        socket = f"/tmp/.X11-unix/X{info['display'].lstrip(':')}"
        assert os.path.exists(socket)

        proc.send_signal(signal.SIGTERM)
        assert proc.wait(15) == 0
        assert screen.running() == []
        assert not (screen.state_dir() / "t1.json").exists()
        wait_for(lambda: not os.path.exists(socket), 10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
