"""Backend command construction and session detection, with a recording runner (no display needed)."""

import os

import pytest

from ccdesk.backends import Backend, WaylandBackend, X11Backend, detect_session, get_backend
from ccdesk.backends.base import interpolate
from ccdesk.backends.wayland import (
    parse_hyprctl_clients,
    parse_hyprctl_monitors,
    parse_sway_outputs,
    parse_sway_tree,
    sway_window_place,
)
from ccdesk.errors import CommandError, UnsupportedError
from ccdesk.parsing import Region, Window, parse_key_combo


class Recorder:
    def __init__(self, responses=None, fail=()):
        self.calls = []
        self.responses = responses or {}
        self.fail = fail

    def __call__(self, argv, input=None, timeout=None, detach=False):
        self.calls.append((list(argv), input, detach))
        key = " ".join(argv[:2])
        if any(" ".join(argv).startswith(f) for f in self.fail):
            raise CommandError(f"{key} failed")
        return self.responses.get(key, "")


def make(cls, installed, env=None, **rec):
    r = Recorder(**rec)
    b = cls(env=env or {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}, runner=r,
            which=lambda n: f"/usr/bin/{n}" if n in installed else None)
    b.sleep = lambda s: None
    return b, r


X11_ALL = {"xdotool", "scrot", "import", "wmctrl", "xclip", "xrdb"}


