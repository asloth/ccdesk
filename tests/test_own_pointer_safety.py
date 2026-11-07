"""Safety around an own-pointer agent: bspwm click guard, one process per agent name, a vanished
target window, ui_tree on the agent's window, signals, leftovers, and the display seat of the fallback."""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from ccdesk.a11y import A11y
from ccdesk.backends.mpx import NameLock, clean_leftovers
from ccdesk.desktop import Desktop
from ccdesk.doctor import build_report, format_report, housekeeping
from ccdesk.errors import CcdeskError, PolicyError
from ccdesk.safety import AuditLog, Policy
from ccdesk.seat import SeatLock, seat_key
from ccdesk.wmguard import ClickGuard
from tests.test_own_pointer import Backend, FakeMpx, NoDisplayMpx, ScriptedXinput, win


@pytest.fixture(autouse=True)
def private_locks(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))


ENV = {"DISPLAY": ":9"}


class FakeBspc:
    """`bspc config click_to_focus` as a function of argv, with a log of every value it was set to."""

    def __init__(self, value="button1", reachable=True):
        self.value, self.reachable, self.sets = value, reachable, []

    def __call__(self, argv):
        if not self.reachable:
            return None
        if argv[-1] == "click_to_focus":
            return self.value + "\n"
        self.value = argv[-1]
        self.sets.append(argv[-1])
        return ""


def guard(bspc, tmp_path, **kw):
    return ClickGuard(bspc, ENV, directory=tmp_path / "ccdesk", sleep=lambda s: None, **kw)


# -- A: bspwm ignores the agent's clicks -----------------------------------------------------------


def test_press_runs_with_click_to_focus_none_and_puts_the_value_back(tmp_path):
    bspc = FakeBspc("button1")
    g = guard(bspc, tmp_path)
    seen = []
    with g.quiet():
        seen.append(bspc.value)
        assert g.state_path.read_text() == "button1"  # saved before it is changed
    assert seen == ["none"]
    assert bspc.value == "button1" and not g.state_path.exists()


def test_the_value_comes_back_when_the_press_fails(tmp_path):
    bspc = FakeBspc("button1")
    g = guard(bspc, tmp_path)
    with pytest.raises(RuntimeError):
        with g.quiet():
            raise RuntimeError("x gone")
    assert bspc.value == "button1" and not g.state_path.exists()


def test_nested_presses_set_it_once(tmp_path):
    bspc = FakeBspc()
    g = guard(bspc, tmp_path)
    with g.quiet():
        with g.quiet():
            pass
        assert bspc.value == "none"
    assert bspc.sets == ["none", "button1"]


def test_other_window_managers_are_left_alone(tmp_path):
    bspc = FakeBspc(reachable=False)
    g = guard(bspc, tmp_path)
    assert g.active() is False
    with g.quiet():
        pass
    assert bspc.sets == []
    assert not g.state_path.exists()
    assert guard(FakeBspc(), tmp_path, which=lambda n: None).active() is False  # no bspc on PATH


def test_a_state_file_keeps_the_real_original_not_none(tmp_path):
    bspc = FakeBspc("none")  # a crash left it off
    g = guard(bspc, tmp_path)
    g.state_path.parent.mkdir(parents=True)
    g.state_path.write_text("button1")
    with g.quiet():
        assert g.state_path.read_text() == "button1"
    assert bspc.value == "button1"


def test_recover_restores_after_a_crash_only_when_the_lock_is_free(tmp_path):
    bspc = FakeBspc("none")
    g = guard(bspc, tmp_path)
    g.state_path.parent.mkdir(parents=True)
    g.state_path.write_text("button1")
    fd = os.open(g.lock_path, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)  # a live process is mid-click
    assert g.recover() is None and bspc.value == "none"
    os.close(fd)
    assert g.recover() == "button1"
    assert bspc.value == "button1" and not g.state_path.exists()
    assert g.recover() is None


def test_two_guards_do_not_overlap(tmp_path):
    """Two agents (here two guards on one lock file): the second waits and saves the first's original."""
    bspc = FakeBspc("button1")
    a, b = guard(bspc, tmp_path), guard(bspc, tmp_path)
    order = []
    entered = threading.Event()

    def second():
        entered.wait()
        with b.quiet():
            order.append(("b", bspc.value))

    t = threading.Thread(target=second)
    t.start()
    with a.quiet():
        entered.set()
        time.sleep(0.2)
        order.append(("a", bspc.value))
    t.join(5)
    assert order == [("a", "none"), ("b", "none")]
    assert bspc.value == "button1"  # b did not save "none" as the original


