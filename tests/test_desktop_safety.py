"""Action layer: validation, safety policy and audit log, against a fake backend."""

import json
import os
import stat
import sys

import pytest

from ccdesk.backends.base import Backend
from ccdesk.desktop import Desktop
from ccdesk.errors import CcdeskError, PolicyError, UnsupportedError, ValidationError
from ccdesk.parsing import Window, parse_confirm_keys
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
    assert log[1]["args"] == {"x": 10, "y": 20, "button": "left", "count": 1, "force": False}
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
    lambda d: d.manage_window("Terminal", "minimize"),
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
    assert d.list_monitors()["screen"] == {"width": 800, "height": 600}
    assert d.screenshot(window="Terminal")["width"] == 300


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


def test_launch_expands_home(make_desk, monkeypatch):
    monkeypatch.setenv("HOME", "/home/ana")
    result = make_desk(allow_launch=True).launch("true --profile ~/ff --data=~/c ~x a~/b")
    os.waitpid(result["pid"], 0)
    assert result["argv"] == ["true", "--profile", "/home/ana/ff", "--data=/home/ana/c", "~x", "a~/b"]


@pytest.mark.parametrize("combo", ["alt+F4", "ctrl+q", "ctrl+w", "super+q", "ctrl+shift+w"])
def test_destructive_keys_need_confirm(make_desk, fake, read_audit, combo):
    d = make_desk()
    with pytest.raises(PolicyError, match="confirm=true"):
        d.press_key(combo)
    assert fake.calls == []
    assert read_audit()[-1]["status"] == "denied"
    d.press_key(combo, confirm=True)
    assert fake.calls and fake.calls[-1][0] == "press_key"


def test_extra_confirm_keys(make_desk, fake, read_audit):
    d = make_desk(confirm_keys=parse_confirm_keys("super+x"))
    with pytest.raises(PolicyError, match="confirm=true"):
        d.press_key("super+shift+x")
    assert fake.calls == []
    assert read_audit()[-1]["status"] == "denied"
    d.press_key("super+x", confirm=True)
    d.press_key("super+Return")
    assert [c[0] for c in fake.calls] == ["press_key", "press_key"]


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
    (lambda d: d.wait(301), "between"),
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
        "path": str(tmp_path / "out" / "r.png"), "width": 30, "height": 40, "approx_tokens": 2,
        "origin": {"x": 10, "y": 20}, "note": region["note"],
    }
    assert full["approx_tokens"] == 640


def test_screenshot_scale(make_desk, fake):
    d = make_desk()
    half = d.screenshot(scale=0.5)
    assert (half["width"], half["height"], half["scale"], half["approx_tokens"]) == (400, 300, 0.5, 160)
    assert fake.calls[-1] == ("scale_png", 400, 300)
    crop = d.screenshot(region="100,100,200,100", scale=0.25)
    assert (crop["width"], crop["height"]) == (50, 25)
    assert "origin.x+px/scale" in crop["note"]
    n = len(fake.calls)
    assert "scale" not in d.screenshot(scale=1)
    assert len(fake.calls) == n + 1  # no resize at full scale


@pytest.mark.parametrize("bad", [0, 0.05, 1.5, "half", True, None])
def test_screenshot_bad_scale(make_desk, fake, bad):
    with pytest.raises(ValidationError):
        make_desk().screenshot(scale=bad)
    assert fake.calls == []


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


FIREFOX, TERMINAL = "0x01e00003", "0x02000001"
GUARDED = [
    lambda d, **kw: d.click(5, 5, **kw),
    lambda d, **kw: d.scroll(5, 5, dy=1, **kw),
    lambda d, **kw: d.drag(5, 5, 9, 9, duration_ms=0, **kw),
    lambda d, **kw: d.type_text("rm -rf ~", **kw),
    lambda d, **kw: d.press_key("Return", **kw),
]


