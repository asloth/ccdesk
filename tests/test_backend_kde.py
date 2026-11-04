"""KDE Plasma backend: command construction and KWin output parsing, with a recording runner.

Nothing here touches the session: kdotool answers with canned JSON and every command is only read
back from the recorder.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from ccdesk.backends import KdeBackend, X11Backend, get_backend, is_kde
from ccdesk.backends.kde import (
    KWIN_CHECK,
    WINDOWS_SCRIPT,
    kwin_on_show,
    kwin_window_at,
    parse_kwin_screens,
    parse_kwin_windows,
    ydotoold_socket,
)
from ccdesk.backends.keycodes import keycode, ydotool_key_sequence
from ccdesk.desktop import Desktop
from ccdesk.doctor import build_report, format_report
from ccdesk.errors import CommandError, PolicyError, UnsupportedError
from ccdesk.parsing import Region, Window, parse_key_combo, parse_window_ref
from ccdesk.safety import AuditLog, Policy

KDE_ENV = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0", "XDG_SESSION_TYPE": "wayland",
           "XDG_CURRENT_DESKTOP": "KDE"}
KDE_ALL = {"spectacle", "ydotool", "kdotool", "wl-copy", "wl-paste", "convert"}
UUID = "{c14f3dda-5bd1-4812-9fc6-4469857b8241}"
WINDOW = Window(id=UUID, title="Terminal", wm_class="kitty", x=0, y=0, width=800, height=600, desktop=1)


class Recorder:
    def __init__(self, responses=None, fail=()):
        self.calls = []
        self.responses = responses or {}
        self.fail = fail

    def __call__(self, argv, input=None, timeout=None, detach=False):
        self.calls.append((list(argv), input, detach))
        if argv[0] == "spectacle" and "--output" in argv:  # the real tool writes the file
            with open(argv[argv.index("--output") + 1], "wb") as fh:
                fh.write(b"\x89PNG\r\n\x1a\n" + b"0" * 25)  # header plus enough for png_size
        if any(" ".join(argv).startswith(f) for f in self.fail):
            raise CommandError(f"{' '.join(argv[:2])} failed")
        return self.responses.get(" ".join(argv[:2]), "")

    def argvs(self):
        return [c[0] for c in self.calls]


def make(installed=KDE_ALL, responses=None, fail=(), env=None):
    r = Recorder(responses, fail)
    b = KdeBackend(env=env or KDE_ENV, runner=r,
                   which=lambda n: f"/usr/bin/{n}" if n in installed else None)
    b.sleep = lambda s: None
    return b, r


def win(**kw):
    raw = {"id": UUID, "title": "Terminal", "cls": "kitty", "pid": 10, "x": 0, "y": 0,
           "width": 800, "height": 600, "minimized": False, "active": False, "desktop": 1}
    return {**raw, **kw}


class PointerRecorder(Recorder):
    """A runner that keeps a pointer position, so move() has something to correct against."""

    def __init__(self, start=(0, 0), gain=1.0, stuck=False):
        super().__init__()
        self.pos = list(start)
        self.gain = gain
        self.stuck = stuck

    def __call__(self, argv, input=None, timeout=None, detach=False):
        self.calls.append((list(argv), input, detach))
        if argv[:2] == ["kdotool", "getmouselocation"]:
            return f"x:{self.pos[0]} y:{self.pos[1]} screen:0 window:{{}}"
        if argv[:2] == ["ydotool", "mousemove"] and "--absolute" not in argv and "--wheel" not in argv:
            if not self.stuck:
                self.pos[0] += round(int(argv[argv.index("-x") + 1]) * self.gain)
                self.pos[1] += round(int(argv[argv.index("-y") + 1]) * self.gain)
        return ""


# -- detection -----------------------------------------------------------


@pytest.mark.parametrize("desktop,expected", [
    ("KDE", True), ("plasma", True), ("KDE:GNOME", True), ("ubuntu:GNOME", False), ("", False),
    (None, False),
])
def test_is_kde(desktop, expected):
    env = {} if desktop is None else {"XDG_CURRENT_DESKTOP": desktop}
    assert is_kde(env) is expected


def test_plasma_on_wayland_gets_its_own_backend():
    assert isinstance(get_backend(env={"WAYLAND_DISPLAY": "w", "XDG_CURRENT_DESKTOP": "KDE"}), KdeBackend)
    assert not isinstance(get_backend(env={"WAYLAND_DISPLAY": "w", "XDG_CURRENT_DESKTOP": "sway"}), KdeBackend)


def test_plasma_on_x11_keeps_xdotool():
    assert isinstance(get_backend(env={"DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "KDE"}), X11Backend)


def test_backend_needs_a_wayland_session():
    b, _ = make(env={"XDG_CURRENT_DESKTOP": "KDE"})
    assert "WAYLAND_DISPLAY" in b.available()


# -- parsing -------------------------------------------------------------


def test_parse_kwin_windows():
    windows = parse_kwin_windows([win(), win(id="{0}", desktop=None, minimized=True, title=None)])
    assert windows[0] == WINDOW
    assert windows[1].title == "" and windows[1].desktop is None


def test_parse_kwin_windows_refuses_junk():
    with pytest.raises(CommandError, match="unexpected window"):
        parse_kwin_windows([{"title": "no id"}])


def test_parse_kwin_screens():
    payload = [{"name": "DP-3", "x": 1920, "y": 394, "width": 1920, "height": 1080, "scale": 1.5},
               {"name": "HDMI-A-1", "x": 0, "y": 0, "width": 1920, "height": 1080}]
    assert parse_kwin_screens(payload) == [
        {"name": "DP-3", "x": 1920, "y": 394, "width": 1920, "height": 1080, "scale": 1.5},
        {"name": "HDMI-A-1", "x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1.0},
    ]


def test_window_at_takes_the_topmost_window():
    bottom, top = win(id="{0}", title="under"), win(id="{1}", title="over")
    # stackingOrder runs bottom to top, so the last match is the one that gets the click
    assert kwin_window_at([bottom, top], 10, 10) == "{1}"


def test_window_at_skips_minimized_and_other_desktops():
    minimized = win(id="{0}", minimized=True)
    elsewhere = win(id="{1}", desktop=2)
    assert kwin_window_at([minimized, elsewhere], 10, 10, current_desktop=1) is None
    assert kwin_window_at([elsewhere], 10, 10, current_desktop=2) == "{1}"
    # a window on every desktop has desktop=None and is always a candidate
    assert kwin_window_at([win(id="{2}", desktop=None)], 10, 10, current_desktop=3) == "{2}"


def test_window_at_outside_the_frame():
    assert kwin_window_at([win()], 800, 600) is None
    assert kwin_window_at([win()], 799, 599) == UUID


# -- pixels --------------------------------------------------------------


def test_screenshot_uses_spectacle(tmp_path):
    b, r = make()
    b.screenshot(str(tmp_path / "s.png"), None)
    assert [a for a in r.argvs() if a[0] != "kdotool"] == [["spectacle", "--fullscreen", "--background", "--nonotify",
                          "--output", str(tmp_path / "s.png")]]


def test_region_screenshot_crops_the_grab(tmp_path):
    b, r = make()
    b.screenshot(str(tmp_path / "s.png"), Region(1900, 380, 400, 300))
    argvs = [a for a in r.argvs() if a[0] != "kdotool"]  # kdotool is asked for the logical size
    assert argvs[0][:2] == ["spectacle", "--fullscreen"]
    crop = argvs[1]
    assert crop[0] == "convert" and crop[2:6] == ["-crop", "400x300+1900+380", "+repage", f"PNG24:{tmp_path / 's.png'}"]


def test_region_screenshot_needs_imagemagick(tmp_path):
    b, _ = make(installed=KDE_ALL - {"convert"})
    with pytest.raises(UnsupportedError, match="ImageMagick"):
        b.screenshot(str(tmp_path / "s.png"), Region(0, 0, 10, 10))


def test_screenshot_needs_spectacle(tmp_path):
    b, _ = make(installed={"kdotool"})
    with pytest.raises(CommandError, match="spectacle"):
        b.screenshot(str(tmp_path / "s.png"), None)


def test_spectacle_that_writes_nothing_is_an_error(tmp_path):
    b, r = make()
    r.responses["spectacle --fullscreen"] = ""  # and the recorder writes no file for this one
    b.run = lambda argv, **kw: ""  # a runner that does not touch the disk
    with pytest.raises(CommandError, match="wrote no screenshot"):
        b.screenshot(str(tmp_path / "s.png"), None)


def test_monitors_and_screen_size_come_from_kwin():
    b, r = make(responses={"kdotool kwinscript": json.dumps(
        [{"name": "DP-3", "x": 1920, "y": 394, "width": 1920, "height": 1080, "scale": 1},
         {"name": "HDMI-A-1", "x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1}])})
    assert [m["name"] for m in b.monitors()] == ["DP-3", "HDMI-A-1"]
    assert b.screen_size() == (3840, 1474)
    assert r.argvs()[0][:2] == ["kdotool", "kwinscript"]  # one call, not one per monitor


def test_monitors_are_empty_without_kdotool():
    b, r = make(installed={"spectacle"})
    assert b.monitors() == []  # no tool to ask: no call, no crash
    assert not any(argv[0] == "kdotool" for argv in r.argvs())


# -- windows -------------------------------------------------------------


def test_list_windows():
    b, _ = make(responses={"kdotool kwinscript": json.dumps([win(), win(id="{0}", title="Steam")])})
    assert [w.to_dict() for w in b.list_windows()] == [
        {**WINDOW.to_dict()}, {**WINDOW.to_dict(), "id": "{0}", "title": "Steam"}]


def test_kdotool_without_json_is_an_error():
    b, _ = make(responses={"kdotool kwinscript": "ERROR: nope"})
    with pytest.raises(CommandError, match="did not answer with JSON"):
        b.list_windows()


def test_window_actions():
    b, r = make()
    b.activate_window(WINDOW)
    b.move_window(WINDOW, 10, 20)
    b.resize_window(WINDOW, 900, 700)
    b.minimize_window(WINDOW)
    b.maximize_window(WINDOW)
    b.restore_window(WINDOW)
    assert r.argvs() == [
        ["kdotool", "windowactivate", UUID],
        ["kdotool", "windowmove", UUID, "10", "20"],
        ["kdotool", "windowsize", UUID, "900", "700"],
        ["kdotool", "windowminimize", UUID],
        ["kdotool", "windowstate", UUID, "--add", "maximized"],
        ["kdotool", "windowstate", UUID, "--remove", "maximized"],
        ["kdotool", "windowstate", UUID, "--remove", "fullscreen"],
        ["kdotool", "windowstate", UUID, "--remove", "minimized"],
        ["kdotool", "windowactivate", UUID],
    ]


@pytest.mark.parametrize("bad", ["--help", "0x1234", "{not-a-uuid}", "x" * 5])
def test_window_actions_refuse_an_id_that_is_not_one(bad):
    b, r = make()
    with pytest.raises(CommandError, match="not a KWin window id"):
        b.activate_window(Window(id=bad, title="t", wm_class="c", x=0, y=0, width=1, height=1))
    assert r.argvs() == []


def test_active_window_and_window_on_show():
    b, _ = make(responses={"kdotool kwinscript": json.dumps([win(), win(id="{1}", active=True)]),
                           "kdotool get_desktop": "1"})
    assert b.active_window() == "{1}"
    assert b.window_on_show("{1}") is True
    assert b.current_desktop() == 1


def test_window_on_show_says_no_when_minimized_or_elsewhere():
    b, _ = make(responses={"kdotool kwinscript": json.dumps(
        [win(id="{0}", minimized=True), win(id="{1}", desktop=2)]), "kdotool get_desktop": "1"})
    assert b.window_on_show("{0}") is False
    assert b.window_on_show("{1}") is False
    assert b.window_on_show("{missing}") is None


def test_current_desktop_survives_a_broken_kdotool():
    b, _ = make(responses={"kdotool get_desktop": "not a number"})
    assert b.current_desktop() is None
    b, _ = make(installed={"spectacle"})
    assert b.current_desktop() is None


def test_window_at_uses_the_live_stack():
    b, _ = make(responses={"kdotool kwinscript": json.dumps(
        [win(id="{0}", title="under"), win(id="{1}", title="over")]), "kdotool get_desktop": "1"})
    assert b.window_at(10, 10) == "{1}"


def test_focus_checks_are_unknown_without_kdotool():
    b, _ = make(installed={"spectacle", "ydotool"})
    assert b.active_window() is None and b.window_at(1, 1) is None and b.window_on_show(UUID) is None


# -- input ---------------------------------------------------------------


def test_typing_ignores_wtype_because_plasma_has_no_virtual_keyboard():
    b, r = make(installed=KDE_ALL | {"wtype"})
    b.type_text("hola")
    assert r.calls[0][0] == ["ydotool", "type", "--key-delay", "12", "--file", "-"]
    assert r.calls[0][1] == "hola"  # through stdin, not the process list


def test_pointer_goes_through_ydotool():
    r = PointerRecorder(start=(0, 0))
    pointer_backend(r).click(10, 20, 1, 1)
    ydotool = [a for a in r.argvs() if a[0] == "ydotool"]
    assert ydotool[-1] == ["ydotool", "click", "--repeat", "1", "--next-delay", "80", "0xc0"]
    assert all("--absolute" not in a for a in ydotool)  # relative steps, corrected against KWin
    assert r.pos == [10, 20]


def pointer_backend(recorder):
    b = KdeBackend(env=KDE_ENV, runner=recorder,
                   which=lambda n: f"/usr/bin/{n}" if n in KDE_ALL else None)
    b.sleep = lambda s: None
    return b


def test_move_corrects_for_pointer_acceleration():
    # ydotool's absolute mode lands in a corner under KWin, so move() goes relatively and
    # corrects: here every requested step comes out twice as large.
    r = PointerRecorder(start=(100, 100), gain=2.0)
    b = pointer_backend(r)
    b.move(500, 400)
    assert abs(r.pos[0] - 500) <= 2 and abs(r.pos[1] - 400) <= 2
    moves = [a for a in r.argvs() if a[0] == "ydotool"]
    assert moves and all("--absolute" not in a for a in moves)


def test_move_without_kdotool_refuses_instead_of_clicking_blind():
    # ydotool --absolute lands in a corner under KWin, and without kdotool there is no position to
    # correct against: refusing beats a click somewhere else.
    b, r = make(installed=KDE_ALL - {"kdotool"})
    with pytest.raises(CommandError, match="needs kdotool"):
        b.move(10, 20)
    assert not [a for a in r.argvs() if a[0] == "ydotool"]


def test_move_refuses_when_kdotool_cannot_report_the_position():
    r = Recorder()  # kdotool is installed but every call answers nothing
    b = pointer_backend(r)
    with pytest.raises(CommandError, match="could not report the pointer position"):
        b.move(10, 20)
    assert not [a for a in r.argvs() if a[0] == "ydotool"]


class SlowPointer(PointerRecorder):
    """Moves one pixel per step: eight tries cannot reach a far target."""

    def __call__(self, argv, input=None, timeout=None, detach=False):
        if argv[0] == "ydotool" and argv[1] == "mousemove" and "--wheel" not in argv:
            self.calls.append((list(argv), input, detach))
            dx, dy = int(argv[argv.index("-x") + 1]), int(argv[argv.index("-y") + 1])
            self.pos[0] += (dx > 0) - (dx < 0)
            self.pos[1] += (dy > 0) - (dy < 0)
            return ""
        return super().__call__(argv, input, timeout, detach)


def test_a_miss_beyond_the_limit_is_refused_not_clicked():
    b = pointer_backend(SlowPointer(start=(0, 0)))
    with pytest.raises(CommandError, match="could not place the pointer"):
        b.move(200, 200)


def test_move_refuses_a_point_it_cannot_reach():
    b = pointer_backend(PointerRecorder(start=(0, 0), stuck=True))
    with pytest.raises(CommandError, match="could not place the pointer"):
        b.move(500, 400)


def test_a_pointer_that_already_sits_there_is_not_moved():
    r = PointerRecorder(start=(500, 400))
    pointer_backend(r).move(500, 400)
    assert not [a for a in r.argvs() if a[0] == "ydotool"]


def test_pointer_step_and_gain():
    step, gain_ = KdeBackend._pointer_step, KdeBackend._pointer_gain
    assert step(0, 2) == 0 and step(100, 2) == 50 and step(-100, 2) == -50
    assert step(3, 1.0) == 3 and step(1, 4.0) == 1  # never rounds a step away
    assert gain_(10, 20, 1.6) == 2.0     # learned from what the step did
    assert gain_(0, 5, 1.6) == 1.6       # nothing to learn from
    assert gain_(10, 0, 1.6) == 1.6      # the pointer did not move: the edge barrier
    assert gain_(1, 100, 1.6) == 4.0     # clamped
    assert gain_(100, 1, 1.6) == 1.0     # clamped the other way


def test_drag_moves_relatively_and_releases():
    r = PointerRecorder(start=(0, 0), gain=1.0)
    b = pointer_backend(r)
    b.drag(100, 100, 300, 260, 48)
    buttons = [a[0][2] for a in r.calls if a[0][0] == "ydotool" and a[0][1] == "click"]
    assert buttons == ["0x40", "0x80"]  # pressed once, released once
    assert abs(r.pos[0] - 300) <= 2 and abs(r.pos[1] - 260) <= 2


def test_a_drag_that_cannot_reach_its_end_point_is_refused_and_released():
    r = SlowPointer(start=(0, 0))
    b = pointer_backend(r)
    with pytest.raises(CommandError, match="the drag ended away"):
        b.drag(0, 0, 200, 200, 48)
    buttons = [a[0][2] for a in r.calls if a[0][0] == "ydotool" and a[0][1] == "click"]
    assert buttons == ["0x40", "0x80"]  # the button is released on the way out


@pytest.mark.parametrize("combo,expected", [
    ("ctrl+shift+t", ["29:1", "42:1", "20:1", "20:0", "42:0", "29:0"]),
    ("Return", ["28:1", "28:0"]),
    ("ctrl+alt+Delete", ["29:1", "56:1", "111:1", "111:0", "56:0", "29:0"]),
    ("plus", ["42:1", "13:1", "13:0", "42:0"]),  # a shifted key brings its own shift
    ("A", ["42:1", "30:1", "30:0", "42:0"]),
    ("?", ["42:1", "53:1", "53:0", "42:0"]),
    ("super", ["125:1", "125:0"]),  # a bare modifier
])
def test_ydotool_key_sequence(combo, expected):
    assert ydotool_key_sequence(parse_key_combo(combo)) == expected


def test_press_key_uses_ydotool():
    b, r = make()
    b.press_key(parse_key_combo("ctrl+l"))
    assert r.argvs() == [["ydotool", "key", "--key-delay", "12", "29:1", "38:1", "38:0", "29:0"]]


def test_a_key_ydotool_has_no_code_for_says_so():
    b, _ = make()
    with pytest.raises(UnsupportedError, match="no keycode"):
        b.press_key(parse_key_combo("Hyper"))


def test_ydotoold_socket(tmp_path):
    assert ydotoold_socket({"XDG_RUNTIME_DIR": str(tmp_path)}) is None
    (tmp_path / ".ydotool_socket").write_text("")
    assert ydotoold_socket({"XDG_RUNTIME_DIR": str(tmp_path)}) == str(tmp_path / ".ydotool_socket")


# -- doctor --------------------------------------------------------------


class StubA11y:
    def apps(self):
        return []


def test_doctor_explains_what_kde_needs():
    b, _ = make(installed={"spectacle"})  # no kdotool, no ydotool, no daemon
    report = build_report(b, Policy(), "/tmp/audit.log", a11y=StubA11y())
    text = format_report(report)
    assert "Backend: kde" in text
    assert "needs kdotool" in text
    assert "no tool for pointer" in text


def test_doctor_does_not_warn_about_a_working_setup(tmp_path):
    (tmp_path / ".ydotool_socket").write_text("")
    b, _ = make(env={**KDE_ENV, "XDG_RUNTIME_DIR": str(tmp_path)},
                responses={"kdotool kwinscript": json.dumps([win()]), "kdotool get_desktop": "1"})
    b.monitors = lambda: []
    b.kwin_missing = list  # the self-check finds every property (the fake answers one JSON to every script)
    report = build_report(b, Policy(), "/tmp/audit.log", a11y=StubA11y())
    text = format_report(report)
    assert "needs kdotool" not in text and "ydotoold is not running" not in text


def test_doctor_formats_monitors_without_a_primary_flag():
    b, _ = make(responses={"kdotool kwinscript": json.dumps([win()]),
                           "kdotool get_desktop": "1"})
    b.monitors = lambda: [{"name": "DP-3", "x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1}]
    text = format_report(build_report(b, Policy(), "/tmp/audit.log", a11y=StubA11y()))
    assert "monitor:             DP-3 1920x1080+0+0" in text


# -- fixes from review ---------------------------------------------------


def png(width, height):
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"0" * 8


def screens(*boxes):
    return json.dumps([{"name": f"m{i}", "x": x, "y": y, "width": w, "height": h, "scale": 1}
                       for i, (x, y, w, h) in enumerate(boxes)])


class Grabs(Recorder):
    """spectacle writes an image of this size; the resizer's size is read back from the command."""

    def __init__(self, size, responses=None):
        super().__init__(responses)
        self.size = size

    def __call__(self, argv, input=None, timeout=None, detach=False):
        out = super().__call__(argv, input, timeout, detach)
        if argv[0] == "spectacle":
            with open(argv[argv.index("--output") + 1], "wb") as fh:
                fh.write(png(*self.size))
        return out


