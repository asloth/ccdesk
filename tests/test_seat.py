"""The input lock shared by several agents on one screen (ccdesk/seat.py) and how Desktop uses it."""

import multiprocessing
import os
from pathlib import Path

import pytest

from ccdesk.errors import CcdeskError, PolicyError
from ccdesk.seat import SeatLock, lock_dir, seat_key
from ccdesk.safety import INPUT_TOOLS

FIREFOX = "0x01e00003"
TERMINAL = "0x02000001"


def test_seat_key_follows_the_backend():
    env = {"DISPLAY": ":1", "WAYLAND_DISPLAY": "wayland-0"}
    assert seat_key("x11", env) == "_1"
    assert seat_key("wayland", env) == "wayland-0"
    assert seat_key("kde", {"WAYLAND_DISPLAY": "wayland-1"}) == "wayland-1"
    assert seat_key("x11", {}) == "default"
    assert seat_key("x11", {"DISPLAY": "../../etc/x:0"}) == ".._.._etc_x_0"


def test_lock_dir_prefers_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert lock_dir() == tmp_path / "ccdesk"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "missing"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "c"))
    assert lock_dir() == tmp_path / "c" / "ccdesk" / "locks"


def test_reentrant_in_one_process(tmp_path):
    lock = SeatLock("k", directory=tmp_path)
    with lock:
        with lock:
            assert lock._depth == 2
        assert lock._fd is not None
    assert lock._fd is None
    assert oct(os.stat(lock.path).st_mode & 0o777) == "0o600"


def _hold(path_dir, ready, release):
    with SeatLock("k", directory=path_dir):
        ready.set()
        release.wait(10)


def test_exclusive_across_processes(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_hold, args=(tmp_path, ready, release))
    child.start()
    try:
        assert ready.wait(10)
        with pytest.raises(CcdeskError, match="another agent has held the input on k"):
            with SeatLock("k", directory=tmp_path, timeout=0.2):
                pass
        release.set()
        child.join(10)
        with SeatLock("k", directory=tmp_path, timeout=2) as lock:  # free again once the holder left
            assert lock._fd is not None
    finally:
        release.set()
        child.join(10)


def test_a_dead_holder_frees_the_lock(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_hold, args=(tmp_path, ready, release))
    child.start()
    assert ready.wait(10)
    child.kill()
    child.join(10)
    with SeatLock("k", directory=tmp_path, timeout=2):
        pass


class RecordingSeat:
    unavailable = None

    def __init__(self):
        self.held = False
        self.entries = 0
        self.last = None

    def note_input(self, when):
        self.last = when

    def last_input(self):
        return self.last

    def __enter__(self):
        self.held = True
        self.entries += 1

    def __exit__(self, *exc):
        self.held = False


def test_input_runs_under_the_lock_and_reads_do_not(make_desk, fake):
    d = make_desk()
    seat = d.seat = RecordingSeat()
    seen = []
    real_click = fake.click
    fake.click = lambda *a: (seen.append(seat.held), real_click(*a))
    real_list = fake.list_windows
    d.click(10, 20)
    assert seen == [True] and seat.entries == 1
    fake.list_windows = lambda: (seen.append(seat.held), real_list())[1]
    d.list_windows()
    assert seen[-1] is False and seat.entries == 1
    d.clipboard("set", "x")
    assert seat.entries == 2


def test_every_input_tool_is_a_seat_user():
    # The lock wraps whatever _act runs for these names: keep them in INPUT_TOOLS.
    assert {"click", "type_text", "press_key", "activate_window", "drag", "scroll", "set_value",
            "click_element", "clipboard_set"} <= INPUT_TOOLS


def _raise_on_activate(fake):
    real = fake.activate_window

    def activate(window):
        real(window)
        fake.under = window.id  # activating raises the window, so it is under the pointer again
    fake.activate_window = activate


def test_agent_takes_its_window_back(make_desk, fake, read_audit):
    """Another agent focused its own window: an agent re-focuses its target instead of refusing."""
    d = make_desk(agent="bot-1")
    _raise_on_activate(fake)
    d.activate_window("Mozilla Firefox")
    fake.active = fake.under = TERMINAL  # the other agent's window
    assert d.type_text("hi")["window"]["id"] == FIREFOX
    assert ("activate", FIREFOX) in fake.calls[-3:]
    fake.active = fake.under = TERMINAL
    assert d.click(10, 20)["window"]["id"] == FIREFOX
    assert all(e.get("agent") == "bot-1" for e in read_audit())