@pytest.mark.parametrize("env,expected", [
    ({}, "none"),
    ({"DISPLAY": ":0"}, "x11"),
    ({"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}, "x11"),
    ({"WAYLAND_DISPLAY": "wayland-0"}, "wayland"),
    ({"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}, "wayland"),
    ({"XDG_SESSION_TYPE": "wayland"}, "wayland"),
    ({"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":99"}, "x11"),
    ({"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0", "CCDESK_BACKEND": "x11"}, "x11"),
    ({"DISPLAY": ":0", "CCDESK_BACKEND": "Wayland"}, "wayland"),
])
def test_detect_session(env, expected):
    assert detect_session(env) == expected


def test_get_backend_types():
    assert isinstance(get_backend(env={"DISPLAY": ":0"}), X11Backend)
    assert isinstance(get_backend(env={"WAYLAND_DISPLAY": "w"}), WaylandBackend)
    assert type(get_backend(env={})) is Backend
    assert isinstance(get_backend("x11", env={}), X11Backend)


def test_interpolate():
    assert interpolate(0, 0, 10, 20, 2) == [(5, 10), (10, 20)]
    assert interpolate(3, 3, 3, 3, 0) == [(3, 3)]


def test_x11_input_commands():
    b, r = make(X11Backend, X11_ALL)
    b.click(10, 20, 3, 2)
    b.move(5, 6)
    b.scroll(1, 2, dx=-2, dy=3)
    b.type_text("hi there")
    b.press_key(parse_key_combo("ctrl+shift+t"))
    argvs = [c[0] for c in r.calls]
    assert argvs == [
        ["xdotool", "mousemove", "10", "20", "click", "--repeat", "2", "--delay", "80", "3"],
        ["xdotool", "mousemove", "5", "6"],
        ["xdotool", "mousemove", "1", "2", "click", "--repeat", "3", "--delay", "30", "5",
         "click", "--repeat", "2", "--delay", "30", "6"],
        ["xdotool", "type", "--clearmodifiers", "--delay", "12", "--", "hi there"],
        ["xdotool", "key", "--clearmodifiers", "ctrl+shift+t"],
    ]


def test_x11_type_text_starting_with_dash_is_not_an_option():
    b, r = make(X11Backend, X11_ALL)
    b.type_text("--help")
    assert r.calls[0][0][-2:] == ["--", "--help"]


def test_x11_drag_releases_button_on_failure():
    b, r = make(X11Backend, X11_ALL, fail=("xdotool mousemove 50",))
    with pytest.raises(CommandError):
        b.drag(0, 0, 100, 0, 64)
    assert r.calls[0][0] == ["xdotool", "mousemove", "0", "0", "mousedown", "1"]
    assert r.calls[-1][0] == ["xdotool", "mouseup", "1"]


def test_x11_drag_path():
    b, r = make(X11Backend, X11_ALL)
    b.drag(0, 0, 100, 50, 64)
    moves = [c[0] for c in r.calls[1:-1]]
    assert moves[-1] == ["xdotool", "mousemove", "100", "50"]
    assert len(moves) == b.drag_steps(64) == 4


def test_x11_screenshot_prefers_scrot_and_falls_back():
    b, r = make(X11Backend, X11_ALL)
    b.screenshot("/tmp/a.png", Region(1, 2, 3, 4))
    assert r.calls[0][0] == ["scrot", "-o", "-z", "-a", "1,2,3,4", "/tmp/a.png"]

    b, r = make(X11Backend, X11_ALL, fail=("scrot",))
    b.screenshot("/tmp/a.png", Region(1, 2, 3, 4))
    assert r.calls[-1][0] == ["import", "-silent", "-window", "root", "-crop", "3x4+1+2", "+repage", "PNG24:/tmp/a.png"]

    b, r = make(X11Backend, {"xdotool"})
    with pytest.raises(CommandError, match="scrot, import"):
        b.screenshot("/tmp/a.png", None)


def test_x11_missing_xdotool():
    b, _ = make(X11Backend, {"scrot"})
    with pytest.raises(CommandError, match="xdotool"):
        b.click(1, 1, 1, 1)


def test_x11_list_windows_wmctrl():
    out = "0x01e00003  0 0 27 1920 1053 Navigator.firefox host Mozilla Firefox\n"
    b, r = make(X11Backend, X11_ALL, responses={"wmctrl -lGx": out})
    wins = b.list_windows()
    assert [w.title for w in wins] == ["Mozilla Firefox"]


def test_x11_list_windows_without_wm_falls_back_to_xdotool():
    responses = {
        "xdotool search": "4194306\n4194307\n",
        "xdotool getwindowname": "ccdesk-test\n",
        "xdotool getwindowgeometry": "WINDOW=4194306\nX=40\nY=50\nWIDTH=420\nHEIGHT=420\nSCREEN=0\n",
        "xdotool getwindowclassname": "Tk\n",
    }
    b, r = make(X11Backend, X11_ALL, responses=responses, fail=("wmctrl",))
    wins = b.list_windows()
    assert wins[0].to_dict()["geometry"] == {"x": 40, "y": 50, "width": 420, "height": 420}
    assert wins[0].id == "0x00400002" and wins[0].wm_class == "Tk"


def test_x11_window_class_prefers_xprop():
    responses = {
        "xdotool search": "4194306\n",
        "xdotool getwindowname": "ccdesk-test\n",
        "xprop -id": 'WM_CLASS(STRING) = "tk", "Tk"\n',
    }
    b, r = make(X11Backend, X11_ALL | {"xprop"}, responses=responses, fail=("wmctrl",))
    assert b.list_windows()[0].wm_class == "tk.Tk"
    assert not any(c[0][:2] == ["xdotool", "getwindowclassname"] for c in r.calls)


def test_x11_list_windows_uses_ewmh_client_list():
    # bspwm without wmctrl: xdotool search would also return monitor roots and polybar.
    responses = {
        "xprop -root": "_NET_CLIENT_LIST(WINDOW): window id # 0x2000016, 0x260000e\n",
        "xdotool getwindowname": "Mozilla Firefox\n",
        "xprop -id": 'WM_CLASS(STRING) = "Navigator", "firefox"\n_NET_WM_DESKTOP(CARDINAL) = 2\n',
    }
    b, r = make(X11Backend, (X11_ALL - {"wmctrl"}) | {"xprop"}, responses=responses)
    wins = b.list_windows()
    assert [w.id for w in wins] == ["0x02000016", "0x0260000e"]
    assert wins[0].wm_class == "Navigator.firefox" and wins[0].desktop == 2
    assert not any(c[0][:2] == ["xdotool", "search"] for c in r.calls)


def test_x11_screen_size_spans_all_monitors():
    # xdotool getdisplaygeometry reports one monitor on recent releases; the root window is the real screen.
    responses = {"xwininfo -root": "  Width: 3840\n  Height: 1080\n", "xdotool getdisplaygeometry": "1920 1080\n"}
    b, _ = make(X11Backend, X11_ALL | {"xwininfo"}, responses=responses)
    assert b.screen_size() == (3840, 1080)
    b, _ = make(X11Backend, X11_ALL, responses=responses)
    assert b.screen_size() == (1920, 1080)
    b, _ = make(X11Backend, X11_ALL | {"xwininfo"}, responses=responses, fail=("xwininfo",))
    assert b.screen_size() == (1920, 1080)


def test_scale_png_uses_imagemagick():
    b, r = make(X11Backend, X11_ALL | {"convert"})
    b.scale_png("/tmp/a.png", 960, 540)
    assert r.calls[-1][0] == ["convert", "/tmp/a.png", "-resize", "960x540!", "PNG24:/tmp/a.png"]
    b, r = make(X11Backend, X11_ALL | {"magick", "convert"})
    b.scale_png("/tmp/a.png", 960, 540)
    assert r.calls[-1][0][0] == "magick"
    with pytest.raises(UnsupportedError, match="ImageMagick"):
        make(X11Backend, X11_ALL)[0].scale_png("/tmp/a.png", 1, 1)


def test_x11_monitors():
    out = "Monitors: 2\n 0: +*HDMI-1 1920/480x1080/270+1920+0  HDMI-1\n 1: +eDP-1 1920/344x1080/193+0+0  eDP-1\n"
    b, _ = make(X11Backend, X11_ALL | {"xrandr"}, responses={"xrandr --listmonitors": out})
    assert b.monitors() == [
        {"name": "HDMI-1", "x": 1920, "y": 0, "width": 1920, "height": 1080, "primary": True},
        {"name": "eDP-1", "x": 0, "y": 0, "width": 1920, "height": 1080, "primary": False},
    ]
    assert make(X11Backend, X11_ALL)[0].monitors() == []


def test_x11_activate_fallbacks():
    w = Window(id="0x00400002", title="t", wm_class="", x=0, y=0, width=1, height=1)
    b, r = make(X11Backend, X11_ALL)
    b.activate_window(w)
    assert r.calls[0][0] == ["wmctrl", "-i", "-a", "0x00400002"]

    b, r = make(X11Backend, X11_ALL, fail=("wmctrl", "xdotool windowactivate"))
    b.activate_window(w)
    assert r.calls[-1][0] == ["xdotool", "windowraise", "4194306", "windowfocus", "4194306"]


def test_x11_focus_queries():
    responses = {
        "xdotool getactivewindow": "31457302\n",
        "xdotool mousemove": "X=78\nY=352\nSCREEN=0\nWINDOW=39845902\n",
        "xprop -id": "WM_TRANSIENT_FOR(WINDOW): window id # 0x1e00016\n",
    }
    b, r = make(X11Backend, X11_ALL | {"xprop"}, responses=responses)
    assert b.active_window() == "0x01e00016"
    assert b.window_at(78, 352) == "0x0260000e"
    assert r.calls[-1][0] == ["xdotool", "mousemove", "78", "352", "getmouselocation", "--shell"]
    assert b.transient_for("0x02a00003") == "0x01e00016"

    # No EWMH window manager: getactivewindow fails, and nothing is a dialog of anything.
    b, _ = make(X11Backend, X11_ALL | {"xprop"}, responses={"xprop -id": "WM_TRANSIENT_FOR:  not found.\n"},
                fail=("xdotool getactivewindow",))
    assert b.active_window() is None
    assert b.transient_for("0x02a00003") is None
    assert Backend(env={}).active_window() is None and Backend(env={}).window_at(1, 1) is None


def test_x11_current_desktop():
    b, _ = make(X11Backend, X11_ALL | {"xprop"}, responses={"xprop -root": "_NET_CURRENT_DESKTOP(CARDINAL) = 1\n"})
    assert b.current_desktop() == 1
    b, _ = make(X11Backend, X11_ALL | {"xprop"}, responses={"xprop -root": "_NET_CURRENT_DESKTOP:  not found.\n"})
    assert b.current_desktop() is None


def test_x11_idle_ms():
    b, r = make(X11Backend, X11_ALL | {"xprintidle"}, responses={"xprintidle": "4215\n"})
    assert b.idle_ms() == 4215 and r.calls[-1][0] == ["xprintidle"]
    assert make(X11Backend, X11_ALL)[0].idle_ms() is None  # not installed
    assert make(X11Backend, X11_ALL | {"xprintidle"}, fail=("xprintidle",))[0].idle_ms() is None


def test_x11_clipboard():
    b, r = make(X11Backend, X11_ALL, responses={"xclip -selection": "pasted"})
    assert b.clipboard_get() == "pasted"
    b.clipboard_set("hello")
    argv, inp, detach = r.calls[-1]
    assert argv == ["xclip", "-selection", "clipboard", "-i"] and inp == "hello" and detach is True

    b, r = make(X11Backend, {"xsel"})
    b.clipboard_set("x")
    assert r.calls[-1][0] == ["xsel", "--clipboard", "--input"]


def test_x11_scale_info():
    b, _ = make(X11Backend, X11_ALL, env={"DISPLAY": ":0", "GDK_SCALE": "2"},
                responses={"xrdb -query": "Xft.dpi:\t144\n"})
    assert b.scale_info() == {"GDK_SCALE": "2", "Xft.dpi": 144.0}


def test_x11_available():
    assert X11Backend(env={}).available()
    assert X11Backend(env={"DISPLAY": ":1"}).available() is None


WL_ALL = {"grim", "ydotool", "wtype", "wl-copy", "wl-paste"}


def test_wayland_input_commands():
    b, r = make(WaylandBackend, WL_ALL)
    b.click(10, 20, 3, 2)
    b.scroll(1, 2, 0, 3)
    b.type_text("hi")
    b.press_key(parse_key_combo("super+q"))
    argvs = [c[0] for c in r.calls]
    assert argvs == [
        ["ydotool", "mousemove", "--absolute", "-x", "10", "-y", "20"],
        ["ydotool", "click", "--repeat", "2", "--next-delay", "80", "0xc1"],
        ["ydotool", "mousemove", "--absolute", "-x", "1", "-y", "2"],
        ["ydotool", "mousemove", "--wheel", "-x", "0", "-y", "-3"],
        ["wtype", "-s", "60", "-d", "12", "--", "hi"],
        ["wtype", "-s", "60", "-M", "logo", "-k", "q", "-m", "logo"],
    ]


def test_wayland_drag_uses_press_and_release():
    b, r = make(WaylandBackend, WL_ALL)
    b.drag(0, 0, 10, 10, 32)
    argvs = [c[0] for c in r.calls]
    assert argvs[1] == ["ydotool", "click", "0x40"]
    assert argvs[-1] == ["ydotool", "click", "0x80"]


def test_wayland_screenshot_and_text_fallback():
    b, r = make(WaylandBackend, {"grim", "ydotool"})
    b.screenshot("/tmp/s.png", Region(1, 2, 3, 4))
    b.type_text("x")
    assert r.calls[0][0] == ["grim", "-s", "1", "-g", "1,2 3x4", "/tmp/s.png"]
    assert r.calls[1][0] == ["ydotool", "type", "--key-delay", "12", "--", "x"]
    with pytest.raises(UnsupportedError, match="wtype"):
        b.press_key(parse_key_combo("Return"))


def test_wayland_gnome_screenshot_cannot_crop():
    b, r = make(WaylandBackend, {"gnome-screenshot"})
    b.screenshot("/tmp/s.png", None)
    assert r.calls[0][0] == ["gnome-screenshot", "-f", "/tmp/s.png"]
    with pytest.raises(UnsupportedError, match="grim"):
        b.screenshot("/tmp/s.png", Region(0, 0, 1, 1))


def test_wayland_windows_unsupported_without_ipc():
    b, _ = make(WaylandBackend, WL_ALL, env={"WAYLAND_DISPLAY": "wayland-0"})
    with pytest.raises(UnsupportedError, match="window list"):
        b.list_windows()
    w = Window(id="1", title="", wm_class="", x=0, y=0, width=0, height=0)
    with pytest.raises(UnsupportedError):
        b.activate_window(w)


def test_wayland_xwayland_window_fallback():
    out = "0x01e00003  0 0 0 800 600 Navigator.firefox host Firefox\n"
    b, r = make(WaylandBackend, WL_ALL | {"xdotool", "wmctrl"}, responses={"wmctrl -lGx": out})
    assert [w.title for w in b.list_windows()] == ["Firefox"]


def test_wayland_sway_ipc():
    tree = {"nodes": [{"type": "output", "nodes": [{"type": "workspace", "nodes": [
        {"type": "con", "id": 7, "pid": 100, "name": "Firefox", "app_id": "firefox",
         "rect": {"x": 0, "y": 30, "width": 1000, "height": 700}, "nodes": []},
    ], "floating_nodes": [
        {"type": "floating_con", "id": 9, "pid": 101, "name": "XTerm", "app_id": None,
         "window_properties": {"class": "XTerm"}, "rect": {"x": 5, "y": 5, "width": 10, "height": 10}},
    ]}]}]}
    wins = parse_sway_tree(tree)
    assert [(w.id, w.title, w.wm_class) for w in wins] == [("7", "Firefox", "firefox"), ("9", "XTerm", "XTerm")]

    import json
    b, r = make(WaylandBackend, WL_ALL | {"swaymsg"}, env={"WAYLAND_DISPLAY": "w", "SWAYSOCK": "/s"},
                responses={"swaymsg -t": json.dumps(tree)})
    assert len(b.list_windows()) == 2
    b.activate_window(wins[0])
    assert r.calls[-1][0] == ["swaymsg", "[con_id=7]", "focus"]


def test_wayland_hyprland_ipc():
    clients = [{"address": "0xabc", "title": "Firefox", "class": "firefox", "at": [1, 2], "size": [3, 4],
                "workspace": {"id": 2}}]
    w = parse_hyprctl_clients(clients)[0]
    assert (w.id, w.x, w.y, w.width, w.height, w.desktop) == ("0xabc", 1, 2, 3, 4, 2)


def test_wayland_clipboard():
    b, r = make(WaylandBackend, WL_ALL, responses={"wl-paste --no-newline": "abc"})
    assert b.clipboard_get() == "abc"
    b.clipboard_set("x")
    assert r.calls[-1] == (["wl-copy"], "x", True)


def test_base_backend_reports_unsupported():
    b = Backend(env={})
    assert "no graphical session" in b.available()
    with pytest.raises(UnsupportedError):
        b.click(1, 1, 1, 1)


SWAY_ENV = {"WAYLAND_DISPLAY": "w", "SWAYSOCK": "/s"}


def test_sway_pointer_needs_no_ydotool():
    b, r = make(WaylandBackend, {"grim", "wtype", "swaymsg"}, env=SWAY_ENV)
    b.click(10, 20, 3, 2)
    b.scroll(1, 2, 0, -1)
    argvs = [c[0][1] for c in r.calls]
    assert argvs == [
        "seat - cursor set 10 20",
        "seat - cursor press button3", "seat - cursor release button3",
        "seat - cursor press button3", "seat - cursor release button3",
        "seat - cursor set 1 2",
        "seat - cursor press button4", "seat - cursor release button4",
    ]
    r.calls.clear()
    b.drag(0, 0, 4, 4, 0)
    assert r.calls[1][0][1] == "seat - cursor press button1" and r.calls[-1][0][1] == "seat - cursor release button1"


def test_sway_screen_size_and_window_origin():
    import json

    outputs = [{"active": True, "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080}},
               {"active": True, "rect": {"x": 1920, "y": 0, "width": 1280, "height": 1024}},
               {"active": False, "rect": {"x": 9000, "y": 0, "width": 1, "height": 1}}]
    tree = {"nodes": [{"nodes": [{"pid": 5, "name": "Signup", "rect": {"x": 300, "y": 200, "width": 564, "height": 669},
                                  "window_rect": {"x": 2, "y": 27, "width": 560, "height": 640}},
                                 {"pid": 6, "name": "Other", "rect": {"x": 0, "y": 0, "width": 1, "height": 1}}]}]}
    b, _ = make(WaylandBackend, {"swaymsg", "grim"}, env=SWAY_ENV,
                responses={"swaymsg -t": json.dumps(outputs)})
    assert b.screen_size() == (3200, 1080)
    b, _ = make(WaylandBackend, {"swaymsg"}, env=SWAY_ENV, responses={"swaymsg -t": json.dumps(tree)})
    assert b.window_origin(5, "Signup") == (302, 227)
    assert b.window_origin(7, "Nope") is None
    assert WaylandBackend(env={"WAYLAND_DISPLAY": "w"}, which=lambda n: None).window_origin(5, "x") is None


def test_tools_run_with_the_backend_env():
    out = X11Backend(env={"PATH": os.environ["PATH"], "DISPLAY": ":42"}).run(["sh", "-c", "echo $DISPLAY"])
    assert out.strip() == ":42"


WIN = Window(id="0x02000001", title="T", wm_class="c", x=0, y=0, width=10, height=10)
WIN_DEC = "33554433"


def test_x11_window_control_without_bspwm():
    b, r = make(X11Backend, X11_ALL)
    b.move_window(WIN, 5, 6)
    assert r.calls[-1][0] == ["xdotool", "windowmove", WIN_DEC, "5", "6"]
    b.resize_window(WIN, 7, 8)
    assert r.calls[-1][0] == ["xdotool", "windowsize", WIN_DEC, "7", "8"]
    b.minimize_window(WIN)
    assert r.calls[-1][0] == ["xdotool", "windowminimize", WIN_DEC]
    b.maximize_window(WIN)
    assert r.calls[-1][0] == ["xdotool", "windowstate", "--add", "MAXIMIZED_VERT", WIN_DEC,
                              "windowstate", "--add", "MAXIMIZED_HORZ", WIN_DEC]
    b.restore_window(WIN)
    assert r.calls[-2][0][:5] == ["xdotool", "windowmap", WIN_DEC, "windowstate", "--remove"]
    assert r.calls[-1][0] == ["wmctrl", "-i", "-a", WIN.id]


def test_x11_window_control_on_bspwm():
    b, r = make(X11Backend, X11_ALL | {"bspc"})
    b.move_window(WIN, 5, 6)
    assert [c[0] for c in r.calls] == [
        ["bspc", "query", "-N", "-n", WIN.id], ["bspc", "node", WIN.id, "-t", "floating"],
        ["xdotool", "windowmove", WIN_DEC, "5", "6"]]
    b.minimize_window(WIN)
    assert r.calls[-1][0] == ["bspc", "node", WIN.id, "-g", "hidden=on"]
    b.maximize_window(WIN)
    assert r.calls[-1][0] == ["bspc", "node", WIN.id, "-t", "fullscreen"]
    b.restore_window(WIN)
    assert [c[0] for c in r.calls if c[0][:2] == ["bspc", "node"]][-2:] == [
        ["bspc", "node", WIN.id, "-g", "hidden=off"], ["bspc", "node", WIN.id, "-t", "tiled"]]


def test_x11_bspc_answers_but_does_not_manage_this_window():
    b, r = make(X11Backend, X11_ALL | {"bspc"}, fail=("bspc query",))
    b.minimize_window(WIN)
    assert r.calls[-1][0] == ["xdotool", "windowminimize", WIN_DEC]
    assert not [c for c in r.calls if c[0][:2] == ["bspc", "node"]]


@pytest.mark.parametrize("state,expected", [("IsViewable", True), ("IsUnMapped", False), ("IsUnviewable", False)])
def test_x11_window_on_show(state, expected):
    b, _ = make(X11Backend, X11_ALL | {"xwininfo"}, responses={"xwininfo -id": f"  Map State: {state}\n"})
    assert b.window_on_show(WIN.id) is expected


def test_x11_window_on_show_unknown():
    b, _ = make(X11Backend, X11_ALL)
    assert b.window_on_show(WIN.id) is None


def test_sway_window_control():
    win = Window(id="7", title="T", wm_class="c", x=0, y=0, width=10, height=10)
    b, r = make(WaylandBackend, WL_ALL | {"swaymsg"}, env={"WAYLAND_DISPLAY": "w", "SWAYSOCK": "/s"})
    b.move_window(win, 5, 6)
    assert r.calls[-1][0] == ["swaymsg", "[con_id=7]", "floating enable, move absolute position 5 6"]
    b.resize_window(win, 7, 8)
    assert r.calls[-1][0] == ["swaymsg", "[con_id=7]", "floating enable, resize set 7 8"]
    b.minimize_window(win)
    assert r.calls[-1][0] == ["swaymsg", "[con_id=7]", "move scratchpad"]
    b.maximize_window(win)
    assert r.calls[-1][0] == ["swaymsg", "[con_id=7]", "fullscreen enable"]
    b.restore_window(win)
    assert r.calls[-1][0] == ["swaymsg", "[con_id=7]", "fullscreen disable, focus"]  # stays on its workspace


@pytest.mark.parametrize("call", [
    lambda b: b.move_window(WIN, 1, 2), lambda b: b.resize_window(WIN, 1, 2), lambda b: b.minimize_window(WIN),
    lambda b: b.maximize_window(WIN), lambda b: b.restore_window(WIN),
])
def test_other_wayland_compositors_refuse_window_control(call):
    b, r = make(WaylandBackend, WL_ALL, env={"WAYLAND_DISPLAY": "w"})
    with pytest.raises(UnsupportedError, match="only implemented on sway and Hyprland"):
        call(b)
    assert r.calls == []


def test_sway_monitors():
    outputs = [{"name": "DP-1", "active": True, "focused": True, "scale": 2.0,
                "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080}},
               {"name": "HDMI-1", "active": False, "rect": {"x": 0, "y": 0, "width": 0, "height": 0}}]
    assert parse_sway_outputs(outputs) == [
        {"name": "DP-1", "x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 2.0, "focused": True}]


def test_hyprland_monitors_are_logical_pixels():
    monitors = [{"name": "eDP-1", "x": 0, "y": 0, "width": 2880, "height": 1800, "scale": 2.0, "transform": 0,
                 "focused": True},
                {"name": "DP-2", "x": 1440, "y": 0, "width": 1920, "height": 1080, "scale": 1.0, "transform": 1}]
    assert [(m["width"], m["height"]) for m in parse_hyprctl_monitors(monitors)] == [(1440, 900), (1080, 1920)]
    import json
    b, r = make(WaylandBackend, WL_ALL | {"hyprctl"}, env={"WAYLAND_DISPLAY": "w", "HYPRLAND_INSTANCE_SIGNATURE": "s"},
                responses={"hyprctl -j": json.dumps(monitors)})
    assert b.monitors()[0]["name"] == "eDP-1"
    assert r.calls[-1][0] == ["hyprctl", "-j", "monitors"]


SWAY_TREE = {"id": 1, "type": "root", "nodes": [{"id": 2, "type": "output", "nodes": [
    {"id": 3, "type": "workspace", "name": "__i3_scratch", "nodes": [],
     "floating_nodes": [{"id": 7, "type": "floating_con", "visible": False}]},
    {"id": 4, "type": "workspace", "name": "1", "nodes": [{"id": 8, "type": "con", "visible": True}]},
    {"id": 5, "type": "workspace", "name": "2", "nodes": [{"id": 9, "type": "con", "visible": False}]},
]}]}
SWAY_ENV = {"WAYLAND_DISPLAY": "w", "SWAYSOCK": "/s"}
HYPR_ENV = {"WAYLAND_DISPLAY": "w", "HYPRLAND_INSTANCE_SIGNATURE": "s"}


def test_sway_window_place():
    assert sway_window_place(SWAY_TREE, "7") == (False, True)
    assert sway_window_place(SWAY_TREE, "8") == (True, False)
    assert sway_window_place(SWAY_TREE, "9") == (False, False)
    assert sway_window_place(SWAY_TREE, "99") is None


@pytest.mark.parametrize("wid,expected", [("7", False), ("8", True), ("9", False), ("99", None)])
def test_sway_window_on_show(wid, expected):
    import json
    b, _ = make(WaylandBackend, WL_ALL | {"swaymsg"}, env=SWAY_ENV, responses={"swaymsg -t": json.dumps(SWAY_TREE)})
    assert b.window_on_show(wid) is expected


def test_sway_restore_brings_a_window_back_from_the_scratchpad():
    import json
    b, r = make(WaylandBackend, WL_ALL | {"swaymsg"}, env=SWAY_ENV, responses={"swaymsg -t": json.dumps(SWAY_TREE)})
    b.restore_window(Window(id="7", title="T", wm_class="c", x=0, y=0, width=1, height=1))
    assert r.calls[-1][0] == ["swaymsg", "[con_id=7]", "scratchpad show, floating disable, fullscreen disable, focus"]


@pytest.mark.parametrize("address,expected", [("0xa", True), ("0xb", True), ("0xc", False), ("0xd", False), ("0xe", None),
                                             ("0xf", True)])
def test_hyprland_window_on_show(address, expected):
    import json
    clients = [{"address": "0xa", "workspace": {"id": 1}}, {"address": "0xb", "workspace": {"id": 2}},
               {"address": "0xc", "workspace": {"id": 3}}, {"address": "0xd", "workspace": {"id": -98}},
               {"address": "0xf", "workspace": {"id": -99}}]
    monitors = [{"activeWorkspace": {"id": 1}, "specialWorkspace": {"id": 0}},  # one on show per monitor
                {"activeWorkspace": {"id": 2}, "specialWorkspace": {"id": -99}}]
    b, _ = make(WaylandBackend, WL_ALL | {"hyprctl"}, env=HYPR_ENV,
                responses={"hyprctl clients": json.dumps(clients), "hyprctl -j": json.dumps(monitors)})
    assert b.window_on_show(address) is expected


HWIN = Window(id="0xa1", title="T", wm_class="c", x=0, y=0, width=10, height=10)


class HyprRecorder(Recorder):
    """hyprctl -j <what> needs the third word to tell the answers apart."""

    def __call__(self, argv, input=None, timeout=None, detach=False):
        if argv[:2] == ["hyprctl", "-j"]:
            self.calls.append((list(argv), input, detach))
            return self.responses[{"activewindow": "__active", "activeworkspace": "__workspace",
                                   "monitors": "__monitors"}[argv[2]]]
        return super().__call__(argv, input, timeout, detach)


def hypr_backend(clients=(), active="{}", workspace='{"id": 3}', monitors=()):
    import json
    r = HyprRecorder(responses={"hyprctl clients": json.dumps(list(clients)), "hyprctl dispatch": "ok\n",
                                "__active": active, "__workspace": workspace, "__monitors": json.dumps(list(monitors))})
    b = WaylandBackend(env=HYPR_ENV, runner=r, which=lambda n: f"/usr/bin/{n}" if n in WL_ALL | {"hyprctl"} else None)
    b.sleep = lambda s: None
    return b, r


def lua_calls(r):
    return [c[0][2] for c in r.calls if c[0][:2] == ["hyprctl", "dispatch"]]


def test_hyprland_focus_uses_lua():
    b, r = hypr_backend()
    b.activate_window(HWIN)
    assert lua_calls(r) == ['hl.dsp.focus({ window = "address:0xa1" })']


def test_hyprland_focus_falls_back_for_versions_before_lua():
    b, r = hypr_backend()
    calls = []

    def legacy(argv, **kw):  # as if hyprctl did not know Lua
        calls.append(argv)
        return "ok" if argv[2] == "focuswindow" else "invalid dispatcher"
    b._runner = legacy
    b.activate_window(HWIN)
    assert calls[-1] == ["hyprctl", "dispatch", "focuswindow", "address:0xa1"]


@pytest.mark.parametrize("act", ["minimize_window", "activate_window"])
def test_hyprland_refuses_an_address_that_is_not_one(act):
    b, r = hypr_backend()
    with pytest.raises(CommandError, match="not a Hyprland window address"):
        getattr(b, act)(Window(id='x" }) os.exit() --', title="T", wm_class="c", x=0, y=0, width=1, height=1))
    assert lua_calls(r) == []


def test_hyprland_escapes_a_key_for_lua():
    b, r = hypr_backend()
    b.press_key(parse_key_combo('"'))
    assert lua_calls(r) == ['hl.dsp.send_shortcut({ mods = "", key = "\\"" })']


def test_hyprland_a_failed_float_is_not_remembered():
    b, r = hypr_backend(clients=[{"address": "0xa1", "floating": False, "workspace": {"id": 3, "name": "3"}}])
    r.responses["hyprctl dispatch"] = "error: nope"
    with pytest.raises(CommandError):
        b.move_window(HWIN, 1, 2)
    assert b._floated == set()


def test_hyprland_a_reused_address_is_not_retiled():
    import json
    b, r = hypr_backend(clients=[{"address": "0xa1", "stableId": "1", "floating": False,
                                  "workspace": {"id": 3, "name": "3"}}])
    b.move_window(HWIN, 1, 2)
    r.responses["hyprctl clients"] = json.dumps([{"address": "0xa1", "stableId": "2", "floating": True,
                                                  "workspace": {"id": 3, "name": "3"}}])
    b.restore_window(HWIN)
    assert not [c for c in lua_calls(r) if "disable" in c]


def test_hyprland_restore_without_a_known_workspace_refuses():
    b, r = hypr_backend(clients=[{"address": "0xa1", "workspace": {"id": -98, "name": "special:ccdesk"}}],
                        workspace="{}")
    with pytest.raises(CommandError, match="which workspace"):
        b.restore_window(HWIN)


def test_hyprland_a_floating_window_belongs_to_its_apps_main_window():
    main = {"address": "0xa", "pid": 7, "workspace": {"id": 1}, "focusHistoryID": 1}
    dialog = {"address": "0xb", "pid": 7, "workspace": {"id": 1}, "floating": True, "focusHistoryID": 0}
    stranger = {"address": "0xc", "pid": 8, "workspace": {"id": 1}, "floating": True}
    b, _ = hypr_backend(clients=[main, dialog, stranger])
    assert b.transient_for("0xb") == "0xa"
    assert b.transient_for("0xa") is None
    assert b.transient_for("0xc") is None


def test_hyprland_window_at_skips_hidden_windows():
    hidden = {"address": "0xa", "at": [0, 0], "size": [100, 100], "workspace": {"id": 1}, "floating": True,
              "hidden": True}
    below = {"address": "0xb", "at": [0, 0], "size": [100, 100], "workspace": {"id": 1}}
    b, _ = hypr_backend(clients=[hidden, below], monitors=[{"activeWorkspace": {"id": 1}, "specialWorkspace": {"id": 0}}])
    assert b.window_at(5, 5) == "0xb"


class FakePointer:
    def __init__(self, env):
        self.events = []
        FakePointer.made.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.events.append("close")

    def move(self, x, y, w, h):
        self.events.append(("move", x, y, w, h))

    def button(self, button, down):
        self.events.append(("button", button, down))

    def scroll(self, dx, dy):
        self.events.append(("scroll", dx, dy))


def test_wayland_uses_a_virtual_pointer_and_grabs_the_layout_once(monkeypatch):
    import ccdesk.backends.wayland as wl
    FakePointer.made = []
    monkeypatch.setattr(wl, "VirtualPointer", FakePointer)
    b, r = make(WaylandBackend, WL_ALL)
    sizes = []
    b.screen_size = lambda: sizes.append(1) or (800, 600)
    b.click(1, 2, 1, 1)
    b.scroll(3, 4, 0, 1)
    b.drag(0, 0, 8, 8, 32)
    assert FakePointer.made[0].events == [("move", 1, 2, 800, 600), ("button", 1, True), ("button", 1, False), "close"]
    assert FakePointer.made[1].events == [("move", 3, 4, 800, 600), ("scroll", 0, 1), "close"]
    assert FakePointer.made[2].events[1] == ("button", 1, True) and FakePointer.made[2].events[-2] == ("button", 1, False)
    assert len(sizes) == 1
    assert not [c for c in r.calls if c[0][0] == "ydotool"]


def test_wayland_stops_trying_a_protocol_the_compositor_lacks(monkeypatch):
    import ccdesk.backends.wayland as wl
    from ccdesk.backends.vpointer import NoVirtualPointer
    tries = []

    def absent(env):
        tries.append(1)
        raise NoVirtualPointer("no")

    monkeypatch.setattr(wl, "VirtualPointer", absent)
    b, r = make(WaylandBackend, WL_ALL)
    b.click(1, 2, 1, 1)
    b.click(1, 2, 1, 1)
    assert len(tries) == 1
    assert r.calls[-1][0][0] == "ydotool"


def test_virtual_pointer_lets_go_on_close():
    from ccdesk.backends.vpointer import VirtualPointer
    p = VirtualPointer.__new__(VirtualPointer)
    sent = []
    p._held, p.pointer = set(), 5
    p._send = sent.append
    p._roundtrip = lambda: None
    p._sock = type("S", (), {"close": lambda self: None})()
    p.button(1, True)
    p.close()
    assert p._held == set()
    down, up = (m for m in sent if m[4:6] == bytes([2, 0]))  # opcode 2: button
    assert (down[16:20], up[16:20]) == (bytes([1, 0, 0, 0]), bytes(4))  # state: pressed, then released


def test_hyprland_move_floats_a_tiled_window_and_restore_tiles_it():
    b, r = hypr_backend(clients=[{"address": "0xa1", "floating": False, "workspace": {"id": 3, "name": "3"}}])
    b.move_window(HWIN, 5, 6)
    assert lua_calls(r) == ['hl.dsp.window.float({ window = "address:0xa1", action = "enable" })',
                            'hl.dsp.window.move({ window = "address:0xa1", x = 5, y = 6 })']
    b.restore_window(HWIN)
    assert lua_calls(r)[-2:] == ['hl.dsp.window.float({ window = "address:0xa1", action = "disable" })',
                                 'hl.dsp.focus({ window = "address:0xa1" })']


def test_hyprland_restore_leaves_a_window_the_user_floated():
    b, r = hypr_backend(clients=[{"address": "0xa1", "floating": True, "workspace": {"id": 3, "name": "3"}}])
    b.resize_window(HWIN, 7, 8)
    assert lua_calls(r) == ['hl.dsp.window.resize({ window = "address:0xa1", x = 7, y = 8 })']
    b.restore_window(HWIN)
    assert not [c for c in lua_calls(r) if "float" in c]


def test_hyprland_minimize_and_restore_use_a_special_workspace():
    b, r = hypr_backend(clients=[{"address": "0xa1", "floating": True,
                                  "workspace": {"id": -98, "name": "special:ccdesk"}}])
    b.minimize_window(HWIN)
    assert lua_calls(r)[-1] == 'hl.dsp.window.move({ window = "address:0xa1", workspace = "special:ccdesk", follow = false })'
    b.restore_window(HWIN)
    assert 'hl.dsp.window.move({ window = "address:0xa1", workspace = "3", follow = false })' in lua_calls(r)


@pytest.mark.parametrize("state,mode", [(1, ', mode = "maximized"'), (2, "")])
def test_hyprland_maximize_and_restore(state, mode):
    b, r = hypr_backend(clients=[{"address": "0xa1", "floating": True, "fullscreen": state,
                                  "workspace": {"id": 3, "name": "3"}}])
    b.maximize_window(HWIN)
    assert lua_calls(r)[-1] == 'hl.dsp.window.fullscreen({ window = "address:0xa1", action = "set", mode = "maximized" })'
    b.restore_window(HWIN)
    assert f'hl.dsp.window.fullscreen({{ window = "address:0xa1", action = "unset"{mode} }})' in lua_calls(r)


def test_hyprland_press_key_uses_its_own_shortcut():
    b, r = hypr_backend()
    b.press_key(parse_key_combo("ctrl+shift+t"))
    assert lua_calls(r) == ['hl.dsp.send_shortcut({ mods = "CTRL SHIFT", key = "t" })']
    b.press_key(parse_key_combo("BackSpace"))
    assert lua_calls(r)[-1] == 'hl.dsp.send_shortcut({ mods = "", key = "BackSpace" })'


def test_hyprland_dispatch_error_is_reported():
    b, r = hypr_backend()
    r.responses["hyprctl dispatch"] = "error: nope"
    with pytest.raises(CommandError, match="nope"):
        b.minimize_window(HWIN)


def test_hyprland_knows_the_active_window_and_workspace():
    b, _ = hypr_backend(active='{"address": "0xa1"}')
    assert b.active_window() == "0xa1"
    assert b.current_desktop() == 3
    assert hypr_backend()[0].active_window() is None


def test_hyprland_screen_size_spans_monitors_in_logical_pixels():
    b, _ = hypr_backend(monitors=[
        {"name": "eDP-1", "x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1.5, "transform": 0},
        {"name": "HDMI-A-1", "x": 1280, "y": 0, "width": 1920, "height": 1080, "scale": 1, "transform": 0}])
    assert b.screen_size() == (3200, 1080)


def test_hyprland_window_origin_by_pid_and_title():
    b, _ = hypr_backend(clients=[{"address": "0xa", "pid": 7, "title": "One", "at": [1, 2]},
                                 {"address": "0xb", "pid": 7, "title": "Two", "at": [3, 4]}])
    assert b.window_origin(7, "Two") == (3, 4)
    assert b.window_origin(8, "Two") is None


def test_hyprland_window_at_goes_by_what_stacks_higher():
    tiled = {"address": "0xa", "at": [0, 0], "size": [100, 100], "workspace": {"id": 1}, "focusHistoryID": 0}
    floating = {"address": "0xb", "at": [10, 10], "size": [50, 50], "workspace": {"id": 1}, "floating": True,
                "focusHistoryID": 1}
    elsewhere = {"address": "0xc", "at": [0, 0], "size": [100, 100], "workspace": {"id": 9}, "floating": True}
    monitors = [{"activeWorkspace": {"id": 1}, "specialWorkspace": {"id": 0}}]
    clients = [tiled, floating, elsewhere]
    b, _ = hypr_backend(clients=clients, monitors=monitors)
    assert b.window_at(20, 20) == "0xb"
    assert b.window_at(80, 80) == "0xa"
    assert b.window_at(500, 500) is None


def test_virtual_pointer_retries_a_failed_release():
    from ccdesk.backends.vpointer import VirtualPointer
    p = VirtualPointer.__new__(VirtualPointer)
    p._held, p.pointer = {1}, 5

    def broken(data):
        raise OSError("gone")

    p._send = broken
    with pytest.raises(OSError):
        p.button(1, False)
    assert p._held == {1}
