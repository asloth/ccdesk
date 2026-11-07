"""An agent's own pointer and keyboard (--own-pointer): the mpx module, its backend and how Desktop
and the CLI use it. A fake stands in for the X libraries here; tests/test_integration_own_pointer.py
runs the real thing on Xvfb."""

from __future__ import annotations

import pytest

from ccdesk import cli
from ccdesk.backends.mpx import (Mpx, device_name, find_devices, keysym_for_char, parse_xinput_list,
                                 unavailable_reason)
from ccdesk.backends.x11_own import OwnPointerBackend
from ccdesk.desktop import Desktop
from ccdesk.doctor import build_report, format_report
from ccdesk.errors import PolicyError
from ccdesk.parsing import KeyCombo, Window, parse_key_combo
from ccdesk.safety import AuditLog, Policy
from ccdesk.seat import SeatLock, seat_key

XINPUT_LIST = """\
⎡ Virtual core pointer                    \tid=2\t[master pointer  (3)]
⎜   ↳ Virtual core XTEST pointer              \tid=4\t[slave  pointer  (2)]
⎜   ↳ Xvfb mouse                              \tid=6\t[slave  pointer  (2)]
⎜   ↳ ccdesk-a XTEST pointer                  \tid=10\t[slave  pointer  (8)]
⎣ Virtual core keyboard                   \tid=3\t[master keyboard (2)]
    ↳ Virtual core XTEST keyboard             \tid=5\t[slave  keyboard (3)]
    ↳ ccdesk-a XTEST keyboard                 \tid=11\t[slave  keyboard (9)]
⎡ ccdesk-a pointer                        \tid=8\t[master pointer  (9)]
⎜   ↳ ccdesk-a XTEST pointer                  \tid=10\t[slave  pointer  (8)]
⎣ ccdesk-a keyboard                       \tid=9\t[master keyboard (8)]
    ↳ ccdesk-a XTEST keyboard                 \tid=11\t[slave  keyboard (9)]
"""


class FakeMpx:
    def __init__(self):
        self.calls: list[tuple] = []
        self.focus_window: int | None = None
        self.under: list[int] = []
        self.ids = {"master_pointer": 8, "master_keyboard": 9, "slave_pointer": 10, "slave_keyboard": 11}

    def warp(self, x, y):
        self.calls.append(("warp", x, y))

    def button(self, button, press):
        self.calls.append(("button", button, press))

    def type_text(self, text):
        self.calls.append(("type_text", text))

    def press_key(self, combo):
        self.calls.append(("press_key", combo.xdotool()))

    def focus(self, window):
        self.calls.append(("focus", window))
        self.focus_window = window

    def query_focus(self):
        return self.focus_window

    def windows_under_pointer(self):
        return self.under

    def info(self):
        return {"enabled": True, "name": "ccdesk-a", **self.ids}


class Backend(OwnPointerBackend):
    """The own-pointer backend with a canned window list and no helper programs."""

    def __init__(self, mpx, windows=(), shown=True, desktop=0):
        super().__init__(mpx, env={"DISPLAY": ":9"}, which=lambda n: None)
        self.windows = list(windows)
        self.shown = shown
        self.desktop = desktop
        self.normal_activations: list[str] = []
        self.slept: list[float] = []

    def sleep(self, seconds):
        self.slept.append(seconds)

    def available(self):
        return None

    def list_windows(self):
        return list(self.windows)

    def window_on_show(self, window_id):
        return self.shown

    def current_desktop(self):
        return self.desktop

    def transient_for(self, window_id):
        return None

    def screen_size(self):
        return (1600, 900)


def win(wid, title="app", desktop=0):
    return Window(id=wid, title=title, wm_class="a.A", x=0, y=0, width=400, height=300, desktop=desktop)


@pytest.fixture
def mpx():
    return FakeMpx()


# -- device listing ----------------------------------------------------------------------------


def test_parse_xinput_list_and_find_devices():
    rows = parse_xinput_list(XINPUT_LIST)
    assert {"name": "Xvfb mouse", "id": 6, "kind": "slave", "type": "pointer"} in rows
    assert find_devices(rows, "ccdesk-a") == {"master_pointer": 8, "master_keyboard": 9,
                                              "slave_pointer": 10, "slave_keyboard": 11}
    assert find_devices(rows, "ccdesk-b") == {}


def test_device_names_use_the_agent_name():
    assert device_name("desk-1") == "ccdesk-desk-1"


def test_keysyms_for_characters():
    assert keysym_for_char("a") == 0x61
    assert keysym_for_char("ñ") == 0xF1
    assert keysym_for_char("€") == 0x01000000 + 0x20AC


def test_lookup_finds_shifted_characters_in_the_second_column():
    rows = [[0x61, 0x41], [0x31, 0x21], [0, 0]]
    table = Mpx.lookup(rows, 38)
    assert table[0x61] == (38, False)
    assert table[0x41] == (38, True)
    assert table[0x21] == (39, True)
    assert 0 not in table


