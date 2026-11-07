"""Two agents in two processes on one private X screen, typing into their own GTK windows.

With the seat lock and agent names, each window ends up with exactly its own agent's words.
The control (no agent name, a no-op lock) documents what happens without them.
Safe on a real desktop: it never uses $DISPLAY.
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
from ccdesk.errors import CcdeskError
from ccdesk.headless import Headless
from ccdesk.safety import AuditLog
from ccdesk.seat import SeatLock, seat_key

APP = Path(__file__).with_name("gtk_text_app.py")
PY = os.environ.get("CCDESK_A11Y_PYTHON", "/usr/bin/python3")
ROOT = Path(__file__).resolve().parent.parent
WORDS = 25


def words(name: str) -> list[str]:
    return [f"{name}{i:02d}" for i in range(WORDS)]


def _skip_reason() -> str | None:
    for tool in ("Xvfb", "dbus-daemon", "xdotool"):
        if not shutil.which(tool):
            return f"{tool} not on PATH"
    probe = "import gi; gi.require_version('Gtk', '3.0')"
    if not os.path.exists(PY) or subprocess.run([PY, "-c", probe], capture_output=True).returncode:
        return f"{PY} has no PyGObject with GTK 3"
    return None


pytestmark = [pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")]


class NoLock:
    """Stands in for SeatLock: nothing is held, so the agents race."""

    key = "none"
    waited_ms = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def worker(name: str, window_id: str, tmp: str, locked: bool, start_at: float) -> None:
    """One agent process: activate its window once, then type its words. Prints a JSON result."""
    env = dict(os.environ)
    tmp_path = Path(tmp)
    seat = SeatLock(seat_key("x11", env), directory=tmp_path / "locks") if locked else NoLock()
    desk = Desktop(backend=X11Backend(env=env), agent=name if locked else None,
                   audit=AuditLog(tmp_path / f"audit-{name}.log"), shots_dir=tmp_path / f"shots-{name}",
                   screen_diff=False, seat=seat)
    errors = []
    while time.time() < start_at:
        time.sleep(0.005)
    desk.activate_window(window_id)
    for word in words(name):
        try:
            desk.type_text(word + " ")
        except CcdeskError as exc:
            errors.append(f"{word}: {exc}")
    print(json.dumps({"errors": errors}))


@pytest.fixture
def screen(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    hl = Headless(size="1280x800")
    try:
        env = hl.start()
    except CcdeskError as exc:
        pytest.skip(f"no headless screen: {exc}")
    if not hl.info().get("window_manager"):
        hl.stop()
        pytest.skip("no window manager on the headless screen: focus is not tracked")
    procs = []
    yield hl, env, procs
    for p in procs:
        p.terminate()
        p.wait(5)
    hl.stop()


def wait_for(read, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if value:
            return value
        time.sleep(0.1)
    pytest.fail("timed out")


def open_two(env, procs, tmp_path):
    backend = X11Backend(env=env)
    ids, files = [], []
    for name in ("a", "b"):
        f = tmp_path / f"{name}.txt"
        files.append(f)
        procs.append(subprocess.Popen([PY, str(APP), str(f)], env=dict(env, GDK_BACKEND="x11")))
        known = set(ids)

        def new():
            mine = [w.id for w in backend.list_windows() if w.title == "ccdesk-text" and w.id not in known]
            return mine[0] if mine else None

        ids.append(wait_for(new))
        wait_for(f.exists)
    return ids, files


def run_agents(env, ids, tmp_path, locked: bool) -> list[dict]:
    start_at = time.time() + 1.0
    agents = []
    for name, wid in zip(("a", "b"), ids):
        code = ("import sys; from tests.test_integration_multiagent import worker; "
                "worker(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == '1', float(sys.argv[5]))")
        agents.append(subprocess.Popen(
            [sys.executable, "-c", code, name, wid, str(tmp_path), "1" if locked else "0", str(start_at)],
            env=env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    results = []
    for p in agents:
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0, err
        results.append(json.loads(out.strip().splitlines()[-1]))
    time.sleep(0.5)  # the app saves its text every 100 ms
    return results


def test_two_agents_keep_to_their_own_windows(screen, tmp_path):
    hl, env, procs = screen
    ids, files = open_two(env, procs, tmp_path)
    results = run_agents(env, ids, tmp_path, locked=True)
    assert results == [{"errors": []}, {"errors": []}]
    for name, f in zip(("a", "b"), files):
        assert f.read_text().split() == words(name)


def test_without_lock_or_names_the_agents_collide(screen, tmp_path):
    hl, env, procs = screen
    ids, files = open_two(env, procs, tmp_path)
    results = run_agents(env, ids, tmp_path, locked=False)
    errored = any(r["errors"] for r in results)
    mixed = any(f.read_text().split() != words(n) for n, f in zip(("a", "b"), files))
    if not (errored or mixed):
        pytest.skip("the two agents did not collide on this run (timing)")
