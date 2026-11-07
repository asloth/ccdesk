"""MCP server over stdio. The only module that imports the third-party `mcp` package."""

from __future__ import annotations

import functools
import inspect
import json
import os
from typing import Literal

try:  # mcp >= 2.0
    from mcp.server.mcpserver import Image, MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer
    from mcp.server.fastmcp import Image
    from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import TextContent, ToolAnnotations

from ccdesk import __version__
from ccdesk.desktop import Desktop
from ccdesk.errors import CcdeskError

INSTRUCTIONS = """\
ccdesk drives the local Linux desktop. Work in a loop: look, do ONE action, verify, repeat.
Look with ui_tree() first (text, no pixels) and act on a line with click_element(ref) or
set_value(ref, text). After a ui_tree(), actions return `changes` (the lines that changed in
that window): verify with it instead of another look. Refs stay valid across ui_tree() calls.
When a window has no useful tree, ui_tree() attaches a small picture of it (fallback_shot).
Fall back to screenshot() for canvas, images, video, apps that publish none,
to read layout, or to double-check something visual: the picture is in its answer. Coordinates
are screen pixels in the image (origin top-left); for a region add the returned origin.
screenshot(scale=0.5) halves each side for ~4x fewer tokens; regions are cheaper still.
Without a tree, actions return screen_changed (and changed_area): if false, the action did
nothing yet. Pass show_change=true to see the changed area in the answer, with no screenshot after.
wait_for_change() waits for a page or dialog instead of wait() + screenshot. Verify with small
regions, not the whole monitor: a screenshot result's `hint` says when you overpaid.
Never type passwords or secrets; never dismiss dialogs you have not read.
Destructive shortcuts (Alt+F4, Ctrl+Q, Ctrl+W, Super+Q) need confirm=true."""
HEADLESS = """
This server works on its own invisible screen, not the user's: it starts empty, so launch()
the apps you need. Browsers need their own profile, e.g. `firefox --no-remote --profile DIR`
or `chromium --user-data-dir=DIR --force-renderer-accessibility` (create DIR first)."""

TOOL_NAMES = (
    "screenshot", "click", "move", "drag", "scroll", "type_text", "press_key",
    "list_windows", "activate_window", "clipboard", "wait", "doctor", "launch",
    "ui_tree", "click_element", "set_value", "wait_for_change", "manage_window", "list_monitors",
)

MAX_PICTURE_BYTES = 3_500_000

# What each tool may do, for hosts deciding what to ask about. Hints, not enforcement: --read-only
# and the other checks in safety.py are what actually refuse.
OBSERVE = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
ARRANGE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
ACT = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)
ANNOTATIONS = {
    "screenshot": OBSERVE, "list_windows": OBSERVE, "list_monitors": OBSERVE, "ui_tree": OBSERVE,
    "wait": OBSERVE, "wait_for_change": OBSERVE, "doctor": OBSERVE,
    "move": ARRANGE, "scroll": ARRANGE, "activate_window": ARRANGE, "manage_window": ARRANGE,
    "clipboard": ARRANGE,
    "click": ACT, "click_element": ACT, "set_value": ACT, "drag": ACT, "type_text": ACT, "press_key": ACT, "launch": ACT,
}


def compact(result: dict) -> list:
    """One line of JSON: the SDK's default indents it, and the model pays for every space."""
    picture = result.pop("_picture", None)
    blocks = [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str))]
    return [*blocks, Image(path=picture).to_image_content()] if picture else blocks


def with_picture(result: dict) -> dict:
    """Put the screenshot in the answer: reading the file instead takes another request, and each
    request re-reads the whole context. Too big for the API (5 MB once in base64), it stays a file."""
    if os.path.getsize(result["path"]) <= MAX_PICTURE_BYTES:
        result["_picture"] = result["path"]
    else:
        result["note"] = "too big to show here: pass scale=0.5 or a region, or Read the path"
    return result


def with_tree_shot(result: dict) -> dict:
    """Show ui_tree's fallback_shot in the answer, as screenshot() does."""
    shot = result.get("fallback_shot")
    if isinstance(shot, dict) and shot.get("path") and os.path.getsize(shot["path"]) <= MAX_PICTURE_BYTES:
        result["_picture"] = shot["path"]
    return result


def with_change_shot(result: dict) -> dict:
    """Show the change_shot capture in the answer, as screenshot() does; it is small by construction."""
    shot = result.get("change_shot")
    if isinstance(shot, dict) and shot.get("path") and os.path.getsize(shot["path"]) <= MAX_PICTURE_BYTES:
        result["_picture"] = shot["path"]
    return result


