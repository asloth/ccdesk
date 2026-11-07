"""Named screens without starting anything: state file, permissions, duplicates, stale files, joining env."""

import json
import os

import pytest

from ccdesk import cli, headless, screen
from ccdesk.errors import CcdeskError


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return tmp_path


def fake_screen(extra=None):
    hl = headless.Headless(size="1280x800")
    hl.display = ":77"
    hl.wm_name = "openbox"
    hl.private_bus = True
    hl.env = {"DISPLAY": ":77", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/x/bus", "HOME": "/home/x",
              "SECRET_TOKEN": "hunter2", **headless.FORCE, **(extra or {})}
    return hl


def test_state_dir_falls_back_to_cache(runtime, monkeypatch):
    assert screen.state_dir() == runtime / "run" / "ccdesk" / "screens"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert screen.state_dir() == runtime / "cache" / "ccdesk" / "screens"


def test_state_file_holds_only_what_a_joiner_needs(runtime):
    path = screen.write_state("t1", fake_screen({"AT_SPI_BUS_ADDRESS": "unix:path=/x/a11y"}))
    info = json.loads(path.read_text())
    assert (info["name"], info["pid"], info["display"], info["size"], info["wm"]) == \
        ("t1", os.getpid(), ":77", "1280x800", "openbox")
    assert set(info["env"]) == {"DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "AT_SPI_BUS_ADDRESS", *headless.FORCE}
    assert "SECRET_TOKEN" not in path.read_text() and "HOME" not in info["env"]
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert oct(path.parent.parent.stat().st_mode & 0o777) == "0o700"


def test_at_spi_address_only_when_set(runtime):
    path = screen.write_state("t1", fake_screen())
    assert "AT_SPI_BUS_ADDRESS" not in json.loads(path.read_text())["env"]


def test_running_screen_is_found_and_start_refuses_a_duplicate(runtime):
    screen.write_state("t1", fake_screen())
    assert [i["name"] for i in screen.running()] == ["t1"]
    with pytest.raises(CcdeskError, match="already running"):
        screen.start("t1")


def test_stale_state_of_a_dead_pid_is_cleaned(runtime):
    path = screen.write_state("t1", fake_screen())
    info = json.loads(path.read_text())
    info["pid"] = 2 ** 22 + 12345  # above pid_max on most systems
    path.write_text(json.dumps(info))
    assert screen.running() == []
    assert not path.exists()
    path.write_text("not json")
    assert screen.read_state("t1") is None and not path.exists()


def test_bad_names_are_refused(runtime):
    for bad in ("", "../x", "a/b", ".hidden", "x" * 70, "a b"):
        with pytest.raises(CcdeskError):
            screen.state_path(bad)


def test_join_builds_env_without_your_session_and_without_foreign_keys(runtime):
    path = screen.write_state("t1", fake_screen())
    info = json.loads(path.read_text())
    info["env"]["LD_PRELOAD"] = "evil.so"  # a tampered file adds nothing outside the allowed keys
    path.write_text(json.dumps(info))
    base = {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0", "AT_SPI_BUS_ADDRESS": "mine", "HOME": "/home/me",
            "SWAYSOCK": "/run/sway"}
    joined = screen.join("t1", base)
    env = joined.env
    assert env["DISPLAY"] == ":77" and env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/x/bus"
    assert env["HOME"] == "/home/me" and env["GDK_BACKEND"] == "x11"
    assert not set(headless.DROP) & set(env) and "LD_PRELOAD" not in env
    assert joined.info()["display"] == ":77" and joined.private_bus
    joined.stop()
    assert screen.read_state("t1")  # joining and leaving does not touch the screen


def test_join_unknown_screen_says_how_to_start_it(runtime):
    with pytest.raises(CcdeskError, match="ccdesk screen start nope"):
        screen.join("nope")


def test_stop_sends_sigterm_to_the_pid(runtime, monkeypatch):
    screen.write_state("t1", fake_screen())
    sent = []
    real_kill = os.kill
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)) if sig else real_kill(pid, sig))
    screen.stop_screen("t1")
    assert sent == [(os.getpid(), 15)]
    with pytest.raises(CcdeskError):
        screen.stop_screen("missing")


def test_cli_list_and_headless_screen_are_exclusive(runtime, capsys):
    assert cli.main(["screen", "list"]) == 0
    assert "no named screen" in capsys.readouterr().out
    screen.write_state("t1", fake_screen())
    assert cli.main(["screen", "list"]) == 0
    assert "t1: :77 1280x800" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["mcp", "--headless", "--screen", "t1"])


def test_mcp_screen_unknown_exits_with_message(runtime, capsys):
    pytest.importorskip("mcp")
    assert cli.main(["mcp", "--screen", "nope"]) == 1
    assert "no screen named" in capsys.readouterr().err


def test_peek_lists_named_screens(runtime, monkeypatch, capsys):
    screen.write_state("t1", fake_screen())
    from ccdesk import backends

    class B:
        def __init__(self, env):
            self.env = env

        def screenshot(self, path, region):
            pass

    monkeypatch.setattr(backends, "X11Backend", B)
    assert cli.main(["peek"]) == 0
    assert "screen t1" in capsys.readouterr().out