def test_click_drag_and_scroll_all_go_through_the_guard(tmp_path):
    mpx = FakeMpx()
    b = Backend(mpx)
    bspc = FakeBspc("button1")
    b.click_guard = guard(bspc, tmp_path)
    during = []
    mpx.button = lambda button, press: during.append(bspc.value)
    b.click(1, 1, 1, 2)
    b.drag(0, 0, 5, 5, 100)
    b.scroll(1, 1, 0, 2)
    assert set(during) == {"none"} and len(during) >= 8
    assert bspc.value == "button1"


def test_doctor_notes_a_window_manager_that_is_not_handled(tmp_path):
    b = Backend(FakeMpx())
    report = build_report(b, Policy(), "/tmp/audit.log")
    assert report["own_pointer"]["bspwm_click_guard"] is False
    assert any("not bspwm" in w for w in report["warnings"])
    b.click_guard = guard(FakeBspc(), tmp_path)
    report = build_report(b, Policy(), "/tmp/audit.log")
    assert report["own_pointer"]["bspwm_click_guard"] is True
    assert not any("not bspwm" in w for w in report["warnings"])


# -- B: one live process per agent name --------------------------------------------------------------


def test_a_second_agent_with_the_same_name_is_refused_and_the_first_keeps_its_devices():
    run = ScriptedXinput()
    first = NoDisplayMpx("a", ENV, run=run)
    first.create()
    second = NoDisplayMpx("a", ENV, run=run)
    run.calls.clear()
    with pytest.raises(CcdeskError, match="another ccdesk agent named a is running on this screen; "
                                          "use another --agent name"):
        second.create()
    assert run.calls == []  # it did not touch xinput: nothing removed, nothing made
    first.close()
    third = NoDisplayMpx("a", ENV, run=run)
    third.create()  # the name is free again
    third.close()


def test_close_only_removes_a_pair_that_is_still_ours():
    run = ScriptedXinput()
    m = NoDisplayMpx("a", ENV, run=run)
    m.create()
    run.has_pair = False  # something else removed it
    run.calls.clear()
    m.close()
    assert [c[0] for c in run.calls] == ["list"]


def test_close_leaves_a_pair_with_a_different_name_on_the_same_id():
    run = ScriptedXinput()
    m = NoDisplayMpx("a", ENV, run=run)
    m.create()
    run.name = "ccdesk-other"  # the id 8 now belongs to another agent's pair
    run.calls.clear()
    m.close()
    assert "remove-master" not in [c[0] for c in run.calls]


# -- E: leftovers -------------------------------------------------------------------------------------


def test_leftovers_are_removed_only_when_their_owner_lock_is_free():
    run = ScriptedXinput(stale=True)  # a pair named ccdesk-a that nobody owns
    held = NameLock(ENV, "a")
    held.acquire()  # a live agent owns it
    assert clean_leftovers(run, ENV) == []
    held.release()
    assert clean_leftovers(run, ENV) == ["ccdesk-a"]
    assert [c[0] for c in run.calls if c[0] == "remove-master"] == ["remove-master"]


def test_create_cleans_other_dead_agents():
    run = ScriptedXinput()
    run.extra = True  # a pair of an agent "dead" whose process is gone
    m = NoDisplayMpx("a", ENV, run=run)
    m.create()
    assert m.cleaned == ["ccdesk-dead"]
    assert ("remove-master", "20", "AttachToMaster", "2", "3") in run.calls


def test_doctor_removes_leftovers_and_lists_them():
    from ccdesk.backends.x11 import X11Backend

    run = ScriptedXinput(stale=True)

    class B(X11Backend):
        def run(self, argv, **kw):
            return run(*argv[1:])

        def try_run(self, argv, **kw):
            return None  # not bspwm

    done = housekeeping(B(env=ENV, which=lambda n: "/usr/bin/" + n))
    assert done["removed_devices"] == ["ccdesk-a"]
    report = build_report(Backend(FakeMpx()), Policy(), "/tmp/a.log")
    report["cleanup"] = done
    assert "removed leftover devices: ccdesk-a" in format_report(report)


def test_housekeeping_restores_click_to_focus():
    from ccdesk.backends.x11 import X11Backend

    bspc = FakeBspc("none")
    g = ClickGuard(bspc, ENV)
    g.state_path.parent.mkdir(parents=True, exist_ok=True)
    g.state_path.write_text("button1")

    class B(X11Backend):
        def try_run(self, argv, **kw):
            return bspc(argv)

    done = housekeeping(B(env=ENV, which=lambda n: "/usr/bin/" + n))
    assert done["restored_click_to_focus"] == "button1" and bspc.value == "button1"


# -- C: the target window is gone -----------------------------------------------------------------------


def desk(tmp_path, windows):
    mpx = FakeMpx()
    backend = Backend(mpx, windows=windows)
    d = Desktop(backend=backend, policy=Policy(), audit=AuditLog(tmp_path / "a.log", agent="a"), agent="a",
                shots_dir=tmp_path / "s", screen_diff=False)
    d.clock = lambda: 0.0
    return d, backend, mpx


