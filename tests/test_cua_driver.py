from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from ccdesk import cli
from ccdesk.backends.cua_driver import CuaDriverBackend, Driver, HOVER_NOTE, report
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
    assert input_calls(session) == [
        {"name": "click", "arguments": address | {"x": 25, "y": 40, "button": "right", "count": 2}},
        {"name": "drag", "arguments": address | {"from_x": 20, "from_y": 30, "to_x": 90, "to_y": 90,
                                                   "duration_ms": 450}},
        {"name": "scroll", "arguments": address | {"x": 40, "y": 60, "direction": "down", "amount": 3, "by": "line"}},
        {"name": "scroll", "arguments": address | {"x": 40, "y": 60, "direction": "left", "amount": 2, "by": "line"}},
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
    session.hidden.add(session.windows[0].id)
    with pytest.raises(TargetHiddenError, match="private screen"):
        backend.activate_window(session.windows[0])
    session.hidden.clear()
    session.desktop = 1
    with pytest.raises(TargetHiddenError):
        backend.activate_window(session.windows[0])
    assert not session.calls


def test_x11_targeting_never_uses_pointer_fallback(backend, session, monkeypatch):
    session.name = "x11"
    monkeypatch.setattr(session, "_xq", lambda: None, raising=False)
    monkeypatch.setattr(session, "window_at", lambda *_: pytest.fail("pointer-warp fallback called"))
    with pytest.raises(UnsupportedError, match="libX11"):
        backend.click(125, 240, 1, 1)
    class Query:
        def window_at(self, x, y):
            assert (x, y) == (125, 240)
            return 32
    monkeypatch.setattr(session, "_xq", Query)
    backend.click(125, 240, 1, 1)
    assert input_calls(session)[0]["arguments"]["x"] == 25


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
    with pytest.raises(CuaDriverError, match="Background delivery is unavailable"):
        CuaDriverBackend(session, session.binary)
    assert report(session, session.binary)["available"] is False
    with pytest.raises(CommandError, match="missing"):
        CuaDriverBackend(session, "/missing/cua-driver")


def test_unsupported_version_fails_clearly(session):
    session.env["FAKE_VERSION"] = "0.28.2"
    with pytest.raises(CommandError, match="0.28.2"):
        CuaDriverBackend(session, session.binary)


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