def slim_schema(schema: dict) -> dict:
    """What the model needs from a generated input schema. The SDK adds a title to every field and
    spells an optional string as anyOf [string, null]; every session pays for those characters.
    Arguments are still validated against the function's own signature."""
    slim = {k: v for k, v in schema.items() if k != "title" and not (k == "default" and v is None)}
    options = slim.get("anyOf")
    if isinstance(options, list):
        real = [o for o in options if o != {"type": "null"}]
        if len(real) == 1:
            del slim["anyOf"]
            slim.update(slim_schema(real[0]))
    if isinstance(slim.get("items"), dict):
        slim["items"] = slim_schema(slim["items"])
    if isinstance(slim.get("properties"), dict):
        slim["properties"] = {name: slim_schema(field) for name, field in slim["properties"].items()}
    return slim


def slim_tools(server: MCPServer) -> None:
    """Slim every registered schema. This reaches into the SDK: if it moves, the server still
    works, with the longer schemas, and test_live_schemas_are_slim says so."""
    try:
        for registered in server._tool_manager._tools.values():
            registered.parameters = slim_schema(registered.parameters)
    except Exception:  # whatever a moved or frozen SDK raises: this is only a saving
        pass


def build_server(desktop: Desktop) -> MCPServer:
    kwargs = {"name": "ccdesk", "instructions": INSTRUCTIONS + (HEADLESS if desktop.headless else "")}
    if "version" in inspect.signature(MCPServer.__init__).parameters:  # only mcp >= 2.0
        kwargs["version"] = __version__
    server = MCPServer(**kwargs)

    def tool(fn):
        @functools.wraps(fn)
        def reporting(*args, **kw):
            try:
                return compact(fn(*args, **kw))
            except CcdeskError as exc:
                # ToolError messages reach the model; other exceptions are masked by the SDK.
                raise ToolError(str(exc)) from exc

        return server.tool(annotations=ANNOTATIONS[fn.__name__], structured_output=False)(reporting)

    @tool
    def screenshot(region: str | None = None, path: str | None = None, scale: float = 1.0,
                   window: str | None = None) -> dict:
        """Capture the screen (or region "x,y,width,height") and show it in this answer, with path,
        width, height and approx_tokens (what it costs). scale=0.5 halves each side for ~4x fewer
        tokens; then divide image pixels by scale to get screen pixels. path: only save it there.
        window: capture only that window, by id or title. It is a crop of the screen, so anything
        covering the window shows too: activate_window() it first."""
        result = desktop.screenshot(region=region, path=path, scale=scale, window=window)
        return result if path else with_picture(result)

    @tool
    def click(x: int, y: int, button: Literal["left", "middle", "right"] = "left", count: int = 1,
              force: bool = False, expect_title: str | None = None, show_change: bool = False) -> dict:
        """Move the pointer to (x, y) in screen pixels and click. count=2 for a double click.
        After activate_window(), refuses if (x, y) is over another window; force=true clicks anyway.
        expect_title: refuse unless the receiving window's title contains it (a browser tab).
        show_change=true: without a ui_tree, also show a small capture of what changed (~600 tokens at most)."""
        return with_change_shot(desktop.click(x, y, button=button, count=count, force=force,
                                       expect_title=expect_title, show_change=show_change))

    @tool
    def move(x: int, y: int) -> dict:
        """Move the pointer to (x, y) without clicking (e.g. to reveal a hover menu or tooltip)."""
        return desktop.move(x, y)

    @tool
    def drag(from_x: int, from_y: int, to_x: int, to_y: int, duration_ms: int = 400, force: bool = False,
             show_change: bool = False) -> dict:
        """Press the left button at (from_x, from_y), move smoothly to (to_x, to_y), release. show_change: as in click."""
        return with_change_shot(desktop.drag(from_x, from_y, to_x, to_y, duration_ms=duration_ms, force=force, show_change=show_change))

    @tool
    def scroll(x: int, y: int, dx: int = 0, dy: int = 0, force: bool = False, show_change: bool = False) -> dict:
        """Scroll the wheel with the pointer at (x, y). dy > 0 scrolls down, dx > 0 right. Units are wheel clicks.
        show_change: as in click."""
        return with_change_shot(desktop.scroll(x, y, dx=dx, dy=dy, force=force, show_change=show_change))

    @tool
    def type_text(text: str, submit: bool = False, replace: bool = False, force: bool = False,
                  expect_title: str | None = None, show_change: bool = False) -> dict:
        """Type text into the focused widget, as if from the keyboard. Click the field first. Never type secrets.
        replace=true selects all (ctrl+a) first, so the text replaces the field's contents.
        submit=true presses Return afterwards (a trailing newline does the same).
        After activate_window(), refuses if focus moved to another window; force=true types anyway.
        expect_title: refuse unless the focused window's title contains it (case-insensitive). A
        browser window's title names its active tab, so this catches a tab switch; force does not skip it.
        show_change: as in click."""
        return with_change_shot(desktop.type_text(text, submit=submit, replace=replace, force=force,
                                           expect_title=expect_title, show_change=show_change))

    @tool
    def press_key(key: str, modifiers: list[str] | None = None, confirm: bool = False, force: bool = False,
                  expect_title: str | None = None, show_change: bool = False) -> dict:
        """Press a key or combo: key="Return", key="ctrl+l", or key="t" with modifiers=["ctrl","shift"].
        Alt+F4, Ctrl+Q, Ctrl+W and Super+Q are refused unless confirm=true. After activate_window(),
        refuses if focus moved to another window; force=true presses anyway. expect_title: as in type_text.
        show_change: as in click."""
        return with_change_shot(desktop.press_key(key, modifiers=modifiers, confirm=confirm, force=force,
                                           expect_title=expect_title, show_change=show_change))

    @tool
    def list_windows() -> dict:
        """List top-level windows: id, title, class, geometry, desktop and whether it is the active
        one, plus current_desktop (the workspace on show)."""
        return desktop.list_windows()

    @tool
    def activate_window(title_or_id: str) -> dict:
        """Raise and focus a window by id (from list_windows) or by (partial, case-insensitive) title.
        Later clicks, keys and typing are refused if they would reach another window (force=true overrides)."""
        return desktop.activate_window(title_or_id)

    @tool
    def manage_window(title_or_id: str, action: Literal["move", "resize", "minimize", "maximize", "restore"],
                      x: int | None = None, y: int | None = None, width: int | None = None,
                      height: int | None = None) -> dict:
        """Move (x, y), resize (width, height), minimize, maximize or restore a window, by id or title.
        Returns before, after, on_show and changed; changed=false means its box and visibility stayed
        the same, so it may already have been like that or the request was ignored: check with a
        screenshot. On a tiling window manager, move and resize make the window float, and maximize
        may mean fullscreen."""
        return desktop.manage_window(title_or_id, action, x=x, y=y, width=width, height=height)

    @tool
    def list_monitors() -> dict:
        """List monitors (name, x, y, width, height) and the whole screen's size. Pass a monitor's
        box as screenshot(region="x,y,width,height") to see it at full resolution."""
        return desktop.list_monitors()

    @tool
    def ui_tree(window: str | None = None, query: str | None = None, page: bool = False,
                max_nodes: int = 300, shot_fallback: bool = True) -> dict:
        """The on-screen elements of a window as text, from the accessibility tree: one line per
        button, field, checkbox, link, heading or text, with its value, states and screen box.
        Usually 5-20x cheaper than a screenshot. window: title substring (default: the active
        window). page=true: only the web page in a browser. query: only elements containing it.
        Act on a line with click_element(ref). With no useful tree (window not on the accessibility
        bus, or almost empty) it attaches a small picture of the window; shot_fallback=false: never."""
        return with_tree_shot(desktop.ui_tree(window=window, query=query, page=page, max_nodes=max_nodes,
                                              shot_fallback=shot_fallback))

    @tool
    def click_element(ref: str, button: Literal["left", "middle", "right"] = "left", count: int = 1,
                      force: bool = False, expect_title: str | None = None) -> dict:
        """Click an element from the latest ui_tree() result, e.g. ref="e12". A single left click on
        a button, checkbox, link or menu item runs its accessibility action when it has one, without
        moving the pointer; otherwise it clicks the centre. Fails if the element changed or left the
        screen; call ui_tree() again then. expect_title: as in click, and always a real click."""
        return desktop.click_element(ref, button=button, count=count, force=force, expect_title=expect_title)

    @tool
    def set_value(ref: str, text: str, force: bool = False) -> dict:
        """Replace the value of a field, slider or spin button from ui_tree() in one call, and read it
        back: an answer means the app holds it. Refuses password fields. If the app ignores the write
        (web pages do), it clicks the field, selects all and types the text instead (typed: true)."""
        return desktop.set_value(ref, text, force=force)

    @tool
    def clipboard(action: Literal["get", "set"] = "get", text: str | None = None) -> dict:
        """Read the clipboard (action="get") or replace its contents (action="set", text=...)."""
        return desktop.clipboard(action=action, text=text)

    @tool
    def wait(seconds: float) -> dict:
        """Sleep for up to 300 seconds. To wait for a page or dialog, wait_for_change() returns as soon
        as it is done; use wait() for fixed delays (a verification that takes minutes)."""
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

    slim_tools(server)
    return server


def serve(desktop: Desktop) -> None:
    build_server(desktop).run()
