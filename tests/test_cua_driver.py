from __future__ import annotations

import json
import os
import shutil
import signal
from pathlib import Path

import pytest

from ccdesk import cli
from ccdesk.backends.cua_driver import CuaDriverBackend, HOVER_NOTE, report
from ccdesk.desktop import Desktop
from ccdesk.errors import CommandError, CuaDriverError, TargetHiddenError, TargetRequiredError, UnsupportedError
from ccdesk.parsing import Window, parse_key_combo
from ccdesk.safety import AuditLog
from tests.conftest import FakeBackend


@pytest.fixture
def session(tmp_path):
    session = FakeBackend()
    session.env = dict(os.environ, FAKE_LOG=str(tmp_path / "calls.jsonl"))
    session.windows = [Window("0x00000020", "Agent", "tk", 100, 200, 400, 300, 0)]
    session.under = session.windows[0].id
    session.active = "user-window"
    session.env["FAKE_WINDOWS"] = json.dumps([
        {"window_id": 32, "pid": 1234, "title": "Agent",
         "bounds": {"x": 100, "y": 200, "width": 400, "height": 300}}
    ])
    fake = tmp_path / "cua-driver"
    shutil.copyfile(Path(__file__).with_name("fake_cua_driver.py"), fake)
    fake.chmod(0o755)
    session.binary = str(fake)
    return session


@pytest.fixture
def backend(session):
    backend = CuaDriverBackend(session, session.binary)
    yield backend
    backend.close()


def calls(session):
    return [json.loads(line) for line in Path(session.env["FAKE_LOG"]).read_text().splitlines()]


def input_calls(session):
    return [r["params"] for r in calls(session) if r["method"] == "tools/call" and
            r["params"]["name"] not in ("check_permissions", "list_windows")]


def test_exact_input_contract_and_one_process(backend, session):
    backend.activate_window(session.windows[0])
    backend.click(125, 240, 3, 2)
    backend.drag(120, 230, 190, 290, 450)
    backend.scroll(140, 260, -2, 3)
    backend.type_text("hello →")
    backend.press_key(parse_key_combo("ctrl+enter"))
    backend.move(800, 600)
    address = {"pid": 1234, "window_id": 32, "delivery_mode": "background"}
    point_address = address | {"coordinate_frame": "desktop"}
    assert input_calls(session) == [
        {"name": "click", "arguments": point_address | {"x": 125, "y": 240, "button": "right", "count": 2}},
        {"name": "drag", "arguments": point_address | {"from_x": 120, "from_y": 230, "to_x": 190, "to_y": 290,
                                                   "duration_ms": 450}},
        {"name": "scroll", "arguments": point_address | {"x": 140, "y": 260, "direction": "down", "amount": 3, "by": "line"}},
        {"name": "scroll", "arguments": point_address | {"x": 140, "y": 260, "direction": "left", "amount": 2, "by": "line"}},
        {"name": "type_text", "arguments": address | {"text": "hello →"}},
        {"name": "press_key", "arguments": address | {"key": "enter", "modifiers": ["ctrl"]}},
        {"name": "move_cursor", "arguments": {"x": 800, "y": 600, "scope": "window"}},
    ]
    assert len({r["pid"] for r in calls(session)}) == 1
    assert session.active == "user-window"
    assert not session.calls
    assert backend.take_note() == HOVER_NOTE
    assert backend.take_note() is None
    proc = backend.driver.proc
    backend.close()
    backend.close()
    assert proc.poll() is not None


def test_typing_requires_selected_live_window(backend, session):
    with pytest.raises(TargetRequiredError):
        backend.type_text("bad")
    backend.activate_window(session.windows[0])
    session.windows.clear()
    with pytest.raises(TargetRequiredError):
        backend.press_key(parse_key_combo("tab"))
    assert not input_calls(session)


def test_hidden_window_never_activates(backend, session):
    backend.activate_window(session.windows[0])
    session.hidden.add(session.windows[0].id)
    with pytest.raises(TargetHiddenError):
        backend.type_text("hidden")
    with pytest.raises(TargetHiddenError, match="private screen"):
        backend.activate_window(session.windows[0])
    session.hidden.clear()
    session.desktop = 1
    with pytest.raises(TargetHiddenError):
        backend.activate_window(session.windows[0])
    assert not session.calls


def test_wayland_without_atspi_does_not_guess_addressed_route_availability(session):
    session.name = "wayland"
    session.env["WAYLAND_DISPLAY"] = "fake"
    session.env["FAKE_ATSPI"] = "0"
    with CuaBackendContext(session) as backend:
        assert backend.status["available"] is None
        assert "unknown" in backend.status["reason"]


