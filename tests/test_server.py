"""The MCP server: exact tool names, and a real JSON-RPC session over stdio."""

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from ccdesk.server import TOOL_NAMES, build_server  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SPEC_TOOLS = {"screenshot", "click", "move", "drag", "scroll", "type_text", "press_key", "list_windows",
              "activate_window", "clipboard", "wait", "doctor", "launch", "ui_tree", "click_element"}


def test_registered_tools(make_desk):
    import anyio

    server = build_server(make_desk())
    tools = anyio.run(server.list_tools)
    # mcp 1.x calls it inputSchema, 2.x input_schema; the wire format is inputSchema in both.
    by_name = {t.name: t.model_dump(by_alias=True, mode="json") for t in tools}
    assert set(by_name) == SPEC_TOOLS == set(TOOL_NAMES)
    props = {name: set(t["inputSchema"]["properties"]) for name, t in by_name.items()}
    assert props["screenshot"] == {"region", "path", "scale"}
    assert props["click"] == {"x", "y", "button", "count", "force"}
    assert props["drag"] == {"from_x", "from_y", "to_x", "to_y", "duration_ms", "force"}
    assert props["scroll"] == {"x", "y", "dx", "dy", "force"}
    assert props["type_text"] == {"text", "submit", "replace", "force"}
    assert props["press_key"] == {"key", "modifiers", "confirm", "force"}
    assert props["clipboard"] == {"action", "text"}
    assert props["activate_window"] == {"title_or_id"}
    assert props["ui_tree"] == {"window", "query", "page", "max_nodes"}
    assert props["click_element"] == {"ref", "button", "count", "force"}
    assert by_name["click"]["inputSchema"]["required"] == ["x", "y"]


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
    assert "waited" in _text(ok) or ok.get("structuredContent", {}).get("waited") == 0

    denied = client.call("click", {"x": 1, "y": 1})
    assert denied.get("isError") is True
    assert "read-only" in _text(denied)

    refused = client.call("launch", {"command": "true"})
    assert refused.get("isError") is True

    report = client.call("doctor", {})
    assert not report.get("isError")
    assert '"read_only": true' in _text(report).replace("True", "true") or \
        report.get("structuredContent", {}).get("policy", {}).get("read_only") is True

    log = [json.loads(line) for line in (tmp_path / "audit.log").read_text().splitlines()]
    assert [e["tool"] for e in log] == ["server_start", "wait", "click", "launch", "doctor"]
    assert [e["status"] for e in log] == ["ok", "ok", "denied", "denied", "ok"]
