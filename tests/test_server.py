"""The MCP server: exact tool names, and a real JSON-RPC session over stdio."""

import asyncio
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from ccdesk.safety import INPUT_TOOLS  # noqa: E402
from ccdesk.server import TOOL_NAMES, build_server, slim_schema, slim_tools  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SPEC_TOOLS = {"screenshot", "click", "move", "drag", "scroll", "type_text", "press_key", "list_windows",
              "activate_window", "clipboard", "wait", "doctor", "launch", "ui_tree", "click_element", "set_value",
              "wait_for_change", "manage_window", "list_monitors", "batch"}


def test_slim_schema_keeps_what_the_model_needs():
    schema = {"title": "clickArguments", "type": "object", "required": ["x"], "properties": {
        "x": {"title": "X", "type": "integer"},
        "window": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Window"},
        "button": {"default": "left", "enum": ["left", "right"], "title": "Button", "type": "string"},
        "mods": {"anyOf": [{"items": {"type": "string", "title": "M"}, "type": "array"}, {"type": "null"}],
                 "default": None},
        "either": {"anyOf": [{"type": "string"}, {"type": "integer"}], "title": "Either"},
        "title": {"title": "A field that has this name", "type": "string"},
    }}
    assert slim_schema(schema) == {"type": "object", "required": ["x"], "properties": {
        "x": {"type": "integer"},
        "window": {"type": "string"},
        "button": {"default": "left", "enum": ["left", "right"], "type": "string"},
        "mods": {"items": {"type": "string"}, "type": "array"},
        "either": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
        "title": {"type": "string"},
    }}


def test_slim_tools_survives_an_sdk_that_moved():
    class Moved:
        _tool_manager = type("Manager", (), {"_tools": ["a list now"]})()
    slim_tools(Moved())
    slim_tools(object())


def test_live_schemas_are_slim(make_desk):
    def walk(schema):
        assert "title" not in schema and {"type": "null"} not in schema.get("anyOf", [])
        for field in schema.get("properties", {}).values():
            walk(field)
        if "items" in schema:
            walk(schema["items"])

    tools = asyncio.run(build_server(make_desk()).list_tools())
    tools = getattr(tools, "tools", tools)
    assert len(tools) == len(TOOL_NAMES)
    for tool in tools:
        walk(tool.model_dump(by_alias=True, mode="json")["inputSchema"])


def test_registered_tools(make_desk):
    import anyio

    server = build_server(make_desk())
    tools = anyio.run(server.list_tools)
    # mcp 1.x calls it inputSchema, 2.x input_schema; the wire format is inputSchema in both.
    by_name = {t.name: t.model_dump(by_alias=True, mode="json") for t in tools}
    assert set(by_name) == SPEC_TOOLS == set(TOOL_NAMES)
    props = {name: set(t["inputSchema"]["properties"]) for name, t in by_name.items()}
    assert props["screenshot"] == {"region", "path", "scale", "window"}
    assert props["manage_window"] == {"title_or_id", "action", "x", "y", "width", "height"}
    assert props["list_monitors"] == set()
    assert props["click"] == {"x", "y", "button", "count", "force", "expect_title", "show_change"}
    assert props["drag"] == {"from_x", "from_y", "to_x", "to_y", "duration_ms", "force", "show_change"}
    assert props["scroll"] == {"x", "y", "dx", "dy", "force", "show_change"}
    assert props["type_text"] == {"text", "submit", "replace", "force", "expect_title", "show_change"}
    assert props["press_key"] == {"key", "modifiers", "confirm", "force", "expect_title", "show_change"}
    assert props["clipboard"] == {"action", "text"}
    assert props["activate_window"] == {"title_or_id"}
    assert props["ui_tree"] == {"window", "query", "page", "max_nodes", "shot_fallback"}
    assert props["click_element"] == {"ref", "button", "count", "force", "expect_title"}
    assert props["set_value"] == {"ref", "text", "force"}
    assert props["wait_for_change"] == {"region", "timeout", "settle_ms"}
    assert by_name["click"]["inputSchema"]["required"] == ["x", "y"]


def test_every_tool_says_what_it_may_do(make_desk):
    import anyio

    tools = anyio.run(build_server(make_desk()).list_tools)
    hints = {t.name: t.model_dump(by_alias=True, mode="json")["annotations"] for t in tools}
    assert set(hints) == SPEC_TOOLS
    looks = {name for name, h in hints.items() if h["readOnlyHint"]}
    assert looks == {"screenshot", "list_windows", "list_monitors", "ui_tree", "wait", "wait_for_change", "doctor"}
    assert not looks & INPUT_TOOLS  # read-only mode and the hints agree
    for name in ("click", "click_element", "drag", "type_text", "press_key", "launch"):
        assert hints[name]["destructiveHint"] is True, name