def make_grabs(size, boxes, installed=KDE_ALL):
    r = Grabs(size, {"kdotool kwinscript": screens(*boxes)})
    return KdeBackend(env=KDE_ENV, runner=r, which=lambda n: f"/usr/bin/{n}" if n in installed else None), r


def test_kwin_ids_are_ids_not_titles():
    assert parse_window_ref(UUID) == ("id", UUID)


def desktop_for(b, tmp_path):
    return Desktop(backend=b, policy=Policy(), audit=AuditLog(tmp_path / "audit.log"), shots_dir=tmp_path)


def test_desktop_finds_a_window_by_its_kwin_id(tmp_path):
    b, _ = make(responses={"kdotool kwinscript": json.dumps([win()])})
    assert desktop_for(b, tmp_path).find_window(UUID).id == UUID


def test_a_grab_at_logical_size_is_left_alone(tmp_path):
    b, r = make_grabs((1920, 1080), [(0, 0, 1920, 1080)])
    b.screenshot(str(tmp_path / "s.png"), None)
    assert not [a for a in r.argvs() if a[0] == "convert"]


def test_a_scaled_grab_comes_down_to_logical_pixels(tmp_path):
    b, r = make_grabs((3840, 2160), [(0, 0, 1920, 1080)])
    b.screenshot(str(tmp_path / "s.png"), None)
    assert next(a for a in r.argvs() if a[0] == "convert")[2:4] == ["-resize", "1920x1080!"]


