"""Action layer: validation, safety policy and audit log, against a fake backend."""

import json
import os
import stat
import sys

import pytest

from ccdesk.errors import PolicyError, UnsupportedError, ValidationError
from ccdesk.backends.base import Backend
from ccdesk.desktop import Desktop
from ccdesk.safety import INPUT_TOOLS, AuditLog, Policy, default_audit_path


def test_default_audit_path(monkeypatch, tmp_path):
    monkeypatch.delenv("CCDESK_AUDIT_LOG", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_audit_path() == tmp_path / ".cache" / "ccdesk" / "audit.log"
    monkeypatch.setenv("CCDESK_AUDIT_LOG", str(tmp_path / "x.log"))
    assert default_audit_path() == tmp_path / "x.log"


def test_every_action_is_audited(make_desk, read_audit, fake):
    d = make_desk()
    d.screenshot()
    d.click(10, 20)
    d.move(1, 2)
    d.drag(0, 0, 100, 100, duration_ms=0)
    d.scroll(5, 5, dy=3)
    d.type_text("hi")
    d.press_key("ctrl+l")
    d.list_windows()
    d.activate_window("firefox")
    d.clipboard("set", "secret-ish")
    d.clipboard("get")
    d.wait(0)
    d.doctor()
    log = read_audit()
    assert [e["tool"] for e in log] == [
        "screenshot", "click", "move", "drag", "scroll", "type_text", "press_key", "list_windows",
        "activate_window", "clipboard", "clipboard", "wait", "doctor",
    ]
    assert all(e["status"] == "ok" and "ts" in e for e in log)
    assert log[1]["args"] == {"x": 10, "y": 20, "button": "left", "count": 1}
    # Clipboard contents never reach the log, only their length.
    assert "secret-ish" not in json.dumps(log)
    assert log[9]["args"] == {"action": "set", "text_len": 10}
    assert log[10]["result"] == {"text": 10}
    assert ("activate", "0x01e00003") in fake.calls


def test_audit_log_permissions_and_append(tmp_path):
    path = tmp_path / "nested" / "audit.log"
    log = AuditLog(path)
    log.record("a", {}, "ok")
    log.record("b", {"text": "x" * 500}, "ok")
    lines = path.read_text().splitlines()
    assert [json.loads(l)["tool"] for l in lines] == ["a", "b"]
    entry = json.loads(lines[1])
    assert entry["args"]["text_len"] == 500 and len(entry["args"]["text"]) < 500
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_audit_failure_does_not_break_actions(fake, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    d = Desktop(backend=fake, audit=AuditLog(blocker / "audit.log"), shots_dir=tmp_path / "s")
    assert d.click(1, 1)["clicked"] == {"x": 1, "y": 1}


@pytest.mark.parametrize("call", [
    lambda d: d.click(1, 1),
    lambda d: d.move(1, 1),
    lambda d: d.drag(1, 1, 2, 2),
    lambda d: d.scroll(1, 1, dy=1),
    lambda d: d.type_text("x"),
    lambda d: d.press_key("a"),
    lambda d: d.activate_window("Terminal"),
    lambda d: d.clipboard("set", "x"),
    lambda d: d.launch("true"),
])
def test_read_only_blocks_input(make_desk, read_audit, fake, call):
    d = make_desk(read_only=True, allow_launch=True)
    with pytest.raises(PolicyError, match="read-only"):
        call(d)
    assert [c for c in fake.calls if c[0] != "screenshot"] == []
    assert read_audit()[-1]["status"] == "denied"


def test_read_only_allows_observation(make_desk):
    d = make_desk(read_only=True)
    assert d.screenshot()["width"] == 800
    assert len(d.list_windows()["windows"]) == 2
    assert d.clipboard("get") == {"text": ""}
    assert d.wait(0) == {"waited": 0.0}
    assert d.doctor()["policy"]["read_only"] is True


def test_input_tools_set():
    assert INPUT_TOOLS >= {"click", "move", "drag", "scroll", "type_text", "press_key", "launch"}
    assert not INPUT_TOOLS & {"screenshot", "list_windows", "wait", "doctor", "clipboard"}


def test_launch_disabled_by_default(make_desk, read_audit):
    with pytest.raises(PolicyError, match="--allow-launch"):
        make_desk().launch("true")
    assert read_audit()[-1] == {**read_audit()[-1], "tool": "launch", "status": "denied"}


def test_launch_when_allowed(make_desk, tmp_path):
    d = make_desk(allow_launch=True)
    marker = tmp_path / "launched"
    result = d.launch(f"{sys.executable} -c \"open(r'{marker}', 'w').write('ok')\"")
    assert result["pid"] > 0 and result["argv"][0] == sys.executable
    os.waitpid(result["pid"], 0)
    assert marker.read_text() == "ok"
    with pytest.raises(ValidationError, match="not found"):
        d.launch("definitely-not-a-real-binary-ccdesk")
    with pytest.raises(ValidationError):
        d.launch("  ")


def test_launch_does_not_use_a_shell(make_desk, tmp_path):
    d = make_desk(allow_launch=True)
    target = tmp_path / "pwned"
    result = d.launch(f"echo hi; touch {target}")
    os.waitpid(result["pid"], 0)
    assert not target.exists()


@pytest.mark.parametrize("combo", ["alt+F4", "ctrl+q", "ctrl+w", "super+q", "ctrl+shift+w"])
def test_destructive_keys_need_confirm(make_desk, fake, read_audit, combo):
    d = make_desk()
    with pytest.raises(PolicyError, match="confirm=true"):
        d.press_key(combo)
    assert fake.calls == []
    assert read_audit()[-1]["status"] == "denied"
    d.press_key(combo, confirm=True)
    assert fake.calls and fake.calls[-1][0] == "press_key"


def test_destructive_via_modifiers_list(make_desk, fake):
    with pytest.raises(PolicyError):
        make_desk().press_key("F4", modifiers=["Alt"])
    assert fake.calls == []


@pytest.mark.parametrize("call,match", [
    (lambda d: d.click(800, 10), "x must be <="),
    (lambda d: d.click(10, 600), "y must be <="),
    (lambda d: d.click(-1, 0), "x must be >="),
    (lambda d: d.click(1, 1, button="back"), "button"),
    (lambda d: d.click(1, 1, count=0), "count"),
    (lambda d: d.click(1, 1, count=11), "count"),
    (lambda d: d.scroll(1, 1), "nothing to scroll"),
    (lambda d: d.scroll(1, 1, dy=51), "dy"),
    (lambda d: d.drag(0, 0, 1, 1, duration_ms=-5), "duration_ms"),
    (lambda d: d.type_text(""), "non-empty"),
    (lambda d: d.type_text("x" * 10_001), "clipboard"),
    (lambda d: d.press_key("bogus key!"), "invalid key"),
    (lambda d: d.wait(61), "between"),
    (lambda d: d.wait("soon"), "number"),
    (lambda d: d.clipboard("cut"), "get"),
    (lambda d: d.clipboard("set"), "needs text"),
    (lambda d: d.screenshot(region="1,2,3"), "4 numbers"),
    (lambda d: d.screenshot(region="700,500,200,200"), "exceeds"),
    (lambda d: d.screenshot(path="/tmp/x.jpg"), ".png"),
    (lambda d: d.activate_window("nonexistent window"), "no window title"),
    (lambda d: d.activate_window("0xdeadbeef"), "no window with id"),
])
def test_validation(make_desk, fake, read_audit, call, match):
    with pytest.raises(ValidationError, match=match):
        call(make_desk())
    assert [c for c in fake.calls if c[0] not in ("screenshot",)] == []
    assert read_audit()[-1]["status"] == "error"


def test_screenshot_results(make_desk, fake, tmp_path):
    d = make_desk()
    full = d.screenshot()
    assert full["width"] == 800 and full["height"] == 600
    assert full["path"].startswith(str(tmp_path / "shots"))
    region = d.screenshot(region="10,20,30,40", path=str(tmp_path / "out" / "r.png"))
    assert region == {
        "path": str(tmp_path / "out" / "r.png"), "width": 30, "height": 40,
        "origin": {"x": 10, "y": 20}, "note": region["note"],
    }


def test_screenshot_pruning(make_desk, tmp_path, monkeypatch):
    import ccdesk.desktop as mod

    monkeypatch.setattr(mod, "KEEP_SCREENSHOTS", 3)
    d = make_desk()
    for _ in range(6):
        d.screenshot()
    assert len(list((tmp_path / "shots").glob("screen-*.png"))) <= 3


def test_activate_window_matching(make_desk, fake):
    d = make_desk()
    assert d.activate_window("Terminal")["activated"]["id"] == "0x02000001"
    assert d.activate_window("mozilla")["activated"]["id"] == "0x01e00003"
    assert d.activate_window("0x1e00003")["activated"]["title"] == "Mozilla Firefox"


def test_no_display_is_reported_cleanly(tmp_path):
    d = Desktop(backend=Backend(env={}), audit=AuditLog(tmp_path / "a.log"), shots_dir=tmp_path)
    with pytest.raises(UnsupportedError, match="no graphical session"):
        d.click(1, 1)
    with pytest.raises(UnsupportedError):
        d.screenshot()
    report = d.doctor()
    assert report["ready"] is False and report["backend"] == "none"