def test_agent_still_refuses_when_its_window_is_gone(make_desk, fake):
    d = make_desk(agent="bot-1")
    d.activate_window("Mozilla Firefox")
    fake.windows = [w for w in fake.windows if w.id != FIREFOX]
    fake.active = fake.under = TERMINAL
    with pytest.raises(PolicyError, match="is gone"):
        d.type_text("hi")


def test_without_agent_focus_change_is_still_refused(make_desk, fake):
    d = make_desk()
    d.activate_window("Mozilla Firefox")
    fake.active = fake.under = TERMINAL
    with pytest.raises(PolicyError, match="the user may have switched windows"):
        d.type_text("hi")


def test_threads_of_one_process_take_turns(tmp_path):
    """The MCP server runs tool calls on worker threads: two calls of one agent must not overlap."""
    import threading
    import time as _t
    lock = SeatLock("k", directory=tmp_path)
    inside, peak = [0], [0]

    def work():
        with lock:
            inside[0] += 1
            peak[0] = max(peak[0], inside[0])
            _t.sleep(0.1)
            inside[0] -= 1

    threads = [threading.Thread(target=work) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1 and lock._fd is None


def test_timeout_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CCDESK_SEAT_TIMEOUT", "7")
    assert SeatLock("k", directory=tmp_path).timeout == 7
    monkeypatch.setenv("CCDESK_SEAT_TIMEOUT", "junk")
    assert SeatLock("k", directory=tmp_path).timeout == 180


def test_lock_dir_is_the_same_without_runtime_env(monkeypatch, tmp_path):
    import os as _os
    run = f"/run/user/{_os.getuid()}"
    if not _os.path.isdir(run):
        pytest.skip("no /run/user for this uid")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert lock_dir() == Path(run) / "ccdesk"


def test_unwritable_lock_dir_leaves_input_working(make_desk, fake, tmp_path):
    d = make_desk()
    d.seat = SeatLock("k", directory=Path("/proc/ccdesk-cannot-exist"))
    assert d.click(10, 20)["clicked"] == {"x": 10, "y": 20}
    assert "no input lock" in d.seat.unavailable
    assert any("no input lock" in w for w in d.doctor()["warnings"])


def test_yield_happens_before_the_seat(make_desk, fake):
    d = make_desk(yield_ms=500)
    seat = d.seat = RecordingSeat()
    held_while_yielding = []
    real = d._yield_to_user

    def spy():
        held_while_yielding.append(seat.held)
        return real()
    d._yield_to_user = spy
    d.click(10, 20)
    assert held_while_yielding and held_while_yielding[0] is False


def test_input_from_another_agent_is_not_the_user(make_desk, fake, tmp_path):
    """X's idle counter is reset by every agent: input right after another agent's is theirs."""
    d = make_desk(yield_ms=500)
    other = SeatLock("test", directory=tmp_path / "locks")
    with other:
        other.note_input(fake.clock())  # another agent on this screen just typed
    fake.last_event = fake.now  # the idle counter was just reset, but by the other agent
    assert d.click(10, 20).get("yielded_ms", 0) == 0


def test_user_coming_back_while_queued_is_refused(make_desk, fake):
    d = make_desk(yield_ms=500)
    calls = []

    def idle():
        calls.append(1)
        return 10_000 if len(calls) == 1 else 5  # idle before queueing, typing once the seat is ours
    fake.idle_ms = idle
    with pytest.raises(PolicyError, match="while this agent waited for another one"):
        d.click(10, 20)
    assert not [c for c in fake.calls if c[0] == "click"]


def test_a_seat_timeout_does_not_leave_the_yield_cached(make_desk, fake):
    d = make_desk(yield_ms=500)

    class Busy(RecordingSeat):
        def __enter__(self):
            raise CcdeskError("another agent has held the input")
    d.seat = Busy()
    with pytest.raises(CcdeskError):
        d.click(10, 20)
    assert d._yield_done is None