def test_a_scaled_region_is_resized_before_the_crop(tmp_path):
    b, r = make_grabs((3840, 2160), [(0, 0, 1920, 1080)])
    b.screenshot(str(tmp_path / "s.png"), Region(10, 20, 30, 40))
    ops = [a[2] for a in r.argvs() if a[0] == "convert"]
    assert ops == ["-resize", "-crop"]


def test_a_scaled_grab_without_imagemagick_refuses(tmp_path):
    b, _ = make_grabs((3840, 2160), [(0, 0, 1920, 1080)], KDE_ALL - {"convert"})
    with pytest.raises(UnsupportedError, match="ImageMagick"):
        b.screenshot(str(tmp_path / "s.png"), None)


def test_window_origin_is_the_content_box_of_the_matching_window():
    raw = [win(pid=5, title="a", cx=1, cy=2), win(pid=5, title="b", cx=3, cy=4), win(pid=6, cx=5, cy=6)]
    b, _ = make(responses={"kdotool kwinscript": json.dumps(raw)})
    assert b.window_origin(5, "b") == (3, 4)
    assert b.window_origin(7, None) is None


def test_ui_tree_warns_when_kde_cannot_place_the_window(tmp_path):
    b, _ = make(installed=KDE_ALL - {"kdotool"})
    desktop = desktop_for(b, tmp_path)
    desktop.a11y.tree = lambda *a: {"tree": "", "truncated": False}
    assert "relative to the window" in desktop.ui_tree()["note"]