@pytest.mark.parametrize("call", GUARDED)
def test_input_refused_after_focus_moved(make_desk, fake, read_audit, call):
    """The user switched to their terminal after activate_window(Firefox): input must not land there."""
    d = make_desk()
    d.activate_window("Mozilla Firefox")
    fake.active = fake.under = TERMINAL
    with pytest.raises(PolicyError, match=r"'Terminal'.*'Mozilla Firefox'.*activate_window\('0x01e00003'\).*force=true"):
        call(d)
    assert [c for c in fake.calls if c[0] != "activate"] == []
    assert read_audit()[-1]["status"] == "denied"
    # force=true sends it anyway, and says where it went.
    assert call(d, force=True)["window"] == {"id": TERMINAL, "title": "Terminal"}


def test_click_element_is_guarded(make_desk, fake):
    d = make_desk()
    d.ui_tree()
    d.activate_window(FIREFOX)
    fake.under = TERMINAL
    with pytest.raises(PolicyError, match="Terminal"):
        d.click_element("e4")
    assert not [c for c in fake.calls if c[0] == "click"]


def test_input_results_name_the_receiving_window(make_desk, fake):
    d = make_desk()
    assert "window" not in d.type_text("x")  # the backend cannot tell which window is focused
    fake.active = TERMINAL
    assert d.type_text("x")["window"] == {"id": TERMINAL, "title": "Terminal"}  # no target yet: not guarded
    d.activate_window("Mozilla Firefox")
    firefox = {"id": FIREFOX, "title": "Mozilla Firefox"}
    assert d.type_text("x")["window"] == firefox
    assert d.press_key("ctrl+l")["window"] == firefox
    fake.under = FIREFOX
    assert d.click(5, 5)["window"] == firefox
    assert d.scroll(5, 5, dy=1)["window"] == firefox


def test_pointer_over_a_popup_falls_back_to_focus(make_desk, fake):
    """Menus and dropdowns are override-redirect windows that list_windows never shows."""
    d = make_desk()
    d.activate_window(FIREFOX)
    fake.under = "0x01e0aaaa"
    assert d.click(5, 5)["window"]["id"] == FIREFOX
    fake.active = TERMINAL
    with pytest.raises(PolicyError):
        d.click(5, 5)


def test_dialogs_of_the_target_are_allowed(make_desk, fake):
    """A file chooser opened by Firefox takes focus; it is still Firefox's input."""
    d = make_desk()
    d.activate_window(FIREFOX)
    fake.windows.append(Window(id="0x02a00003", title="File Upload", wm_class="xdg-desktop-portal-gtk",
                               x=0, y=0, width=10, height=10))
    fake.active = "0x02a00003"
    fake.transients["0x02a00003"] = FIREFOX
    assert d.type_text("/tmp/logo.png")["window"]["title"] == "File Upload"


def test_trailing_newline_is_an_explicit_return(make_desk, fake):
    """`type_text("https://…\\n")` typed the URL but never navigated in Firefox's address bar."""
    r = make_desk().type_text("https://play.google.com\n")
    assert fake.calls == [("type_text", "https://play.google.com"), ("press_key", "Return")]
    assert r["typed_chars"] == 23 and r["submitted"] is True
    assert fake.slept  # the field gets a moment to take the text before Return


def test_type_text_submit_and_replace(make_desk, fake):
    r = make_desk().type_text("Ana Lopez", replace=True, submit=True)
    assert fake.calls == [("press_key", "ctrl+a"), ("type_text", "Ana Lopez"), ("press_key", "Return")]
    assert r["replaced"] is True and r["submitted"] is True


def test_type_text_keeps_inner_newlines(make_desk, fake):
    d = make_desk()
    assert "submitted" not in d.type_text("line one\nline two")
    assert fake.calls == [("type_text", "line one\nline two")]
    fake.calls.clear()
    d.type_text("\n")
    assert fake.calls == [("press_key", "Return")]


@pytest.mark.parametrize("key,settles", [("ctrl+t", True), ("ctrl+l", True), ("alt+d", True), ("F6", True),
                                         ("Tab", False), ("Return", False), ("ctrl+shift+t", False)])
def test_focus_moving_shortcuts_settle(make_desk, fake, key, settles):
    """ctrl+t then type_text raced Firefox: the first letters went to the page and opened quick find."""
    make_desk().press_key(key)
    assert bool(fake.slept) is settles


