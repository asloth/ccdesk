"""MCP server over stdio. The only module that imports the third-party `mcp` package."""

from __future__ import annotations

import functools
import inspect
from typing import Literal

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer
    from mcp.server.fastmcp.exceptions import ToolError

from ccdesk import __version__
from ccdesk.desktop import Desktop
from ccdesk.errors import CcdeskError

INSTRUCTIONS = """\
ccdesk drives the local Linux desktop. Work in a loop: screenshot(), Read the PNG it returns,
do ONE action, then screenshot() again to verify before the next action. Coordinates are
screen pixels in the screenshot's space (origin top-left). For region screenshots add the
returned origin. Save tokens: try ui_tree() first (text, no pixels) and click_element(ref);
use screenshot(scale=0.5) to orient and small regions to verify.
Never type passwords or secrets; never dismiss dialogs you have not read.
Destructive shortcuts (Alt+F4, Ctrl+Q, Ctrl+W, Super+Q) need confirm=true."""

TOOL_NAMES = (
    "screenshot", "click", "move", "drag", "scroll", "type_text", "press_key",
    "list_windows", "activate_window", "clipboard", "wait", "doctor", "launch",
    "ui_tree", "click_element",
)


def build_server(desktop: Desktop) -> MCPServer:
    kwargs = {"name": "ccdesk", "instructions": INSTRUCTIONS}
    if "version" in inspect.signature(MCPServer.__init__).parameters:  # only mcp >= 2.0
        kwargs["version"] = __version__
    server = MCPServer(**kwargs)

    def tool(fn):
        @functools.wraps(fn)
        def reporting(*args, **kw):
            try:
                return fn(*args, **kw)
            except CcdeskError as exc:
                # ToolError messages reach the model; other exceptions are masked by the SDK.
                raise ToolError(str(exc)) from exc

        return server.tool()(reporting)

    @tool
    def screenshot(region: str | None = None, path: str | None = None, scale: float = 1.0) -> dict:
        """Capture the screen (or region "x,y,width,height") to a PNG. Returns path, width, height
        and approx_tokens (what reading it costs). scale=0.5 halves each side for ~4x fewer tokens;
        then divide image pixels by scale to get screen pixels. Read the returned path to see it."""
        return desktop.screenshot(region=region, path=path, scale=scale)

    @tool
    def click(x: int, y: int, button: Literal["left", "middle", "right"] = "left", count: int = 1) -> dict:
        """Move the pointer to (x, y) in screen pixels and click. count=2 for a double click."""
        return desktop.click(x, y, button=button, count=count)

    @tool
    def move(x: int, y: int) -> dict:
        """Move the pointer to (x, y) without clicking (e.g. to reveal a hover menu or tooltip)."""
        return desktop.move(x, y)

    @tool
    def drag(from_x: int, from_y: int, to_x: int, to_y: int, duration_ms: int = 400) -> dict:
        """Press the left button at (from_x, from_y), move smoothly to (to_x, to_y), release."""
        return desktop.drag(from_x, from_y, to_x, to_y, duration_ms=duration_ms)

    @tool
    def scroll(x: int, y: int, dx: int = 0, dy: int = 0) -> dict:
        """Scroll the wheel with the pointer at (x, y). dy > 0 scrolls down, dx > 0 right. Units are wheel clicks."""
        return desktop.scroll(x, y, dx=dx, dy=dy)

    @tool
    def type_text(text: str) -> dict:
        """Type text into the focused widget, as if from the keyboard. Click the field first. Never type secrets."""
        return desktop.type_text(text)

    @tool
    def press_key(key: str, modifiers: list[str] | None = None, confirm: bool = False) -> dict:
        """Press a key or combo: key="Return", key="ctrl+l", or key="t" with modifiers=["ctrl","shift"].
        Alt+F4, Ctrl+Q, Ctrl+W and Super+Q are refused unless confirm=true."""
        return desktop.press_key(key, modifiers=modifiers, confirm=confirm)

    @tool
    def list_windows() -> dict:
        """List top-level windows: id, title, class and geometry."""
        return desktop.list_windows()

    @tool
    def activate_window(title_or_id: str) -> dict:
        """Raise and focus a window by id (from list_windows) or by (partial, case-insensitive) title."""
        return desktop.activate_window(title_or_id)

    @tool
    def ui_tree(window: str | None = None, query: str | None = None, page: bool = False,
                max_nodes: int = 300) -> dict:
        """The on-screen elements of a window as text, from the accessibility tree: one line per
        button, field, checkbox, link, heading or text, with its value, states and screen box.
        Usually 5-20x cheaper than a screenshot. window: title substring (default: the active
        window). page=true: only the web page in a browser. query: only elements containing it.
        Act on a line with click_element(ref)."""
        return desktop.ui_tree(window=window, query=query, page=page, max_nodes=max_nodes)

    @tool
    def click_element(ref: str, button: Literal["left", "middle", "right"] = "left", count: int = 1) -> dict:
        """Click the centre of an element from the latest ui_tree() result, e.g. ref="e12".
        Fails if the element changed or left the screen; call ui_tree() again then."""
        return desktop.click_element(ref, button=button, count=count)

    @tool
    def clipboard(action: Literal["get", "set"] = "get", text: str | None = None) -> dict:
        """Read the clipboard (action="get") or replace its contents (action="set", text=...)."""
        return desktop.clipboard(action=action, text=text)

    @tool
    def wait(seconds: float) -> dict:
        """Sleep for up to 60 seconds, e.g. while a page loads. Screenshot afterwards to check."""
        return desktop.wait(seconds)

    @tool
    def doctor() -> dict:
        """Report session type, detected helper tools, screen size, scaling, and safety policy."""
        return desktop.doctor()

    @tool
    def launch(command: str) -> dict:
        """Start an application, e.g. "firefox https://example.org". Runs without a shell.
        Disabled unless the server was started with --allow-launch."""
        return desktop.launch(command)

    return server


def serve(desktop: Desktop) -> None:
    build_server(desktop).run()
