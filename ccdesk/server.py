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
returned origin. Save tokens: try ui_tree() first (text, no pixels) and click_element(ref).
After a ui_tree(), actions return `changes` (the lines that changed in that window): use it to
verify instead of another ui_tree() or screenshot. Refs stay valid across ui_tree() calls.
Without a tree, actions return screen_changed (and changed_area): if false, the action did
nothing yet. wait_for_change() waits for a page or dialog instead of wait() + screenshot.
Use screenshot(scale=0.5) to orient and small regions to verify.
Never type passwords or secrets; never dismiss dialogs you have not read.
Destructive shortcuts (Alt+F4, Ctrl+Q, Ctrl+W, Super+Q) need confirm=true."""
HEADLESS = """
This server works on its own invisible screen, not the user's: it starts empty, so launch()
the apps you need. Browsers need their own profile, e.g. `firefox --no-remote --profile DIR`
or `chromium --user-data-dir=DIR --force-renderer-accessibility` (create DIR first)."""

TOOL_NAMES = (
    "screenshot", "click", "move", "drag", "scroll", "type_text", "press_key",
    "list_windows", "activate_window", "clipboard", "wait", "doctor", "launch",
    "ui_tree", "click_element", "wait_for_change",
)


def build_server(desktop: Desktop) -> MCPServer:
    kwargs = {"name": "ccdesk", "instructions": INSTRUCTIONS + (HEADLESS if desktop.headless else "")}
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
    def click(x: int, y: int, button: Literal["left", "middle", "right"] = "left", count: int = 1,
              force: bool = False) -> dict:
        """Move the pointer to (x, y) in screen pixels and click. count=2 for a double click.
        After activate_window(), refuses if (x, y) is over another window; force=true clicks anyway."""
        return desktop.click(x, y, button=button, count=count, force=force)

    @tool
    def move(x: int, y: int) -> dict:
        """Move the pointer to (x, y) without clicking (e.g. to reveal a hover menu or tooltip)."""
        return desktop.move(x, y)

    @tool
    def drag(from_x: int, from_y: int, to_x: int, to_y: int, duration_ms: int = 400, force: bool = False) -> dict:
        """Press the left button at (from_x, from_y), move smoothly to (to_x, to_y), release."""
        return desktop.drag(from_x, from_y, to_x, to_y, duration_ms=duration_ms, force=force)

    @tool
    def scroll(x: int, y: int, dx: int = 0, dy: int = 0, force: bool = False) -> dict:
        """Scroll the wheel with the pointer at (x, y). dy > 0 scrolls down, dx > 0 right. Units are wheel clicks."""
        return desktop.scroll(x, y, dx=dx, dy=dy, force=force)

    @tool
    def type_text(text: str, submit: bool = False, replace: bool = False, force: bool = False) -> dict:
        """Type text into the focused widget, as if from the keyboard. Click the field first. Never type secrets.
        replace=true selects all (ctrl+a) first, so the text replaces the field's contents.
        submit=true presses Return afterwards (a trailing newline does the same).
        After activate_window(), refuses if focus moved to another window; force=true types anyway."""
        return desktop.type_text(text, submit=submit, replace=replace, force=force)

    @tool
    def press_key(key: str, modifiers: list[str] | None = None, confirm: bool = False, force: bool = False) -> dict:
        """Press a key or combo: key="Return", key="ctrl+l", or key="t" with modifiers=["ctrl","shift"].
        Alt+F4, Ctrl+Q, Ctrl+W and Super+Q are refused unless confirm=true. After activate_window(),
        refuses if focus moved to another window; force=true presses anyway."""
        return desktop.press_key(key, modifiers=modifiers, confirm=confirm, force=force)

    @tool
    def list_windows() -> dict:
        """List top-level windows: id, title, class and geometry."""
        return desktop.list_windows()

    @tool
    def activate_window(title_or_id: str) -> dict:
        """Raise and focus a window by id (from list_windows) or by (partial, case-insensitive) title.
        Later clicks, keys and typing are refused if they would reach another window (force=true overrides)."""
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
    def click_element(ref: str, button: Literal["left", "middle", "right"] = "left", count: int = 1,
                      force: bool = False) -> dict:
        """Click the centre of an element from the latest ui_tree() result, e.g. ref="e12".
        Fails if the element changed or left the screen; call ui_tree() again then."""
        return desktop.click_element(ref, button=button, count=count, force=force)

    @tool
    def clipboard(action: Literal["get", "set"] = "get", text: str | None = None) -> dict:
        """Read the clipboard (action="get") or replace its contents (action="set", text=...)."""
        return desktop.clipboard(action=action, text=text)

    @tool
    def wait(seconds: float) -> dict:
        """Sleep for up to 60 seconds, e.g. while a page loads. Screenshot afterwards to check."""
        return desktop.wait(seconds)

    @tool
    def wait_for_change(region: str | None = None, timeout: float = 10, settle_ms: int = 500) -> dict:
        """Wait (up to timeout seconds, max 300) until the screen changes from how it looked right
        after your last action, then until it is still for settle_ms. Use instead of wait() + screenshot
        to learn when a page or dialog is done. region "x,y,width,height" limits what is watched.
        Returns changed, settled, waited_ms and changed_area; screenshot that area to see it."""
        return desktop.wait_for_change(region=region, timeout=timeout, settle_ms=settle_ms)

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
