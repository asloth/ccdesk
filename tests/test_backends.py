"""Backend command construction and session detection, with a recording runner (no display needed)."""

import os

import pytest

from ccdesk.backends import Backend, WaylandBackend, X11Backend, detect_session, get_backend
from ccdesk.backends.base import interpolate
from ccdesk.backends.wayland import parse_hyprctl_clients, parse_sway_tree
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
    assert r.calls[0][0] == ["grim", "-g", "1,2 3x4", "/tmp/s.png"]
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
