"""Two own-pointer agents in two processes on one private X screen, typing into their own GTK windows
at the same time, with no seat between them. Safe on a real desktop: it never uses $DISPLAY.
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
from ccdesk.backends.mpx import Mpx, parse_xinput_list
from ccdesk.errors import CcdeskError
from tests.test_integration_multiagent import PY, ROOT, _skip_reason, open_two, screen, wait_for, words  # noqa: F401

pytestmark = [pytest.mark.skipif(_skip_reason() is not None or not shutil.which("xinput"),
                                 reason=_skip_reason() or "xinput not on PATH")]


def worker(name: str, window_id: str, tmp: str, start_at: float) -> None:
    """One agent process: its own devices, its own seat, its own window. Prints a JSON result."""
    from ccdesk.backends.x11_own import OwnPointerBackend
    from ccdesk.desktop import Desktop
    from ccdesk.safety import AuditLog

    env = dict(os.environ)
    tmp_path = Path(tmp)
    mpx = Mpx(name, env)
    mpx.create()
    try:
        desk = Desktop(backend=OwnPointerBackend(mpx, env=env), agent=name,
                       audit=AuditLog(tmp_path / f"audit-{name}.log"), shots_dir=tmp_path / f"shots-{name}",
                       screen_diff=False)
        errors = []
        while time.time() < start_at:
            time.sleep(0.005)
        desk.activate_window(window_id)
        window = next(w for w in desk.backend.list_windows() if w.id == window_id)
        try:
            desk.click(window.x + window.width // 2, window.y + window.height // 2)
        except CcdeskError as exc:
            errors.append(f"click: {exc}")
        for word in words(name):
            try:
                desk.type_text(word + " ")
            except CcdeskError as exc:
                errors.append(f"{word}: {exc}")
        print(json.dumps({"errors": errors, "seat": desk.seat.key}))
    finally:
        mpx.close()


def core_pointer(env) -> str:
    return subprocess.run(["xdotool", "getmouselocation"], env=env, capture_output=True, text=True).stdout.split(" screen")[0]


def devices(env) -> list[dict]:
    out = subprocess.run(["xinput", "list"], env=env, capture_output=True, text=True).stdout
    return [r for r in parse_xinput_list(out) if r["name"].startswith("ccdesk-")]


def test_two_agents_type_at_once_with_their_own_pointers(screen, tmp_path):
    hl, env, procs = screen
    ids, files = open_two(env, procs, tmp_path)
    backend = X11Backend(env=env)
    for wid, x in zip(ids, (50, 700)):  # side by side, so each click lands on its own window
        backend.move_window(next(w for w in backend.list_windows() if w.id == wid), x, 100)
    time.sleep(0.5)
    subprocess.run(["xdotool", "mousemove", "1000", "700"], env=env, check=True)
    before = core_pointer(env)
    start_at = time.time() + 1.5
    agents = []
    for name, wid in zip(("a", "b"), ids):
        code = ("import sys; from tests.test_integration_own_pointer import worker; "
                "worker(sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4]))")
        agents.append(subprocess.Popen([sys.executable, "-c", code, name, wid, str(tmp_path), str(start_at)],
                                       env=env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    results = []
    for p in agents:
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0, err
        results.append(json.loads(out.strip().splitlines()[-1]))
    time.sleep(0.5)  # the app saves its text every 100 ms
    assert [r["errors"] for r in results] == [[], []]
    assert results[0]["seat"] != results[1]["seat"]
    for name, f in zip(("a", "b"), files):
        assert f.read_text().split() == words(name)
    assert core_pointer(env) == before
    assert devices(env) == []  # both servers removed their devices on the way out


def test_a_stale_master_from_a_crash_is_replaced(screen):
    hl, env, procs = screen
    subprocess.run(["xinput", "create-master", "ccdesk-stale"], env=env, check=True)
    old = {r["id"] for r in devices(env)}
    assert old
    m = Mpx("stale", env)
    m.create()
    try:
        now = devices(env)
        assert [r["name"] for r in now if r["kind"] == "master"].count("ccdesk-stale pointer") == 1
        assert len([r for r in now if r["kind"] == "master"]) == 2  # one pair, not the stale one plus ours
        assert m.query_pointer() is not None
    finally:
        m.close()
    assert devices(env) == []


# -- bspwm must ignore the agent's clicks ----------------------------------------------------------------

BUTTON_APP = Path(__file__).with_name("gtk_button_app.py")


def bspc(env, *args) -> str:
    return subprocess.run(["bspc", *args], env=env, capture_output=True, text=True).stdout.strip()


def core_focus(env) -> int:
    return int(subprocess.run(["xdotool", "getwindowfocus"], env=env, capture_output=True, text=True).stdout)


def test_bspwm_does_not_move_the_users_focus_on_agent_clicks(screen, tmp_path):
    from ccdesk.backends.x11_own import OwnPointerBackend

    hl, env, procs = screen
    if hl.info().get("window_manager") != "bspwm" or not shutil.which("bspc"):
        pytest.skip("needs bspwm as the headless window manager")
    backend = X11Backend(env=env)
    ids, files = [], []
    for name in ("A", "B"):
        f = tmp_path / f"{name}.txt"
        files.append(f)
        procs.append(subprocess.Popen(["/usr/bin/python3", str(BUTTON_APP), str(f), f"BTN-{name}"],
                                      env=dict(env, GDK_BACKEND="x11")))
        ids.append(wait_for(lambda n=name: next((w.id for w in backend.list_windows() if w.title == f"BTN-{n}"), None)))
        wait_for(f.exists)
    a, b = ids
    for wid, x in zip(ids, (50, 700)):
        backend.move_window(next(w for w in backend.list_windows() if w.id == wid), x, 100)
    time.sleep(0.5)
    win_a = next(w for w in backend.list_windows() if w.id == a)
    original = bspc(env, "config", "click_to_focus")
    assert original and original != "none"

    mpx = Mpx("clk", env)
    mpx.create()
    try:
        own = OwnPointerBackend(mpx, env=env)
        assert own.click_guard.active()
        bspc(env, "node", b, "-f")  # the user is working in B
        time.sleep(0.3)
        user_focus = core_focus(env)
        assert user_focus == int(b, 16)
        for _ in range(10):
            own.click(win_a.x + win_a.width // 2, win_a.y + win_a.height // 2, 1, 1)
            time.sleep(0.15)
            assert core_focus(env) == user_focus
            assert bspc(env, "query", "-N", "-n", "focused") == b
            assert bspc(env, "config", "click_to_focus") == original
        time.sleep(0.3)
        assert files[0].read_text() == "CLICKED 10"
        assert files[1].read_text() == "CLICKED 0"
        assert bspc(env, "config", "click_to_focus") == original
        # the user's own click, on the core pointer, still focuses the window
        subprocess.run(["xdotool", "mousemove", str(win_a.x + win_a.width // 2), str(win_a.y + 50), "click", "1"],
                       env=env, check=True)
        wait_for(lambda: core_focus(env) == int(a, 16))
    finally:
        mpx.close()


def test_a_second_agent_with_the_same_name_is_refused_and_leftovers_of_dead_ones_are_cleaned(screen):
    from ccdesk.doctor import housekeeping

    hl, env, procs = screen
    subprocess.run(["xinput", "create-master", "ccdesk-dead"], env=env, check=True)  # what kill -9 leaves
    first = Mpx("dup", env)
    first.create()
    try:
        assert first.cleaned == ["ccdesk-dead"]
        ids = {r["id"] for r in devices(env)}
        with pytest.raises(CcdeskError, match="another ccdesk agent named dup is running"):
            Mpx("dup", env).create()
        assert {r["id"] for r in devices(env)} == ids  # the first agent's pair is untouched
        subprocess.run(["xinput", "create-master", "ccdesk-dead2"], env=env, check=True)
        done = housekeeping(X11Backend(env=env))
        assert done["removed_devices"] == ["ccdesk-dead2"]  # doctor, with the live "dup" left alone
        assert {r["name"] for r in devices(env) if r["kind"] == "master"} == {"ccdesk-dup pointer", "ccdesk-dup keyboard"}
    finally:
        first.close()
    assert devices(env) == []