def test_results_are_one_line_of_json(client):
    text = _text(client.call("wait", {"seconds": 0}))
    assert text == '{"waited":0.0}'


def test_screenshot_answers_with_the_picture(tmp_path, monkeypatch):
    from ccdesk import server
    from tests.pngutil import write_png

    png = tmp_path / "s.png"
    write_png(png, 4, 4)
    blocks = server.compact(server.with_picture({"path": str(png), "width": 4, "height": 4}))
    assert [b.type for b in blocks] == ["text", "image"]
    assert json.loads(blocks[0].text) == {"path": str(png), "width": 4, "height": 4}

    monkeypatch.setattr(server, "MAX_PICTURE_BYTES", 1)  # would be rejected, and break the session
    blocks = server.compact(server.with_picture({"path": str(png)}))
    assert [b.type for b in blocks] == ["text"] and "too big" in blocks[0].text


def test_change_shot_answers_with_its_picture(tmp_path):
    from ccdesk import server
    from tests.pngutil import write_png

    png = tmp_path / "c.png"
    write_png(png, 4, 4)
    blocks = server.compact(server.with_change_shot({"screen_changed": True, "change_shot": {"path": str(png)}}))
    assert [b.type for b in blocks] == ["text", "image"]
    blocks = server.compact(server.with_change_shot({"screen_changed": False}))
    assert [b.type for b in blocks] == ["text"]


def test_headless_adds_instructions(make_desk):
    from ccdesk.server import HEADLESS

    desk = make_desk()
    assert HEADLESS not in build_server(desk).instructions
    desk.headless = object()
    assert HEADLESS in build_server(desk).instructions


class StdioClient:
    def __init__(self, *args, env):
        self.proc = subprocess.Popen([sys.executable, "-m", "ccdesk", "mcp", *args], cwd=ROOT, env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.next_id = 0

    def send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def request(self, method, params=None, timeout=20):
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.proc.stdout], [], [], 0.5)
            if not ready:
                if self.proc.poll() is not None:
                    pytest.fail(f"server died: {self.proc.stderr.read().decode()}")
                continue
            line = self.proc.stdout.readline()
            if not line:
                pytest.fail(f"server closed stdout: {self.proc.stderr.read().decode()}")
            msg = json.loads(line)
            if msg.get("id") == self.next_id:
                return msg
        pytest.fail(f"no response to {method}")

    def call(self, name, args):
        msg = self.request("tools/call", {"name": name, "arguments": args})
        assert "result" in msg, msg
        return msg["result"]

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture
def client(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY", "CCDESK_BACKEND")}
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    c = StdioClient("--read-only", "--audit-log", str(tmp_path / "audit.log"), env=env)
    init = c.request("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "ccdesk-test", "version": "0"},
    })
    assert init["result"]["serverInfo"]["name"] == "ccdesk"
    c.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    yield c
    c.close()


def _text(result):
    return " ".join(part.get("text", "") for part in result.get("content", []))


def test_stdio_session(client, tmp_path):
    names = {t["name"] for t in client.request("tools/list")["result"]["tools"]}
    assert names == SPEC_TOOLS

    ok = client.call("wait", {"seconds": 0})
    assert not ok.get("isError")
    assert json.loads(_text(ok)) == {"waited": 0.0}

    denied = client.call("click", {"x": 1, "y": 1})
    assert denied.get("isError") is True
    assert "read-only" in _text(denied)

    refused = client.call("launch", {"command": "true"})
    assert refused.get("isError") is True

    report = client.call("doctor", {})
    assert not report.get("isError")
    assert json.loads(_text(report))["policy"]["read_only"] is True

    log = [json.loads(line) for line in (tmp_path / "audit.log").read_text().splitlines()]
    assert [e["tool"] for e in log] == ["server_start", "wait", "click", "launch", "doctor"]
    assert [e["status"] for e in log] == ["ok", "ok", "denied", "denied", "ok"]


def test_ui_tree_fallback_shot_becomes_a_picture(make_desk, helper, fake):
    from dataclasses import replace

    fake.windows[0] = replace(fake.windows[0], title="Form — Mozilla Firefox")
    import anyio
    from mcp.types import ImageContent

    helper.nodes = {"0": {"path": "0", "role": "frame", "name": "Slack", "depth": 0, "showing": True,
                          "box": [0, 0, 800, 600]}}
    server = build_server(make_desk())

    async def call():
        return await server.call_tool("ui_tree", {})

    blocks = anyio.run(call).content
    assert any(isinstance(b, ImageContent) for b in blocks)
    assert "fallback_shot" in blocks[0].text and "_picture" not in blocks[0].text