# -- the Mpx lifecycle -----------------------------------------------------------------------------


class ScriptedXinput:
    """Answers `xinput list` from a list that create-master and remove-master change."""

    def __init__(self, stale=False):
        self.calls: list[tuple] = []
        self.has_pair = stale
        self.name = "ccdesk-a"  # whose pair `xinput list` shows (ids 8 to 11)
        self.extra = False  # also show a pair of an agent "dead" (ids 20 to 23)

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "list":
            text = XINPUT_LIST.replace("ccdesk-a", self.name) if self.has_pair else ""
            if self.extra:
                text += XINPUT_LIST.replace("ccdesk-a", "ccdesk-dead").replace("id=8", "id=20").replace(
                    "id=9", "id=21").replace("id=10", "id=22").replace("id=11", "id=23")
            return text
        if args[0] == "create-master":
            self.has_pair = True
        if args[0] == "remove-master":
            self.has_pair = False
        return ""


class NoDisplayMpx(Mpx):
    def _open(self):  # no X here
        pass


def test_create_removes_a_stale_pair_first_and_close_removes_ours():
    run = ScriptedXinput(stale=True)
    m = NoDisplayMpx("a", {"DISPLAY": ":9"}, run=run)
    m.create()
    verbs = [c[0] for c in run.calls if c[0] != "list"]
    assert verbs == ["remove-master", "create-master"]
    assert run.calls[[c[0] for c in run.calls].index("remove-master")] == ("remove-master", "8", "AttachToMaster", "2", "3")
    assert m.ids["master_keyboard"] == 9
    run.calls.clear()
    m.close()
    assert [c[0] for c in run.calls] == ["list", "remove-master"]  # it checks the pair is still ours
    m.close()  # idempotent
    assert [c[0] for c in run.calls] == ["list", "remove-master"]


def test_create_without_a_stale_pair_does_not_remove():
    run = ScriptedXinput()
    m = NoDisplayMpx("a", {"DISPLAY": ":9"}, run=run)
    m.create()
    assert "remove-master" not in [c[0] for c in run.calls]
    m.close()


def test_unavailable_reasons():
    assert "x11 backend" in unavailable_reason("wayland")
    assert "xinput" in unavailable_reason("x11", which=lambda n: None)


# -- the backend -------------------------------------------------------------------------------------


def test_click_warps_then_presses(mpx):
    Backend(mpx).click(30, 40, 3, 2)
    assert mpx.calls == [("warp", 30, 40), ("button", 3, True), ("button", 3, False),
                         ("button", 3, True), ("button", 3, False)]


def test_scroll_uses_wheel_buttons(mpx):
    Backend(mpx).scroll(5, 6, 1, -2)
    presses = [c[1] for c in mpx.calls if c[0] == "button" and c[2]]
    assert presses == [4, 4, 7]


def test_drag_releases_even_when_a_move_fails(mpx):
    def broken(x, y):
        if (x, y) != (0, 0):
            raise RuntimeError("x gone")
        mpx.calls.append(("warp", x, y))

    mpx.warp = broken
    with pytest.raises(RuntimeError):
        Backend(mpx).drag(0, 0, 100, 100, 100)
    assert mpx.calls[-1] == ("button", 1, False)


def test_type_and_keys_go_to_the_agent_keyboard(mpx):
    b = Backend(mpx)
    b.type_text("hi\nthere")
    b.press_key(parse_key_combo("ctrl+l"))
    assert mpx.calls == [("type_text", "hi\nthere"), ("press_key", "ctrl+l")]


def test_activate_sets_agent_focus_and_leaves_the_window_manager_alone(mpx):
    b = Backend(mpx)
    b.activate_window(win("0x00400010"))
    assert mpx.calls == [("focus", 0x400010)]
    assert b.take_note() is None
    assert b.active_window() == "0x00400010"


def test_activate_a_hidden_window_uses_the_normal_path_and_says_so(mpx, monkeypatch):
    b = Backend(mpx, shown=False)
    normal = []
    monkeypatch.setattr("ccdesk.backends.x11.X11Backend.activate_window", lambda self, w: normal.append(w.id))
    b.activate_window(win("0x00400010"))
    assert normal == ["0x00400010"]
    assert mpx.calls == [("focus", 0x400010)]
    assert "not on screen" in b.take_note()
    assert b.take_note() is None


def test_activate_a_window_on_another_desktop_uses_the_normal_path(mpx, monkeypatch):
    b = Backend(mpx, desktop=0)
    normal = []
    monkeypatch.setattr("ccdesk.backends.x11.X11Backend.activate_window", lambda self, w: normal.append(w.id))
    b.activate_window(win("0x00400010", desktop=1))
    assert normal == ["0x00400010"]


def test_no_focus_means_no_active_window(mpx):
    assert Backend(mpx).active_window() is None