def test_input_waits_while_the_user_is_active(make_desk, fake, read_audit):
    """--yield-ms: the user typed half a second ago, so ccdesk waits until they pause."""
    d = make_desk(yield_ms=3000)
    fake.last_event = fake.now - 0.5
    r = d.click(5, 5)
    assert fake.calls[-1][0] == "click"
    assert 2400 <= r["yielded_ms"] <= 2700
    assert read_audit()[-1]["status"] == "ok"


@pytest.mark.parametrize("call", [*GUARDED, lambda d: d.move(1, 1), lambda d: d.activate_window("Terminal")])
def test_input_refused_while_the_user_keeps_going(make_desk, fake, read_audit, call):
    d = make_desk(yield_ms=3000)
    fake.user_typing = True
    with pytest.raises(PolicyError, match="the user is using the mouse or keyboard"):
        call(d)
    assert fake.calls == []
    assert read_audit()[-1]["status"] == "denied"


def test_own_input_is_not_the_user(make_desk, fake):
    """xdotool's events reset the idle counter too; only input after ccdesk's own is the user's."""
    d = make_desk(yield_ms=3000)
    d.click(5, 5)
    fake.now += 0.1
    r = d.type_text("hello")
    assert "yielded_ms" not in r and fake.calls[-1] == ("type_text", "hello")


def test_yield_off_or_idle_unknown(make_desk, fake):
    fake.user_typing = True
    assert "yielded_ms" not in make_desk().click(5, 5)  # yield_ms=0, the default
    fake.last_event = None
    assert "yielded_ms" not in make_desk(yield_ms=3000).click(5, 5)


def test_doctor_reports_user_idle(make_desk, fake):
    fake.last_event = fake.now - 4.2
    report = make_desk(yield_ms=3000).doctor()
    assert report["user_idle_ms"] == 4200
    assert report["policy"]["yield_ms"] == 3000


def test_list_windows_marks_the_active_window_and_desktop(make_desk, fake):
    """Firefox and kitty had the same geometry on desktops 0 and 1: the list alone hid which was in front."""
    fake.active, fake.desktop = TERMINAL, 1
    r = make_desk().list_windows()
    assert [w["active"] for w in r["windows"]] == [False, True]
    assert r["current_desktop"] == 1
    fake.active, fake.desktop = None, None
    r = make_desk().list_windows()
    assert not any(w["active"] for w in r["windows"]) and r["current_desktop"] is None


def test_wait_allows_five_minutes(make_desk, fake):
    assert make_desk().wait(0)["waited"] == 0
    with pytest.raises(ValidationError, match="between 0 and 300"):
        make_desk().wait(301)


def test_closed_target_is_reported(make_desk, fake):
    d = make_desk()
    d.activate_window(FIREFOX)
    fake.windows = [w for w in fake.windows if w.id != FIREFOX]
    fake.active = TERMINAL
    with pytest.raises(PolicyError, match="is gone"):
        d.press_key("Return")


def test_manage_window_reports_before_and_after(make_desk, fake):
    d = make_desk()
    out = d.manage_window("Terminal", "move", x=50, y=60)
    assert fake.calls[-1] == ("move_window", "0x02000001", 50, 60)
    assert out["changed"] is True and out["window"]["title"] == "Terminal"
    assert (out["before"]["geometry"]["x"], out["after"]["geometry"]["x"]) == (10, 50)
    assert d.manage_window("Terminal", "resize", width=640, height=480)["after"]["geometry"]["width"] == 640
    assert d.manage_window("Terminal", "maximize")["after"]["geometry"]["height"] == 600


def test_manage_window_minimize_and_restore_change_on_show(make_desk):
    d = make_desk()
    out = d.manage_window("Terminal", "minimize")
    assert (out["changed"], out["on_show"]) == (True, False)
    out = d.manage_window("Terminal", "restore")
    assert (out["changed"], out["on_show"]) == (True, True)


def test_manage_window_says_when_nothing_happened(make_desk, fake):
    fake.move_window = lambda window, x, y: None  # a window manager that ignores the request
    out = make_desk().manage_window("Terminal", "move", x=50, y=60)
    assert out["changed"] is False and "ignored" in out["note"]