def test_input_is_refused_when_the_target_window_is_gone_even_with_no_known_receiver(tmp_path):
    d, backend, mpx = desk(tmp_path, [win("0x00400010", "mine"), win("0x00500010", "user's")])
    d.activate_window("0x00400010")
    backend.windows = [win("0x00500010", "user's")]  # the target closed
    mpx.focus_window = 0x600001  # the agent's focus fell to a window that is not listed
    mpx.under = [0x600001]
    for call in (lambda: d.type_text("LEAK"), lambda: d.click(5, 5), lambda: d.press_key("ctrl+a")):
        with pytest.raises(PolicyError, match="is gone"):
            call()
    assert not [c for c in mpx.calls if c[0] in ("type_text", "press_key")]
    assert d.type_text("ok", force=True)  # force still sends it


def test_gone_target_is_refused_on_the_core_path_too(make_desk, fake):
    d = make_desk()
    d.activate_window("0x01e00003")
    fake.windows = [w for w in fake.windows if w.id != "0x01e00003"]
    fake.active = None
    with pytest.raises(PolicyError, match="is gone"):
        d.type_text("x")


# -- D: ui_tree reads the agent's window -----------------------------------------------------------------


def test_ui_tree_without_a_window_reads_the_agents_window(tmp_path, helper):
    d, backend, mpx = desk(tmp_path, [win("0x00400010", "Agent window"), win("0x00500010", "User window")])
    d.a11y = A11y(runner=helper, pythons=("/bin/sh",))
    d.activate_window("0x00400010")
    d.ui_tree()
    assert helper.requests[-1]["window"] == "Agent window"
    d.ui_tree(window="User window")  # an explicit window still wins
    assert helper.requests[-1]["window"] == "User window"
    mpx.focus_window = None  # no focus of its own: the helper's default
    d.ui_tree()
    assert helper.requests[-1]["window"] is None


def test_ui_tree_on_the_core_path_keeps_the_helper_default(make_desk, helper):
    d = make_desk()
    d.ui_tree()
    assert helper.requests[-1]["window"] is None


# -- F: the fallback activation takes the display seat ------------------------------------------------------


def test_fallback_activation_holds_the_display_seat(tmp_path, monkeypatch):
    seat = SeatLock("display", directory=tmp_path / "seats", timeout=1)
    b = Backend(FakeMpx(), shown=False)
    b._display_seat = seat
    held = []
    monkeypatch.setattr("ccdesk.backends.x11.X11Backend.activate_window", lambda self, w: held.append(seat._depth))
    b.activate_window(win("0x00400010"))
    assert held == [1]
    assert seat._depth == 0


def test_display_seat_defaults_to_the_screens_seat():
    assert Backend(FakeMpx()).display_seat.key == seat_key("x11", ENV)


# -- E: a signal ends the server promptly (a real process, a fake xinput) -------------------------------------

CHILD = """
import os, time
from types import SimpleNamespace
from ccdesk import cli
from ccdesk.backends import mpx as m


class Fake(m.Mpx):
    def _open(self):
        pass

    def devices(self):
        return {"master_pointer": 8, "master_keyboard": 9, "slave_pointer": 10, "slave_keyboard": 11}


m.Mpx = Fake
m.unavailable_reason = lambda *a: None


class B:
    name = "x11"
    env = dict(os.environ)
    which = staticmethod(lambda n: None)


cli._own_pointer(SimpleNamespace(agent="sig"), B())
print("ready", flush=True)
time.sleep(60)
"""


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_removes_the_devices_and_ends_the_process(tmp_path, sig):
    xinput = tmp_path / "bin" / "xinput"
    xinput.parent.mkdir()
    state = tmp_path / "pair"
    xinput.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f" list) [ -f {state} ] && printf '⎡ ccdesk-sig pointer\\tid=8\\t[master pointer  (9)]\\n' ;;\n"
        f" create-master) touch {state} ;;\n"
        f" remove-master) rm -f {state}; echo removed >> {tmp_path}/log ;;\n"
        "esac\n")
    xinput.chmod(0o755)
    env = {**os.environ, "PATH": f"{xinput.parent}:{os.environ['PATH']}", "XDG_RUNTIME_DIR": str(tmp_path),
           "DISPLAY": ":9", "PYTHONPATH": os.getcwd()}
    p = subprocess.Popen([sys.executable, "-c", CHILD], env=env, stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "ready"
    p.send_signal(sig)
    assert p.wait(5) == -sig  # killed by the signal itself, promptly
    assert (tmp_path / "log").read_text().strip() == "removed"
