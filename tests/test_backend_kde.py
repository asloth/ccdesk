"""KDE Plasma backend: command construction and KWin output parsing, with a recording runner.

Nothing here touches the session: kdotool answers with canned JSON and every command is only read
back from the recorder.
"""

from __future__ import annotations

import json

import pytest

from ccdesk.backends import KdeBackend, X11Backend, get_backend, is_kde
from ccdesk.backends.kde import kwin_window_at, parse_kwin_screens, parse_kwin_windows, ydotoold_socket
from ccdesk.backends.keycodes import ydotool_key_sequence
from ccdesk.doctor import build_report, format_report
from ccdesk.errors import CommandError, UnsupportedError
from ccdesk.parsing import Region, Window, parse_key_combo
from ccdesk.safety import Policy

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
    assert r.argvs() == [["spectacle", "--fullscreen", "--background", "--nonotify",
                          "--output", str(tmp_path / "s.png")]]


def test_region_screenshot_crops_the_grab(tmp_path):
    b, r = make()
    b.screenshot(str(tmp_path / "s.png"), Region(1900, 380, 400, 300))
    assert r.argvs()[0][:2] == ["spectacle", "--fullscreen"]
    crop = r.argvs()[1]
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
    b, r = make()
    b.click(10, 20, 1, 1)
    assert r.argvs()[0] == ["ydotool", "mousemove", "--absolute", "-x", "10", "-y", "20"]
    assert r.argvs()[1] == ["ydotool", "click", "--repeat", "1", "--next-delay", "80", "0xc0"]


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
    report = build_report(b, Policy(), "/tmp/audit.log", a11y=StubA11y())
    text = format_report(report)
    assert "needs kdotool" not in text and "ydotoold is not running" not in text


def test_doctor_formats_monitors_without_a_primary_flag():
    b, _ = make(responses={"kdotool kwinscript": json.dumps([win()]),
                           "kdotool get_desktop": "1"})
    b.monitors = lambda: [{"name": "DP-3", "x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1}]
    text = format_report(build_report(b, Policy(), "/tmp/audit.log", a11y=StubA11y()))
    assert "monitor:             DP-3 1920x1080+0+0" in text