def test_transient_for_follows_the_kwin_parent():
    raw = [win(id="{dialog}", transient_for=UUID), win()]
    b, _ = make(responses={"kdotool kwinscript": json.dumps(raw)})
    assert b.transient_for("{dialog}") == UUID
    assert b.transient_for(UUID) is None


def test_shell_surfaces_are_not_listed_but_still_take_clicks():
    raw = [win(), win(id="{panel}", shell=True, title="", cls="plasmashell"), win(id="{menu}", popup=True)]
    b, _ = make(responses={"kdotool kwinscript": json.dumps(raw), "kdotool get_desktop": "1"})
    assert [w.id for w in b.list_windows()] == [UUID]
    assert b.window_at(5, 5) == "{menu}"
    # the guard can name a panel, but an app's menu belongs to the focus and is not a shell surface
    assert b.shell_window("{panel}").title == "a Plasma plasmashell surface"
    assert b.shell_window("{menu}") is None and b.shell_window(UUID) is None


def test_menus_plasma_opens_are_shell_but_an_apps_menu_is_not():
    raw = [win(), win(id="{panel}", shell=True, cls="plasmashell"),
           win(id="{desktop-menu}", popup=True, cls="plasmashell"),             # right click on the desktop
           win(id="{tray-menu}", popup=True, cls="org.kde.foo", transient_for="{panel}"),
           win(id="{submenu}", popup=True, cls="", transient_for="{tray-menu}"),
           win(id="{app-menu}", popup=True, cls="kitty", transient_for=UUID)]
    b, _ = make(responses={"kdotool kwinscript": json.dumps(raw), "kdotool get_desktop": "1"})
    assert [w for w in ("{desktop-menu}", "{tray-menu}", "{submenu}", "{app-menu}") if b.shell_window(w)] == [
        "{desktop-menu}", "{tray-menu}", "{submenu}"]


