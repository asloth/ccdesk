"""Headless screen setup without starting anything: size parsing, env, window manager choice."""

import json
import os

import pytest

from ccdesk import headless
from ccdesk.errors import CcdeskError
from ccdesk.headless import Headless, parse_size, running


def test_parse_size():
    assert parse_size("1600x900") == (1600, 900)
    for bad in ("1600", "big", "16x9", "1600x900x24"):
        with pytest.raises(CcdeskError):
            parse_size(bad)


def test_missing_xvfb_says_what_to_install():
    with pytest.raises(CcdeskError, match="xorg-server-xvfb"):
        Headless(which=lambda name: None).start()


class Recorder(Headless):
    def __init__(self, tools, wm="auto"):
        super().__init__(wm=wm, which=lambda n: f"/usr/bin/{n}" if n in tools else None)
        self.spawned = []

    def _spawn(self, argv, **kw):
        self.spawned.append(argv)


@pytest.mark.parametrize("tools,wm,expected", [
    ({"openbox", "bspwm"}, "auto", ["openbox"]),
    ({"bspwm"}, "auto", ["bspwm", "-c", str(headless.BSPWMRC)]),
    (set(), "auto", None),
    ({"openbox"}, "none", None),
    ({"icewm"}, "icewm --replace", ["icewm", "--replace"]),
])
def test_window_manager_choice(tools, wm, expected):
    hl = Recorder(tools, wm)
    hl._start_wm()
    assert hl.spawned == ([expected] if expected else [])
    assert bool(hl.notes) == (expected is None)


def test_missing_custom_window_manager():
    with pytest.raises(CcdeskError, match="not found"):
        Recorder(set(), "twm")._start_wm()


def test_env_points_away_from_your_session(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    monkeypatch.setenv("BSPWM_SOCKET", "/tmp/bspwm_0_0-socket")
    hl = Recorder(set())
    hl.env = {k: v for k, v in os.environ.items() if k not in headless.DROP}
    hl.env.update(headless.FORCE)
    assert "WAYLAND_DISPLAY" not in hl.env and "BSPWM_SOCKET" not in hl.env
    assert hl.env["GDK_BACKEND"] == "x11" and hl.env["QT_QPA_PLATFORM"] == "xcb"


def test_bspwm_config_is_executable():
    assert os.access(headless.BSPWMRC, os.X_OK)


def test_running_drops_dead_servers(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    folder = tmp_path / "ccdesk" / "headless"
    folder.mkdir(parents=True)
    (folder / "1.json").write_text(json.dumps({"display": ":5", "pid": 2**22 + 7}))
    (folder / "me.json").write_text(json.dumps({"display": ":6", "pid": os.getpid()}))
    assert [s["display"] for s in running()] == [":6"]
    assert not (folder / "1.json").exists()