def test_x11_targeting_never_uses_pointer_fallback(backend, session, monkeypatch):
    session.name = "x11"
    monkeypatch.setattr(session, "_xq", lambda: None, raising=False)
    monkeypatch.setattr(session, "window_at", lambda *_: pytest.fail("pointer-warp fallback called"))
    with pytest.raises(UnsupportedError, match="libX11"):
        backend.click(125, 240, 1, 1)
    class Query:
        def window_at(self, x, y, *, ignore_titles):
            assert (x, y) == (125, 240)
            assert ignore_titles == ("Cua.AgentCursorOverlay.",)
            return 32
    monkeypatch.setattr(session, "_xq", Query)
    backend.click(125, 240, 1, 1)
    assert input_calls(session)[0]["arguments"]["x"] == 125


def test_ambiguous_native_handle_refused(session):
    records = json.loads(session.env["FAKE_WINDOWS"])
    session.env["FAKE_WINDOWS"] = json.dumps(records + [records[0] | {"window_id": 33}])
    with CuaBackendContext(session) as backend:
        with pytest.raises(CuaDriverError) as exc:
            backend.click(125, 240, 1, 1)
        assert exc.value.input_emitted is False
    assert not input_calls(session)


class CuaBackendContext:
    def __init__(self, session):
        self.backend = CuaDriverBackend(session, session.binary)
    def __enter__(self):
        return self.backend
    def __exit__(self, *args):
        self.backend.close()


@pytest.mark.parametrize("code", ["target_occluded", "background_pointer_failed", "popup_keyboard_grab",
                                  "background_unavailable"])
@pytest.mark.parametrize("emitted", [False, True])
def test_refusals_preserve_emission_without_focus_advice(session, code, emitted):
    session.env["FAKE_FAILURE"] = json.dumps({"ok": False, "code": code, "input_emitted": emitted,
                                            "suggestion": "use foreground and take user focus"})
    with CuaBackendContext(session) as backend:
        with pytest.raises(CuaDriverError) as caught:
            backend.click(125, 240, 1, 1)
        assert caught.value.code == code
        assert caught.value.input_emitted is emitted
        assert "foreground" not in str(caught.value)
        assert "user focus" not in str(caught.value)
        assert caught.value.extra == {}
    assert len(input_calls(session)) == 1


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_lost_response_never_retries(session, failure):
    session.env["FAKE_FAILURE"] = failure
    with CuaBackendContext(session) as backend:
        backend.driver.timeout = 0.1
        with pytest.raises(CuaDriverError) as caught:
            backend.click(125, 240, 1, 1)
        assert caught.value.input_emitted is True
        assert backend.driver.proc.poll() is not None
    assert len(input_calls(session)) == 1


def test_probe_startup_and_missing_binary(session):
    assert report(session, session.binary)["available"] is True
    session.env["FAKE_UNAVAILABLE"] = "1"
    with pytest.raises(CuaDriverError, match="Background input is unavailable"):
        CuaDriverBackend(session, session.binary)
    assert report(session, session.binary)["available"] is False
    with pytest.raises(CommandError, match="missing"):
        CuaDriverBackend(session, "/missing/cua-driver")


def test_unsupported_version_fails_clearly(session):
    session.env["FAKE_VERSION"] = "0.28.2"
    with pytest.raises(CommandError, match="0.28.2"):
        CuaDriverBackend(session, session.binary)
    assert report(session, session.binary)["version"] == "cua-driver 0.28.2"


def test_move_note_reaches_desktop(backend, tmp_path):
    desk = Desktop(backend=backend, agent="agent", audit=AuditLog(tmp_path / "audit"), shots_dir=tmp_path)
    assert desk.move(50, 60)["note"] == HOVER_NOTE


def test_cli_contract():
    args = cli.build_parser().parse_args(["mcp", "--agent", "a", "--input", "cua-driver", "--cua-driver", "/driver"])
    assert args.input == "cua-driver" and args.cua_driver == "/driver"
    for argv in (["mcp", "--input", "cua-driver"],
                 ["mcp", "--agent", "a", "--input", "cua-driver", "--own-pointer"],
                 ["mcp", "--cua-driver", "/driver"]):
        with pytest.raises(SystemExit) as caught:
            cli.main(argv)
        assert caught.value.code == 2