def test_a_click_on_a_panel_is_refused_when_a_window_was_activated(tmp_path):
    """The panel is not in list_windows, but it is not a menu of the focused window either."""
    raw = [win(active=True), win(id="{panel}", shell=True, title="Panel", x=0, y=0, width=800, height=40)]
    b, _ = make(responses={"kdotool kwinscript": json.dumps(raw), "kdotool get_desktop": "1"})
    b.monitors = lambda: [{"name": "a", "x": 0, "y": 0, "width": 800, "height": 600, "scale": 1.0}]
    d = desktop_for(b, tmp_path)
    d.activate_window(UUID)
    with pytest.raises(PolicyError, match=r"over 'Panel' \(\{panel\}\)"):
        d.click(5, 5)


@pytest.mark.parametrize("raw,current,expected", [
    (win(desktops=[1, 3]), 3, True),
    (win(desktops=[1, 3]), 2, False),
    (win(desktops=[], desktop=None), 2, True),
    (win(hidden=True), 1, False),
    (win(on_activity=False), 1, False),
    (win(desktops=[None], desktop=None), 2, True),  # an entry KWin left out: unknown, not a crash
    (win(desktops=[None, 3]), 2, False),
])
def test_on_show_reads_every_desktop_and_the_activity(raw, current, expected):
    assert kwin_on_show(raw, current) is expected


