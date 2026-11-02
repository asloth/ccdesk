"""ccdesk firefox, install --firefox and the doctor report, against a fake /proc."""

import json

import pytest

from ccdesk import firefox
from ccdesk.cli import main
from tests.test_cli import fake_claude, home  # noqa: F401 (home is a fixture)


def fake_proc(tmp_path, *cmdlines):
    proc = tmp_path / "proc"
    proc.mkdir(parents=True)
    (proc / "self").mkdir()  # not a pid: skipped
    for pid, argv in enumerate(cmdlines, start=100):
        (proc / str(pid)).mkdir()
        (proc / str(pid) / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
    return proc


BROWSER = ["/usr/lib/firefox/firefox"]
AUTOMATED = [*BROWSER, "--marionette", "--remote-debugging-port"]
CONTENT = ["/usr/lib/firefox/firefox", "-contentproc", "-isForBrowser", "tab"]


def test_finds_the_browser_not_its_tab_processes(tmp_path):
    proc = fake_proc(tmp_path, BROWSER, CONTENT, ["/usr/bin/zsh"])
    assert firefox.processes(proc) == [{"pid": 100, "automation": False, "agent": False}]
    assert firefox.status(proc) == {"running": True, "automation": False, "pids": [100], "agent_running": False}


def test_claudes_own_firefox_is_not_the_users(tmp_path, monkeypatch):
    """Its automation flags are expected: doctor must not tell the user to turn them off."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    own = str(tmp_path / "data" / "ccdesk" / "firefox" / "firefox_devtools_mcp_profile")
    proc = fake_proc(tmp_path, [*AUTOMATED, "-profile", own])
    assert firefox.status(proc) == {"running": False, "automation": False, "pids": [], "agent_running": True}


def test_automation_needs_both_flags(tmp_path):
    proc = fake_proc(tmp_path, AUTOMATED, [*BROWSER, "--marionette"],
                     ["firefox", "--marionette", "--remote-debugging-port=9222"])
    assert [p["automation"] for p in firefox.processes(proc)] == [True, False, True]


def test_other_users_are_ignored(tmp_path):
    proc = fake_proc(tmp_path, AUTOMATED)
    assert firefox.processes(proc, uid=12345) == []


class Popen:
    def __init__(self):
        self.argv = None

    def __call__(self, argv, **kw):
        assert kw["start_new_session"] is True
        self.argv = argv


def test_start_with_automation(tmp_path, monkeypatch):
    monkeypatch.setattr(firefox.shutil, "which", lambda name: "/usr/bin/firefox")
    popen, out = Popen(), []
    assert firefox.start(["-P", "work"], proc=fake_proc(tmp_path), popen=popen, out=out.append) == 0
    assert popen.argv == ["/usr/bin/firefox", "--marionette", "--remote-debugging-port", "-P", "work"]
    assert "navigator.webdriver" in out[0]


def test_never_quits_a_running_firefox(tmp_path, monkeypatch):
    monkeypatch.setattr(firefox.shutil, "which", lambda name: "/usr/bin/firefox")
    popen, out = Popen(), []
    assert firefox.start([], proc=fake_proc(tmp_path, BROWSER), popen=popen, out=out.append) == 1
    assert popen.argv is None and "Quit it (Ctrl+Q" in out[0]
    assert firefox.start([], proc=fake_proc(tmp_path / "b", AUTOMATED), popen=popen, out=out.append) == 0
    assert popen.argv is None and "already running with automation on" in out[1]


def test_off(tmp_path, monkeypatch):
    monkeypatch.setattr(firefox.shutil, "which", lambda name: "/usr/bin/firefox")
    popen, out = Popen(), []
    assert firefox.start([], automation=False, proc=fake_proc(tmp_path, AUTOMATED), popen=popen,
                         out=out.append) == 1
    assert popen.argv is None
    assert firefox.start([], automation=False, proc=fake_proc(tmp_path / "b"), popen=popen, out=out.append) == 0
    assert popen.argv == ["/usr/bin/firefox"]


def test_cli_firefox_passes_extra_args(monkeypatch):
    seen = {}
    monkeypatch.setattr(firefox, "start", lambda extra, automation=True: seen.update(extra=extra, on=automation) or 0)
    assert main(["firefox", "--", "-P", "work"]) == 0
    assert seen == {"extra": ["-P", "work"], "on": True}
    assert main(["firefox", "--off"]) == 0
    assert seen == {"extra": [], "on": False}


def test_install_firefox_gives_claude_its_own(home, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    log = fake_claude(tmp_path, monkeypatch)
    assert main(["install", "--firefox"]) == 0
    calls = log.read_text().splitlines()
    profile = home / ".local" / "share" / "ccdesk" / "firefox"
    assert calls[2] == "mcp remove --scope user firefox-devtools"
    assert calls[3] == ("mcp add --scope user firefox-devtools -- npx -y @mozilla/firefox-devtools-mcp@0.10.6 "
                        f"--profile-path {profile} --tool-preset slim")
    assert profile.is_dir() and (profile.stat().st_mode & 0o777) == 0o700
    assert (profile / "firefox_devtools_mcp_profile" / "chrome" / "userChrome.css").is_file()
    out = capsys.readouterr().out
    assert "your own Firefox is not touched" in out and "orange" in out


def test_install_firefox_mine_attaches_to_the_users(home, tmp_path, monkeypatch, capsys):
    log = fake_claude(tmp_path, monkeypatch)
    assert main(["install", "--firefox", "mine"]) == 0
    assert log.read_text().splitlines()[3].endswith(
        "@mozilla/firefox-devtools-mcp@0.10.6 --connect-existing --marionette-port 2828 --tool-preset slim")
    assert "navigator.webdriver" in capsys.readouterr().out
    assert not (home / ".local" / "share" / "ccdesk" / "firefox").exists()


def test_install_without_firefox_leaves_it_alone(home, tmp_path, monkeypatch, capsys):
    log = fake_claude(tmp_path, monkeypatch)
    main(["install", "--dry-run"])
    assert "firefox-devtools" not in capsys.readouterr().out
    main(["install"])
    assert "firefox-devtools" not in log.read_text()


def test_doctor_warns_when_automation_is_left_on(monkeypatch, capsys):
    monkeypatch.setenv("CCDESK_BACKEND", "x11")
    state = {"running": True, "automation": True, "pids": [100], "agent_running": False}
    monkeypatch.setattr(firefox, "status", lambda: state)
    main(["doctor", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["firefox"] == state
    assert any("ccdesk firefox --off" in w for w in report["warnings"])
    main(["doctor"])
    assert "firefox:      running, automation on" in capsys.readouterr().out


def test_claudes_firefox_is_marked(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    profile = firefox.prepare_profile()
    assert profile == tmp_path / "data" / "ccdesk" / "firefox" / "firefox_devtools_mcp_profile"
    assert "#d97757" in (profile / "chrome" / "userChrome.css").read_text()
    assert (profile / "user.js").read_text() == firefox.STYLESHEETS_PREF + "\n"
    assert (profile.parent.stat().st_mode & 0o777) == 0o700


def test_marking_keeps_the_users_edits(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    profile = firefox.prepare_profile()
    (profile / "chrome" / "userChrome.css").write_text("/* mine */")
    (profile / "user.js").write_text('user_pref("a", 1);')
    firefox.prepare_profile()
    firefox.prepare_profile()
    assert (profile / "chrome" / "userChrome.css").read_text() == "/* mine */"
    assert (profile / "user.js").read_text() == 'user_pref("a", 1);\n' + firefox.STYLESHEETS_PREF + "\n"
