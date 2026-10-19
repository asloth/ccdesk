import pytest

from ccdesk.errors import ValidationError
from ccdesk.parsing import (
    KeyCombo,
    Region,
    check_point,
    is_destructive,
    parse_button,
    parse_confirm_keys,
    parse_display_geometry,
    parse_key_combo,
    parse_region,
    parse_shell_vars,
    parse_window_ref,
    parse_wmctrl_lgx,
    parse_xprop_wm_class,
    parse_xrdb_dpi,
    png_size,
)
from tests.pngutil import write_png


@pytest.mark.parametrize("key,mods,expected", [
    ("Return", None, "Return"),
    ("enter", None, "Return"),
    ("esc", None, "Escape"),
    ("ctrl+l", None, "ctrl+l"),
    ("Ctrl+Shift+T", None, "ctrl+shift+T"),
    ("t", ["shift", "control"], "ctrl+shift+t"),
    ("cmd+q", None, "super+q"),
    ("alt+f4", None, "alt+F4"),
    ("pagedown", None, "Page_Down"),
    ("ctrl++", None, "ctrl+plus"),
    ("+", None, "plus"),
    ("super", None, "super"),
    ("a", ["ctrl", "ctrl"], "ctrl+a"),
])
def test_parse_key_combo(key, mods, expected):
    assert parse_key_combo(key, mods).xdotool() == expected


@pytest.mark.parametrize("key,mods", [
    ("", None), ("ctrl+", None), ("hyper+a", None), ("a", ["fn"]), ("rm -rf", None), ("a;b", None), (None, None),
])
def test_parse_key_combo_rejects(key, mods):
    with pytest.raises(ValidationError):
        parse_key_combo(key, mods)


@pytest.mark.parametrize("key,mods,destructive", [
    ("alt+F4", None, True),
    ("F4", ["alt"], True),
    ("ctrl+q", None, True),
    ("ctrl+Q", None, True),
    ("ctrl+w", None, True),
    ("ctrl+shift+w", None, True),
    ("q", ["super"], True),
    ("cmd+q", None, True),
    ("q", None, False),
    ("ctrl+t", None, False),
    ("F4", None, False),
    ("alt+q", None, False),
    ("shift+w", None, False),
    ("ctrl+alt+Delete", None, True),
    ("ctrl+alt+BackSpace", None, True),
    ("ctrl+BackSpace", None, False),
])
def test_destructive(key, mods, destructive):
    assert is_destructive(parse_key_combo(key, mods)) is destructive


@pytest.mark.parametrize("key,destructive", [
    ("super+x", True),
    ("super+shift+x", True),  # supersets count, as with the built-ins
    ("ctrl+super+x", True),
    ("x", False),
    ("ctrl+alt+super+p", True),
    ("super+p", False),
])
def test_destructive_extra(key, destructive):
    extra = parse_confirm_keys("super+x, ctrl+alt+super+p")
    assert is_destructive(parse_key_combo(key), extra) is destructive


def test_parse_confirm_keys():
    assert parse_confirm_keys(None) == ()
    assert parse_confirm_keys(" ") == ()
    assert parse_confirm_keys("win+X,") == (KeyCombo("X", ("super",)),)
    with pytest.raises(ValidationError):
        parse_confirm_keys("super+x,bad key")


def test_keycombo_str():
    assert str(KeyCombo("q", ("ctrl", "shift"))) == "Ctrl+Shift+q"


@pytest.mark.parametrize("value,expected", [
    ("10,20,300,400", Region(10, 20, 300, 400)),
    (" 10, 20, 300, 400 ", Region(10, 20, 300, 400)),
    ("300x400+10+20", Region(10, 20, 300, 400)),
    ([10, 20, 300, 400], Region(10, 20, 300, 400)),
    ({"x": 1, "y": 2, "width": 3, "height": 4}, Region(1, 2, 3, 4)),
    (None, None),
    ("", None),
])
def test_parse_region(value, expected):
    assert parse_region(value) == expected


@pytest.mark.parametrize("value", ["1,2,3", "a,b,c,d", "0,0,0,10", "-1,0,5,5", [1, 2], {"x": 1}, 42])
def test_parse_region_rejects(value):
    with pytest.raises(ValidationError):
        parse_region(value)


def test_region_bounds():
    assert parse_region("0,0,800,600", (800, 600)) == Region(0, 0, 800, 600)
    with pytest.raises(ValidationError):
        parse_region("700,0,200,10", (800, 600))
    assert Region(1, 2, 3, 4).geometry() == "3x4+1+2"


def test_check_point():
    assert check_point(0, 0, (800, 600)) == (0, 0)
    assert check_point(799, 599, (800, 600)) == (799, 599)
    assert check_point(5.0, 6.0) == (5, 6)
    for bad in [(800, 0), (0, 600), (-1, 0), (1.5, 2), ("1", 2), (True, 1)]:
        with pytest.raises(ValidationError):
            check_point(*bad, (800, 600))
    assert check_point(5000, 5000, None) == (5000, 5000)


def test_parse_button():
    assert parse_button("left") == 1
    assert parse_button("Middle") == 2
    assert parse_button("right") == 3
    assert parse_button(3) == 3
    for bad in ["back", 4, True, None]:
        with pytest.raises(ValidationError):
            parse_button(bad)


WMCTRL = """\
0x01e00003  0 0    27   1920 1053 Navigator.firefox     host Example Domain — Mozilla Firefox
0x02a0000a -1 0    0    1920 27   xfce4-panel.Xfce4-panel  host xfce4-panel
0x03400004  1 100  200  640  480  xterm.XTerm           host
garbage line
"""


def test_parse_wmctrl():
    wins = parse_wmctrl_lgx(WMCTRL)
    assert len(wins) == 3
    ff = wins[0].to_dict()
    assert ff == {
        "id": "0x01e00003", "title": "Example Domain — Mozilla Firefox", "class": "Navigator.firefox",
        "desktop": 0, "geometry": {"x": 0, "y": 27, "width": 1920, "height": 1053},
    }
    assert wins[1].desktop == -1
    assert wins[2].title == "" and wins[2].x == 100


def test_small_parsers():
    assert parse_shell_vars("WINDOW=1\nX=10\nY=20\nWIDTH=30\n") == {"WINDOW": "1", "X": "10", "Y": "20", "WIDTH": "30"}
    assert parse_display_geometry("1920 1080\n") == (1920, 1080)
    with pytest.raises(ValidationError):
        parse_display_geometry("")
    assert parse_xrdb_dpi("Xcursor.size:\t24\nXft.dpi:\t192\n") == 192.0
    assert parse_xrdb_dpi("Xcursor.size: 24") is None
    assert parse_xprop_wm_class('WM_CLASS(STRING) = "Navigator", "firefox"') == "Navigator.firefox"
    assert parse_xprop_wm_class("WM_CLASS:  not found.") == ""


@pytest.mark.parametrize("value,expected", [
    ("0x01e00003", ("id", "0x01e00003")),
    ("0x1E00003", ("id", "0x01e00003")),
    ("31457283", ("id", "0x01e00003")),
    (31457283, ("id", "0x01e00003")),
    ("Firefox", ("title", "Firefox")),
])
def test_parse_window_ref(value, expected):
    assert parse_window_ref(value) == expected


def test_png_size(tmp_path):
    p = tmp_path / "a.png"
    write_png(p, 37, 21)
    assert png_size(p) == (37, 21)
    bad = tmp_path / "b.png"
    bad.write_bytes(b"not a png at all, sorry...")
    with pytest.raises(ValidationError):
        png_size(bad)