@pytest.mark.parametrize("key,code", [
    ("-", 12), ("=", 13), ("[", 26), ("]", 27), (";", 39), ("'", 40), ("`", 41), ("\\", 43), (",", 51),
    (".", 52), ("/", 53), ("Next", 109), ("Pause", 119),
])
def test_unshifted_keys_have_codes(key, code):
    assert keycode(key) == (code, False)


@pytest.mark.parametrize("key,code", [("question", 53), ("underscore", 12), ("colon", 39)])
def test_named_shifted_keys_hold_shift(key, code):
    assert keycode(key) == (code, True)


@pytest.mark.parametrize("key", ["Ñ", "é", "KP_Nonsense"])
def test_keys_with_no_code_say_so(key):
    with pytest.raises(UnsupportedError, match="no keycode"):
        keycode(key)


def test_non_ascii_text_warns():
    b, _ = make()
    assert b.typing_warning("plain") is None
    assert "ASCII" in b.typing_warning("café")


def test_mixed_scales_drawn_at_one_factor_resize(tmp_path):
    b, r = make_grabs((7680, 2160), [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)])
    b.screenshot(str(tmp_path / "s.png"), None)
    assert next(a for a in r.argvs() if a[0] == "convert")[2:4] == ["-resize", "3840x1080!"]