def test_cli_owns_and_closes_driver(session, monkeypatch, tmp_path):
    import ccdesk.backends
    import ccdesk.server

    monkeypatch.setattr(ccdesk.backends, "get_backend", lambda _: session)
    seen = []
    def serve(desktop):
        assert isinstance(desktop.backend, CuaDriverBackend)
        assert desktop.policy.yield_ms == 0
        seen.append(desktop.backend.driver.proc)
    monkeypatch.setattr(ccdesk.server, "serve", serve)
    original_signal = signal.getsignal(signal.SIGTERM)
    assert cli.main(["mcp", "--agent", "a", "--input", "cua-driver", "--cua-driver", session.binary,
                     "--yield-ms", "2000", "--audit-log", str(tmp_path / "audit")]) == 0
    assert seen[0].returncode == 0  # EOF, rather than a forced signal
    assert signal.getsignal(signal.SIGTERM) == original_signal


def test_cli_fails_before_serving_when_background_is_unavailable(session, monkeypatch, capsys):
    import ccdesk.backends
    import ccdesk.server

    session.env["FAKE_UNAVAILABLE"] = "1"
    monkeypatch.setattr(ccdesk.backends, "get_backend", lambda _: session)
    monkeypatch.setattr(ccdesk.server, "serve", lambda _: pytest.fail("server started despite refusal"))
    assert cli.main(["mcp", "--agent", "a", "--input", "cua-driver", "--cua-driver", session.binary]) == 1
    assert "Background input is unavailable" in capsys.readouterr().err


def test_inspection_and_clipboard_stay_on_session(backend, session, tmp_path):
    session.clipboard_set("sentinel")
    assert backend.clipboard_get() == "sentinel"
    assert backend.list_windows() == session.windows
    backend.screenshot(str(tmp_path / "screen.png"), None)
    assert session.calls[-1] == ("screenshot", None)
    assert input_calls(session) == []
    assert backend.tools_report()["input"] == {"cua-driver": session.binary}


@pytest.mark.parametrize("effect,emitted", [("refused", False), ("none", False), ("unknown", True),
                                           ("unverifiable", True)])
def test_driver_effect_maps_input_emission(effect, emitted):
    error = CuaDriverError.from_result({"code": "background_pointer_failed", "effect": effect}, input_call=True)
    assert error.input_emitted is emitted


def test_partial_scroll_preserves_prior_input(session):
    with CuaBackendContext(session) as backend:
        original = backend.driver.call
        scrolls = []
        def call(name, args, **kwargs):
            if name == "scroll":
                scrolls.append(args)
                if len(scrolls) == 2:
                    raise CuaDriverError("background_pointer_failed", input_emitted=False)
            return original(name, args, **kwargs)
        backend.driver.call = call
        with pytest.raises(CuaDriverError) as caught:
            backend.scroll(125, 240, 0, 51)
        assert caught.value.input_emitted is True
        assert [r["amount"] for r in scrolls] == [50, 1]


def test_hyprland_native_address_and_pid(session):
    from ccdesk.backends.wayland import WaylandBackend

    clients = [{"address": "0x00000020", "title": "Agent", "class": "gtk", "pid": 1234,
                "at": [100, 200], "size": [400, 300], "workspace": {"id": 1}, "focusHistoryID": 0}]
    commands = []
    def run(argv, **kwargs):
        commands.append(argv)
        if argv == ["hyprctl", "clients", "-j"]:
            return json.dumps(clients)
        if argv == ["hyprctl", "-j", "monitors"]:
            return json.dumps([{"activeWorkspace": {"id": 1}}])
        if argv == ["hyprctl", "-j", "activeworkspace"]:
            return json.dumps({"id": 1})
        pytest.fail(f"Unexpected session command: {argv}")
    native = WaylandBackend(env=session.env | {"HYPRLAND_INSTANCE_SIGNATURE": "fake", "WAYLAND_DISPLAY": "fake"},
                            runner=run, which=lambda n: "/usr/bin/hyprctl" if n == "hyprctl" else None)
    backend = CuaDriverBackend(native, session.binary)
    try:
        backend.activate_window(native.list_windows()[0])
        backend.click(125, 240, 1, 1)
        backend.type_text("hello")
        assert input_calls(session)[0]["arguments"] == {"pid": 1234, "window_id": 32, "x": 125, "y": 240,
                                                       "coordinate_frame": "desktop", "delivery_mode": "background",
                                                       "button": "left", "count": 1}
        clients[0]["pid"] = 9999
        with pytest.raises(CuaDriverError) as caught:
            backend.type_text("wrong owner")
        assert caught.value.input_emitted is False
        assert backend.driver.proc.args == [session.binary, "mcp", "--direct"]
        assert backend.driver.version == "cua-driver 0.38.0"
    finally:
        backend.close()