def test_window_at_picks_the_deepest_listed_window_under_the_agent_pointer(mpx):
    b = Backend(mpx, windows=[win("0x00400010")])
    mpx.under = [0x400001, 0x400010, 0x400011]  # a frame, the client, a toolkit child
    assert b.window_at(50, 60) == "0x00400010"
    assert mpx.calls == [("warp", 50, 60)]
    mpx.under = [0x500001, 0x500002]  # nothing listed: the top-level one
    assert b.window_at(1, 1) == "0x00500001"
    mpx.under = []
    assert b.window_at(1, 1) is None


# -- Desktop ----------------------------------------------------------------------------------------------


def desk(mpx, tmp_path, windows, **kw):
    backend = Backend(mpx, windows=windows)
    d = Desktop(backend=backend, policy=Policy(), audit=AuditLog(tmp_path / "a.log", agent="a"), agent="a",
                shots_dir=tmp_path / "s", screen_diff=False, **kw)
    d.clock = lambda: 0.0
    return d, backend


def test_seat_is_per_agent_for_an_own_pointer(mpx, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    d, backend = desk(mpx, tmp_path, [win("0x00400010")])
    assert d.seat.key == seat_key("x11", backend.env, "a") == "_9-agent-a"
    plain = Desktop(backend=Backend(mpx), agent=None, audit=AuditLog(tmp_path / "b.log"), shots_dir=tmp_path / "s")
    assert plain.seat.key == "_9"


def test_seat_key_without_an_agent_is_the_screen():
    assert seat_key("x11", {"DISPLAY": ":1"}) == "_1"
    assert seat_key("x11", {"DISPLAY": ":1"}, "bot/1") == "_1-agent-bot_1"


def test_two_own_pointer_agents_do_not_share_a_seat(mpx, tmp_path):
    a = SeatLock(seat_key("x11", {"DISPLAY": ":1"}, "a"), directory=tmp_path, timeout=0.2)
    b = SeatLock(seat_key("x11", {"DISPLAY": ":1"}, "b"), directory=tmp_path, timeout=0.2)
    with a:
        with b:  # would raise if they shared the file
            pass


def test_activate_window_reports_the_note(mpx, tmp_path, monkeypatch):
    d, backend = desk(mpx, tmp_path, [win("0x00400010")])
    backend.shown = False
    monkeypatch.setattr("ccdesk.backends.x11.X11Backend.activate_window", lambda self, w: None)
    result = d.activate_window("0x00400010")
    assert "not on screen" in result["note"]


def test_guard_checks_the_agent_focus_not_the_users(mpx, tmp_path):
    windows = [win("0x00400010", "mine"), win("0x00500010", "other")]
    d, backend = desk(mpx, tmp_path, windows)
    d.activate_window("0x00400010")
    assert d.type_text("hi")["window"]["id"] == "0x00400010"
    assert ("type_text", "hi") in mpx.calls
    mpx.focus_window = 0x500010  # the agent's own focus moved elsewhere: a named agent takes its window back
    assert d.type_text("again")["window"]["id"] == "0x00400010"
    assert mpx.focus_window == 0x400010
    d.agent = None  # without a name it refuses instead
    mpx.focus_window = 0x500010
    with pytest.raises(PolicyError, match="keyboard focus is on 'other'"):
        d.type_text("again")


def test_click_checks_the_window_under_the_agent_pointer(mpx, tmp_path):
    windows = [win("0x00400010", "mine"), win("0x00500010", "other")]
    d, backend = desk(mpx, tmp_path, windows)
    d.activate_window("0x00400010")
    mpx.under = [0x400010]
    assert d.click(10, 10)["window"]["id"] == "0x00400010"
    mpx.under = [0x500010]
    with pytest.raises(PolicyError, match="the pointer at 20,20 is over 'other'"):
        d.click(20, 20)


# -- doctor and the CLI ---------------------------------------------------------------------------------------


def test_doctor_reports_the_devices(mpx):
    report = build_report(Backend(mpx), Policy(), "/tmp/audit.log")
    assert report["own_pointer"]["master_pointer"] == 8
    assert "master_pointer=8" in format_report(report)


def test_doctor_says_why_it_is_unavailable():
    from tests.conftest import FakeBackend

    report = build_report(FakeBackend(), Policy(), "/tmp/audit.log")
    assert report["own_pointer"]["enabled"] is False
    assert "x11 backend" in report["own_pointer"]["reason"]
    assert "unavailable" in format_report(report)


def test_cli_own_pointer_needs_an_agent_name(capsys):
    assert cli.main(["mcp", "--own-pointer"]) == 1
    assert "--own-pointer needs --agent" in capsys.readouterr().err


def test_cli_own_pointer_refuses_other_backends(capsys):
    assert cli.main(["mcp", "--backend", "wayland", "--agent", "a", "--own-pointer"]) == 1
    assert "x11 backend" in capsys.readouterr().err