@pytest.mark.parametrize("size", [(5760, 2160), (1920, 1080)])  # two factors; smaller than the layout
def test_grabs_one_resize_cannot_fix_are_refused(tmp_path, size):
    b, _ = make_grabs(size, [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)])
    with pytest.raises(CommandError, match="one scale"):
        b.screenshot(str(tmp_path / "s.png"), None)


def test_a_monitor_left_of_zero_is_cut_so_pixels_stay_screen_coordinates(tmp_path):
    b, r = make_grabs((3840, 1080), [(-1920, 0, 1920, 1080), (0, 0, 1920, 1080)])
    b.screenshot(str(tmp_path / "s.png"), None)
    assert [a[2:4] for a in r.argvs() if a[0] == "convert"] == [["-crop", "1920x1080+1920+0"]]


def test_doctor_says_when_kdotool_cannot_run_a_script():
    b, _ = make(fail=("kdotool kwinscript",), responses={"kdotool get_desktop": "1"})
    b.monitors = list
    report = build_report(b, Policy(), "/tmp/audit.log", a11y=StubA11y())
    assert any("could not run a KWin script" in w and "0.3 or newer" in w for w in report["warnings"])


def test_doctor_names_kwin_properties_that_are_missing():
    b, _ = make(responses={"kdotool kwinscript": json.dumps(["clientGeometry"]), "kdotool get_desktop": "1"})
    b.monitors = list
    report = build_report(b, Policy(), "/tmp/audit.log", a11y=StubA11y())
    assert any("lacks clientGeometry" in w for w in report["warnings"])