@pytest.mark.parametrize("kw,message", [
    ({"action": "spin"}, "action must be"),
    ({"action": "move", "x": 1}, "needs x and y"),
    ({"action": "resize", "width": 5}, "needs width and height"),
    ({"action": "resize", "width": 0, "height": 5}, ">= 1"),
    ({"action": "minimize", "x": 1}, "does not take x"),
    ({"action": "move", "x": 1, "y": 2, "width": 3}, "does not take width"),
])
def test_manage_window_validates(make_desk, fake, kw, message):
    with pytest.raises(ValidationError, match=message):
        make_desk().manage_window("Terminal", **kw)
    assert not [c for c in fake.calls if c[0].endswith("_window")]


def test_manage_window_audited(make_desk, read_audit):
    make_desk().manage_window("Terminal", "move", x=1, y=2)
    entry = read_audit()[-1]
    assert (entry["tool"], entry["status"], entry["args"]["x"]) == ("manage_window", "ok", 1)


def test_list_monitors(make_desk, fake):
    d = make_desk()
    assert d.list_monitors()["monitors"] == [] and "unknown" in d.list_monitors()["note"]
    fake.monitor_list = [{"name": "HDMI-1", "x": 0, "y": 0, "width": 800, "height": 600}]
    out = d.list_monitors()
    assert out["monitors"] == fake.monitor_list and "note" not in out


def test_screenshot_of_a_window_crops_to_it(make_desk):
    out = make_desk().screenshot(window="Terminal")
    assert (out["width"], out["height"]) == (300, 200)
    assert out["origin"] == {"x": 10, "y": 10}
    assert out["window"]["title"] == "Terminal"


def test_screenshot_of_a_window_refuses_what_is_not_on_show(make_desk, fake):
    fake.hidden.add("0x02000001")
    with pytest.raises(CcdeskError, match="minimized or on another desktop"):
        make_desk().screenshot(window="Terminal")
    assert not [c for c in fake.calls if c[0] == "screenshot"]


def test_screenshot_window_and_region_conflict(make_desk):
    with pytest.raises(ValidationError, match="not both"):
        make_desk().screenshot(window="Terminal", region="0,0,10,10")


def test_manage_window_ignores_a_title_change(make_desk, fake):
    def retitle(window, x, y):
        fake._replace_window(window, title="Terminal: building")  # the app did it; the move was ignored
    fake.move_window = retitle
    out = make_desk().manage_window("Terminal", "move", x=50, y=60)
    assert out["changed"] is False and "ignored" in out["note"]


def test_manage_window_waits_for_a_slow_window_manager(make_desk, fake):
    real_sleep = fake.sleep
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        real_sleep(seconds)
        if len(slept) == 4:  # landed a while after the request
            fake._replace_window(fake.windows[1], x=50, y=60)

    fake.sleep = sleep
    fake.move_window = lambda window, x, y: None
    out = make_desk().manage_window("Terminal", "move", x=50, y=60)
    assert out["changed"] is True and out["after"]["geometry"]["x"] == 50 and "note" not in out


def test_manage_window_minimize_counts_when_only_on_show_changes(make_desk):
    out = make_desk().manage_window("Terminal", "minimize")  # same box, as on sway's scratchpad
    assert out["changed"] is True and out["before"]["geometry"] == out["after"]["geometry"]


def test_screenshot_of_a_window_warns_when_it_cannot_check(make_desk, fake):
    fake.window_on_show = lambda window_id: None
    assert "could not check" in make_desk().screenshot(window="Terminal")["warning"]
    fake.window_on_show = lambda window_id: True
    assert "warning" not in make_desk().screenshot(window="Terminal")


def test_manage_window_says_when_the_window_closed(make_desk, fake):
    def close(window, x, y):
        fake.windows = [w for w in fake.windows if w.id != window.id]
    fake.move_window = close
    out = make_desk().manage_window("Terminal", "move", x=50, y=60)
    assert out["after"] is None and "gone" in out["note"]