def test_kde_native_pid_and_unique_window_join(session):
    from ccdesk.backends.kde import KdeBackend, WINDOWS_SCRIPT

    raw = [{"id": "{native-uuid}", "title": "Agent", "cls": "gtk", "pid": 1234,
            "x": 100, "y": 200, "width": 400, "height": 300}]
    def run(argv, **kwargs):
        if argv == ["kdotool", "get_desktop"]:
            return "1"
        assert argv == ["kdotool", "kwinscript", "--inline", WINDOWS_SCRIPT]
        return json.dumps(raw)
    native = KdeBackend(env=session.env | {"WAYLAND_DISPLAY": "fake"}, runner=run,
                        which=lambda n: "/usr/bin/kdotool" if n == "kdotool" else None)
    backend = CuaDriverBackend(native, session.binary)
    try:
        backend.activate_window(native.list_windows()[0])
        backend.type_text("hello")
        assert input_calls(session)[0]["arguments"]["window_id"] == 32
        raw[0]["pid"] = 9999
        with pytest.raises(CuaDriverError):
            backend.type_text("wrong owner")
    finally:
        backend.close()


def test_keys_after_adopting_tree_window_send_no_focus(backend, session, tmp_path):
    # click_element/set_value adopt the tree's window without activate_window(): the backend's
    # own target is still unset, so the keys need focus_keyboard. With cua-driver that only
    # addresses the window; it must not fall through to the session's activation.
    desk = Desktop(backend=backend, agent="agent", audit=AuditLog(tmp_path / "audit"), shots_dir=tmp_path)
    desk._target = session.windows[0]
    result = desk.press_key("ctrl+a")
    assert not [c for c in session.calls if c[0] in ("activate", "focus")]
    assert session.active == "user-window"
    assert input_calls(session)[-1] == {"name": "press_key", "arguments": {
        "pid": 1234, "window_id": 32, "delivery_mode": "background", "key": "a", "modifiers": ["ctrl"]}}
    assert "XISetFocus" not in str(result.get("note"))


def test_a_refusal_before_sending_says_so_and_does_not_send_to_doctor(session):
    """The bench's 10 background_unavailable calls (scroll, click, drag, press_key on Electron,
    LibreOffice and Tk) came back without an effect, so they were reported as input_emitted=true,
    with "Check ccdesk doctor" (which says background input is available)."""
    session.env["FAKE_FAILURE"] = json.dumps({"ok": False, "code": "background_unavailable",
                                            "reason": "toolkit drops routed pointer events"})
    with CuaBackendContext(session) as backend:
        backend.activate_window(session.windows[0])
        for call, tool in ((lambda: backend.scroll(125, 240, 0, 3), "scroll"),
                           (lambda: backend.drag(120, 230, 190, 290, 300), "drag"),
                           (lambda: backend.press_key(parse_key_combo("Tab")), "press_key")):
            with pytest.raises(CuaDriverError) as caught:
                call()
            text = str(caught.value)
            assert caught.value.code == "background_unavailable" and caught.value.input_emitted is False
            assert f"refused this {tool} in the background before sending anything" in text
            assert "toolkit drops routed pointer events" in text and "ui_tree()" in text
            assert "doctor" not in text and "foreground" not in text


def test_an_unconfirmed_background_unavailable_stays_emitted(session):
    session.env["FAKE_FAILURE"] = json.dumps({"ok": False, "code": "background_unavailable",
                                            "effect": "unverifiable",
                                            "reason": "bring the window to the foreground"})
    with CuaBackendContext(session) as backend:
        with pytest.raises(CuaDriverError) as caught:
            backend.click(125, 240, 1, 1)
    text = str(caught.value)
    assert caught.value.input_emitted is True and "may have reached the app" in text
    assert "foreground" not in text and "doctor" not in text  # the driver's reason was foreground advice


def test_an_unaddressable_window_names_it(session):
    session.env["FAKE_WINDOWS"] = "[]"  # read by the driver when it starts
    with CuaBackendContext(session) as backend:
        backend.activate_window(session.windows[0])
        with pytest.raises(CuaDriverError) as caught:
            backend.type_text("x")
    assert "lists no window for 'Agent'" in str(caught.value) and caught.value.input_emitted is False