NODE = shutil.which("node")


def run_kwin(script, workspace):
    """Run a KWin script under node against a fake workspace, as kdotool would inside KWin."""
    js = f"var workspace={workspace};var r;function output_result(x){{r=x;}}{script};console.log(r);"
    return json.loads(subprocess.run([NODE, "-e", js], capture_output=True, text=True, check=True).stdout)


FAKE_WINDOW = ("{internalId:'{u}',caption:'T',resourceClass:'k',pid:1,minimized:false,hidden:false,"
               "frameGeometry:{x:10,y:20,width:100,height:50},desktops:[{x11DesktopNumber:2}],activities:[]")


@pytest.mark.skipif(not NODE, reason="needs node")
def test_windows_script_reads_kwin_as_documented():
    ws = f"{{currentActivity:'a',stackingOrder:[{FAKE_WINDOW},clientGeometry:{{x:10,y:44}},dock:true}}]}}"
    [w] = run_kwin(WINDOWS_SCRIPT, ws)
    assert (w["cx"], w["cy"], w["shell"], w["desktops"], w["on_activity"]) == (10, 44, True, [2], True)


@pytest.mark.skipif(not NODE, reason="needs node")
def test_windows_script_fails_to_known_sides_when_kwin_differs():
    # a type flag that is a method, a content box that is a method, an activity list but no current one
    ws = (f"{{stackingOrder:[{FAKE_WINDOW},clientGeometry:function(){{return {{x:1,y:2}};}},"
          "dock:function(){return true;},activities:['b']}]}")
    [w] = run_kwin(WINDOWS_SCRIPT, ws)
    assert (w["cx"], w["cy"], w["shell"], w["on_activity"]) == (1, 2, False, True)


@pytest.mark.skipif(not NODE, reason="needs node")
def test_kwin_check_lists_what_is_missing():
    assert run_kwin(KWIN_CHECK, f"{{currentActivity:'a',stackingOrder:[{FAKE_WINDOW}}}]}}") == [
        "desktopWindow", "dock", "notification", "criticalNotification", "onScreenDisplay", "splash",
        "tooltip", "popupWindow", "appletPopup", "clientGeometry"]


@pytest.mark.skipif(not NODE, reason="needs node")
def test_hidden_and_minimized_fail_to_on_show_and_popups_are_not_shell():
    # hidden and minimized as methods: the window still counts as on show, like the type flags
    ws = (f"{{currentActivity:'a',stackingOrder:[{FAKE_WINDOW},hidden:function(){{return true;}},"
          "minimized:function(){return true;},popupWindow:true},"
          f"{FAKE_WINDOW},appletPopup:true}}]}}")
    menu, launcher = run_kwin(WINDOWS_SCRIPT, ws)
    assert (menu["hidden"], menu["minimized"], menu["popup"], menu["shell"]) == (False, False, True, False)
    assert (launcher["popup"], launcher["shell"]) == (False, True)


@pytest.mark.skipif(not NODE, reason="needs node")
def test_kwin_check_names_a_missing_stacking_order():
    assert "workspace.stackingOrder" in run_kwin(KWIN_CHECK, "{currentActivity:'a'}")
    assert "workspace.stackingOrder" in run_kwin(KWIN_CHECK, f"{{currentActivity:'a',activeWindow:{FAKE_WINDOW}}}}}")
