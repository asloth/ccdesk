"""The action layer: validation, safety policy and audit around a backend.

Everything the MCP server exposes is a method here, so it can be tested without MCP.
"""

from __future__ import annotations

import inspect
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from ccdesk.a11y import NATIVE_ROLES, A11y, WindowNotFound, bus_enabled, missing_hint
from ccdesk.backends import Backend, get_backend
from ccdesk.errors import CcdeskError, PolicyError, UnsupportedError, ValidationError
from ccdesk.parsing import (
    KeyCombo,
    Region,
    Window,
    check_int,
    check_point,
    check_scale,
    moves_focus,
    parse_button,
    parse_key_combo,
    parse_region,
    parse_window_ref,
    png_size,
)
from ccdesk.safety import INPUT_TOOLS, AuditLog, Policy, _redact, cache_dir
from ccdesk.screendiff import Grid, changed_box
from ccdesk.seat import SeatLock, seat_key

# Input tools that wait for the user with --yield-ms (launch and clipboard never did).
YIELDING_TOOLS = INPUT_TOOLS - {"launch", "clipboard_set"}

MAX_TEXT = 10_000
MAX_WAIT = 300.0
MAX_WAIT_FOR_CHANGE = 300.0
BATCH_TOOLS = ("click_element", "set_value", "type_text", "press_key", "click", "scroll", "move", "drag",
               "activate_window", "wait")
MAX_BATCH_STEPS = 20
MAX_BATCH_WAIT = 5.0  # seconds per wait step: the seat is held meanwhile
DIFF_CELL = 8  # screen pixels per cell when comparing before/after captures
POLL = 0.2  # seconds between captures in wait_for_change
MAX_YIELD = 15.0  # seconds input waits for the user to pause (--yield-ms) before it is refused
OWN_INPUT_SLACK_MS = 150  # xdotool's events can land this long after it returns
KEEP_SCREENSHOTS = 200
SETTLE = 0.3  # seconds the helper watches for a change before an action reports "no change"
KEY_SETTLE = 0.15  # between select-all, the text and Return in type_text
FOCUS_SETTLE = 0.3  # after a shortcut that moves focus (ctrl+t, ctrl+l, ...)
SELECT_ALL = KeyCombo("a", ("ctrl",))
RETURN = KeyCombo("Return")
DELETE = KeyCombo("Delete")
TYPED_POLLS = 5  # reads, POLL apart, for a typed value to reach the accessibility tree
WINDOW_ACTIONS = ("move", "resize", "minimize", "maximize", "restore")
MAX_WINDOW_COORD = 65_535
WINDOW_POLLS = 8  # reads, POLL apart, for a window manager to finish before calling a request ignored
SHOT_PAD = 24  # screen pixels around changed_area in a change_shot
SHOT_MAX_TOKENS = 600  # a bigger change_shot is scaled down to about this
TREE_SHOT_MAX_TOKENS = 1500  # the picture ui_tree attaches when the window has no useful tree
HINT_MIN_TOKENS = 400  # a screenshot cheaper than this gets no "crop it" hint
DIFF_TOOLS = frozenset({"click", "drag", "scroll", "type_text", "press_key"})  # report screen_changed


def shown(value) -> str:
    """A field's text as a tree read shows it: whitespace collapsed, cut at 200 characters."""
    text = " ".join(str(value or "").split())
    return text[:200].removesuffix("…")


TREE_SHOT_NOTE = ("A picture of the window is attached: work from it. Screen pixel = "
                  "origin + pixel/scale")
# Roles that count as something to act on (the same idea as ACTIONABLE in a11y_helper.py).
ACTIONABLE_ROLES = frozenset({
    "push button", "toggle button", "button", "entry", "password text", "text", "check box",
    "radio button", "combo box", "link", "menu item", "check menu item", "radio menu item",
    "page tab", "spin button", "slider", "list item", "tree item", "switch",
})


def tree_is_empty(text: str) -> bool:
    """A tree too thin to work from: fewer than 3 lines and none with a role to act on. An
    Electron or canvas window gives just its frame and title (0-2 lines, no button or field),
    while the smallest real dialog has a message and an OK (2 lines, one a button)."""
    lines = [ln.split(None, 1)[1] for ln in text.splitlines() if ln.strip() and len(ln.split(None, 1)) == 2]
    if len(lines) >= 3:
        return False
    return not any(ln.startswith(role + " ") or ln == role for ln in lines for role in ACTIONABLE_ROLES)


class Desktop:
    def __init__(self, backend: Backend | None = None, policy: Policy | None = None,
                 audit: AuditLog | None = None, shots_dir: str | os.PathLike | None = None,
                 a11y: A11y | None = None, headless=None, screen_diff: bool = True,
                 agent: str | None = None, seat: SeatLock | None = None, cursor=None):
        self.backend = backend or get_backend()
        # With several agents on one screen: this one's name, and the lock they all share.
        self.agent = agent
        # An own-pointer agent does not use the user's pointer or keyboard: its seat is its own.
        own = self.backend.own_pointer and bool(agent)
        self.seat = seat or SeatLock(seat_key(self.backend.name, self.backend.env, agent if own else None))
        # The agent's drawn cursor (X11 only, and only with an agent name): a visual, moved after each input.
        self.cursor = cursor if self.agent and self.backend.name == "x11" else None
        self.a11y = a11y or A11y()
        self.a11y.locate = self.backend.window_origin
        self.headless = headless  # ccdesk.headless.Headless when the agent has its own screen
        if headless is not None:
            self.a11y.env = self.backend.env
        self.policy = policy or Policy()
        self.audit = audit or AuditLog()
        self.shots_dir = Path(shots_dir) if shots_dir else cache_dir() / "screenshots"
        self._screen: tuple[int, int] | None = None
        self._target: Window | None = None  # the window from the latest activate_window()
        # Pixel comparison when there is no accessibility tree: the receiving window before the
        # current action, and the screen after the latest one (wait_for_change's starting point).
        self.screen_diff = screen_diff
        self._pending: tuple[Region | None, Grid] | None = None
        self._after: tuple[Region | None, Grid] | None = None
        # What the latest action changed, for the "screenshot just this" hint; and the tool before
        # this one, for the "wait_for_change instead of wait" hint.
        self._last_area: Region | None = None
        self._prev_tool: str | None = None
        # Said once, when the first screenshot pays for the whole screen before any tree was read.
        self._tree_hinted = False
        self.clock = time.monotonic
        self._own_input: float | None = None  # clock() when ccdesk's latest input action finished
        self._yielded = 0  # ms the current action waited for the user (--yield-ms)
        self._yield_done: int | None = None  # set while an action runs that already waited, before the seat
        self._cursor_next: tuple[int, int] | None = None  # where the drawn cursor goes when the action ends
        self._busy = threading.RLock()  # one input action of this server at a time, observation included
        self._batching = False  # inside batch(): steps keep the first baseline and re-check password focus
        self._deferring = False  # inside an input action: _changes() leaves the observing to _act
        self._deferred: tuple[dict, bool] | None = None  # what _act observes once the seat is free

    # -- plumbing -------------------------------------------------------------

    def _act(self, tool: str, args: dict, fn, policy_tool: str | None = None, loggable=None):
        start = time.monotonic()
        try:
            self.policy.check(policy_tool or tool)
            if (policy_tool or tool) in INPUT_TOOLS:
                # Wait for the user before taking the seat: a human at the mouse must not stall the
                # other agents too. The action's own _yield_to_user() calls then return this result.
                # _busy keeps this server's own calls (MCP runs each on a worker thread) from sharing
                # the state below while one of them observes outside the seat.
                with self._busy:
                    result = self._seated(tool, fn)
            else:
                result = fn()
        except PolicyError as exc:  # refused before any input was sent
            self.audit.record(tool, args, "denied", error=str(exc))
            raise
        except CcdeskError as exc:
            self._input_done(tool)
            self.audit.record(tool, args, "error", error=str(exc), duration_ms=(time.monotonic() - start) * 1000)
            raise
        except Exception as exc:
            self._input_done(tool)
            self.audit.record(tool, args, "error", error=f"{type(exc).__name__}: {exc}",
                              duration_ms=(time.monotonic() - start) * 1000)
            raise
        self._input_done(tool)
        self._prev_tool = tool
        self.audit.record(tool, args, "ok", result=loggable(result) if loggable else result,
                          duration_ms=(time.monotonic() - start) * 1000)
        return result

    def _show_cursor(self, x: int, y: int) -> None:
        """Move the drawn cursor once the action is over: after _observe() has compared the window,
        so its own arrow never counts as the app changing."""
        if self.cursor is not None:
            self._cursor_next = (x, y)

    def _flush_cursor(self) -> None:
        self._yield_done = None
        point, self._cursor_next = self._cursor_next, None
        if point is not None and self.cursor is not None:
            self.cursor.move(*point)

    def _cursor_to_node(self, node: dict | None) -> None:
        if self.cursor is not None and node and node.get("box"):
            x, y, w, h = node["box"]
            self._show_cursor(*self._point(x + w // 2, y + h // 2))

    def _user_idle_ms(self) -> float | None:
        """How long the user has left mouse and keyboard alone, or None when it can't be told.
        X's idle counter is reset by every agent's xdotool events too, so input that came right
        after an agent's (this one's or another's on the same screen) counts as theirs."""
        try:
            idle = self.backend.idle_ms()
        except CcdeskError:
            return None
        if idle is None:
            return None
        agents = [t for t in (self._own_input, self.seat.last_input()) if t is not None and t == t]
        if agents:
            since_agents = (self.clock() - max(agents)) * 1000
            if idle >= since_agents - OWN_INPUT_SLACK_MS:
                return float("inf")  # nothing since the agents' input: the user is away
        return idle

    def _still_idle(self) -> None:
        """One look, no waiting: _act waited for the user before queueing for the seat, and the
        wait for another agent can be long."""
        need = self.policy.yield_ms
        idle = self._user_idle_ms() if need else None
        if idle is not None and idle < need:
            raise PolicyError(f"the user started using the mouse or keyboard ({idle:.0f} ms ago) while this "
                              f"agent waited for another one; nothing was sent. wait() and retry")

    def _input_done(self, tool: str) -> None:
        if tool in INPUT_TOOLS:
            self._own_input = self.clock()

    def _yield_to_user(self) -> int:
        """With --yield-ms, hold input while the user is using the mouse or keyboard: wait until
        they have been idle that long, up to MAX_YIELD. Returns how long it waited, in ms."""
        if self._yield_done is not None:
            return self._yield_done  # _act already waited, before taking the seat
        need = self.policy.yield_ms
        if not need:
            return 0
        waited = 0.0
        while True:
            idle = self._user_idle_ms()
            if idle is None or idle >= need:
                return round(waited * 1000)  # idle long enough, or cannot tell: don't block
            if waited >= MAX_YIELD:
                raise PolicyError(f"the user is using the mouse or keyboard (last input {idle} ms ago; "
                                  f"waited {waited:.0f}s for a {need} ms pause). Don't race them: wait() "
                                  f"and retry, or ask them to step away")
            pause = min(max((need - idle) / 1000, 0.1), MAX_YIELD - waited)
            self.backend.sleep(pause)
            waited += pause

    def _seated(self, tool: str, fn):
        """Run an input action: its input under the seat lock, then what it changed, observed once
        the seat is free so the other agents' input can go meanwhile."""
        try:
            if tool in YIELDING_TOOLS:
                self._yield_done = self._yield_to_user()
            outer = self._deferring
            deferred = None
            self._deferring = True
            try:
                with self.seat:  # the focus check and the input itself, with no other agent in between
                    try:
                        if self._yield_done is not None:
                            self._still_idle()  # the user may have come back while we queued
                        result = fn()
                    finally:
                        if tool in YIELDING_TOOLS:
                            self.seat.note_input(self.clock())
            finally:
                self._deferring = outer
                if not outer:  # a nested action (a step of batch) leaves it to the outermost one
                    deferred, self._deferred = self._deferred, None
            if deferred is not None:
                # Out of the seat: settling and re-reading the window only reads the screen,
                # so the other agents can send their input meanwhile.
                result = self._observe(*deferred)
        finally:
            if not self._deferring:
                self._yield_done = None  # a step inside batch keeps the wait the batch did
                self._flush_cursor()
        return result

    def _changes(self, result: dict, show_change: bool = False) -> dict:
        """What an action changed, filled in by _observe(). Inside an input action it runs after
        _act has released the seat lock; the result dict is filled in place then."""
        if self._deferring:
            self._deferred = (result, show_change)
            return result
        return self._observe(result, show_change)

    def _observe(self, result: dict, show_change: bool = False) -> dict:
        """After ui_tree() has been used, add what the action changed in that window. Without a
        tree, say whether the receiving window's pixels changed (see _guard and screendiff), and
        with show_change, attach a small capture of that area so no screenshot is needed to see it."""
        self._last_area = None
        if self.a11y._last is None:
            before, self._pending = self._pending, None
            if before is not None:
                region, grid = before
                self.backend.sleep(SETTLE)
                after = self._snapshot(region)
                if after is not None:
                    self._after = (region, after)
                    box = changed_box(grid, after)
                    result["screen_changed"] = box is not None
                    if box:
                        result["changed_area"] = dict(zip(("x", "y", "width", "height"), box))
                        self._last_area = Region(*box)
                        if show_change:
                            result["change_shot"] = self._change_shot(Region(*box))
            if show_change and "change_shot" not in result and "screen_changed" not in result:
                result["note"] = "show_change needs ImageMagick to see what changed; take a screenshot"
            return result
        try:
            changes = self.a11y.changes(settle=SETTLE)
        except CcdeskError as exc:
            changes = f"could not re-read the window: {exc}"
        if changes is not None:
            result["changes"] = changes
        return result

    def _padded(self, box: Region) -> Region:
        """box with SHOT_PAD around it, clipped to the screen."""
        x0, y0 = max(0, box.x - SHOT_PAD), max(0, box.y - SHOT_PAD)
        x1, y1 = box.x + box.width + SHOT_PAD, box.y + box.height + SHOT_PAD
        size = self.screen_size()
        if size:
            x1, y1 = min(x1, size[0]), min(y1, size[1])
        return Region(x0, y0, max(1, x1 - x0), max(1, y1 - y0))

    def _change_shot(self, box: Region) -> dict:
        """A capture of what an action changed, scaled down to about SHOT_MAX_TOKENS."""
        reg = self._padded(box)
        try:
            shot, factor = self._scaled_capture(reg, SHOT_MAX_TOKENS)
        except (CcdeskError, OSError, ValueError) as exc:
            return {"error": f"could not capture the change: {exc}"}
        if factor < 1:
            shot["scale"] = round(factor, 3)
        return shot

    def _scaled_capture(self, reg: Region, max_tokens: int) -> tuple[dict, float]:
        """Capture reg, scaled down to about max_tokens (tokens ~ width*height/750).
        Returns the shot (path, width, height, approx_tokens, origin) and the scale factor used."""
        out = self._shot_path(None)
        self.backend.screenshot(str(out), reg)
        width, height = png_size(out)
        factor = min(1.0, (max_tokens * 750 / (width * height)) ** 0.5)
        if factor < 1:
            self.backend.scale_png(str(out), max(1, round(width * factor)), max(1, round(height * factor)))
            width, height = png_size(out)
        return ({"path": str(out), "width": width, "height": height, "approx_tokens": round(width * height / 750),
                 "origin": {"x": reg.x, "y": reg.y}}, factor)

    def _tree_fallback_shot(self, title: str | None) -> dict:
        """A picture of one window for ui_tree() when it has no useful tree. Raises CcdeskError if
        the window cannot be found or captured."""
        if title is None:
            wid = self.backend.active_window()
            if wid is None:
                raise CcdeskError("no active window to capture")
            title = wid
        _, reg, _ = self._shown_window_region(title)
        shot, factor = self._scaled_capture(reg, TREE_SHOT_MAX_TOKENS)
        shot["scale"] = round(factor, 3) if factor < 1 else 1.0
        return shot

    def screen_size(self) -> tuple[int, int] | None:
        """Cached screen size, or None if it cannot be determined."""
        if self._screen is None:
            try:
                self.backend.ensure_available()
                self._screen = self.backend.screen_size()
            except CcdeskError:
                return None
        return self._screen

    def _point(self, x, y) -> tuple[int, int]:
        return check_point(x, y, self.screen_size())

    def _ready(self) -> None:
        self.backend.ensure_available()

    def _receiver(self, windows: dict[str, Window], point: tuple[int, int] | None) -> Window | None:
        """The window input is about to reach: the one under the pointer for pointer actions,
        else the focused one. None when the backend cannot tell."""
        try:
            wid = self.backend.window_at(*point) if point else None
            if wid in windows:
                return windows[wid]
            # A panel or the desktop is not listed either, but takes the click itself.
            shell = self.backend.shell_window(wid) if wid else None
            if shell is not None:
                return shell
            wid = self.backend.active_window()  # menus and dropdowns are not listed: they belong to the focus
            if wid in windows:
                return windows[wid]
            return self.backend.shell_window(wid) if wid else None  # e.g. a focused launcher's search box
        except CcdeskError:
            return None

    def _belongs_to_target(self, window: Window) -> bool:
        """The target itself, or one of its dialogs (a file chooser, a print dialog)."""
        wid = window.id
        for _ in range(4):
            if wid == self._target.id:
                return True
            try:
                wid = self.backend.transient_for(wid)
            except CcdeskError:
                return False
            if not wid:
                return False
        return False

    def _guard(self, force: bool, point: tuple[int, int] | None = None,
               expect_title: str | None = None) -> Window | None:
        """Refuse input when focus moved away from the window of the latest activate_window(), e.g.
        because the user switched to their terminal, or when the receiving window's title lacks
        expect_title (a browser tab: the window stays the same when the user switches tabs).
        Returns the receiving window for the result, and keeps a small copy of it for _changes()
        to compare with afterwards."""
        if expect_title is not None and (not isinstance(expect_title, str) or not expect_title.strip()):
            raise ValidationError("expect_title must be a non-empty string, or left out")
        self._yielded = self._yield_to_user()  # first: focus may move while the user finishes
        try:
            windows = {w.id: w for w in self.backend.list_windows()}
        except CcdeskError:
            windows = {}
        window = self._receiver(windows, point) if windows else None
        target = self._target
        if target and not force and windows and target.id not in windows:
            # The window we meant is gone, whoever would receive the input now (or nobody known):
            # with an own pointer the keys would go to whatever the agent's focus fell back to.
            who = (f"{'the pointer at %d,%d is over' % point if point else 'keyboard focus is on'} "
                   f"{window.title!r} ({window.id}), and " if window is not None else "")
            raise PolicyError(f"{who}the window from activate_window() ({target.title!r}, {target.id}) is "
                              f"gone; call list_windows() and activate_window() again, or pass force=true")
        if (self.agent and target and not force and window is not None and target.id in windows
                and not self._belongs_to_target(window)):
            # Another agent on this screen moved focus or raised its window over ours. We hold the
            # seat lock, so take our window back and check again.
            self.backend.activate_window(target)
            window = self._receiver(windows, point)
        if target and not force and window is not None and not self._belongs_to_target(window):
            what = f"the pointer at {point[0]},{point[1]} is over" if point else "keyboard focus is on"
            if target.id not in windows:
                raise PolicyError(f"{what} {window.title!r} ({window.id}), and the window from "
                                  f"activate_window() ({target.title!r}, {target.id}) is gone; call "
                                  f"list_windows() and activate_window() again, or pass force=true")
            raise PolicyError(f"{what} {window.title!r} ({window.id}), not {target.title!r} from "
                              f"activate_window(): the user may have switched windows. Call "
                              f"activate_window({target.id!r}) again, or pass force=true to send it anyway")
        if expect_title is not None:
            # Not skipped by force: the caller named the title it wants on purpose.
            if window is None:
                raise PolicyError(f"cannot tell which window would get this input, so expect_title="
                                  f"{expect_title!r} cannot be checked; leave expect_title out to send it anyway")
            if expect_title.strip().lower() not in window.title.lower():
                raise PolicyError(f"{window.title!r} ({window.id}) does not contain {expect_title.strip()!r}: "
                                  f"the user may have switched tabs. Take a screenshot, switch back, then retry")
        self._after = None  # wait_for_change() compares with the screen after this action, not before
        if self._batching and self._pending is not None:
            return window  # a batch is compared with the screen before its first step
        self._pending = None
        if self.a11y._last is None:
            region = self._window_region(window)
            grid = self._snapshot(region)
            self._pending = (region, grid) if grid is not None else None
        return window

    def _with_window(self, result: dict, window: Window | None) -> dict:
        if window:
            result["window"] = {"id": window.id, "title": window.title}
        if self._yielded:
            result["yielded_ms"] = self._yielded
        return result

    def _window_region(self, window: Window | None) -> Region | None:
        """The window's box clipped to the screen; None (the whole screen) if unknown or tiny."""
        if window is None:
            return None
        x0, y0 = max(0, window.x), max(0, window.y)
        x1, y1 = window.x + window.width, window.y + window.height
        size = self.screen_size()
        if size:
            x1, y1 = min(x1, size[0]), min(y1, size[1])
        if x1 - x0 < 2 * DIFF_CELL or y1 - y0 < 2 * DIFF_CELL:
            return None
        return Region(x0, y0, x1 - x0, y1 - y0)

    def _snapshot(self, region: Region | None) -> Grid | None:
        """A grayscale copy of the screen at 1/DIFF_CELL size, or None (no ImageMagick, or it failed)."""
        if not self.screen_diff:
            return None
        fd, tmp = tempfile.mkstemp(prefix="ccdesk-diff-", suffix=".png")
        os.close(fd)
        try:
            self.backend.screenshot(tmp, region)
            width, height = png_size(tmp)
            cols, rows = max(1, width // DIFF_CELL), max(1, height // DIFF_CELL)
            self.backend.scale_png(tmp, cols, rows)
            origin = (region.x, region.y) if region else (0, 0)
            return Grid.from_png(tmp, origin, scale=(width / cols, height / rows))
        except (CcdeskError, OSError, ValueError):
            return None
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # -- tools ------------------------------------------------------------------

    def _shown_window_region(self, title_or_id) -> tuple[Window, Region, bool]:
        """A window to capture and its box on screen. A capture is a crop of the screen, so a
        window that is not on show would come out as whatever is there instead."""
        window = self.find_window(title_or_id)
        shown = self.backend.window_on_show(window.id)
        if shown is False:
            raise CcdeskError(f"{window.title!r} ({window.id}) is minimized or on another desktop; "
                              f"call activate_window() first")
        reg = self._window_region(window)
        if reg is None:
            raise CcdeskError(f"{window.title!r} ({window.id}) has no visible area on this screen")
        return window, reg, shown is None

    def screenshot(self, region=None, path: str | None = None, scale: float = 1.0, window: str | None = None) -> dict:
        def do():
            self._ready()
            if region and window:
                raise ValidationError("pass region or window, not both")
            shot_of, reg, unchecked = self._shown_window_region(window) if window else (None, None, False)
            reg = reg or parse_region(region, self.screen_size())
            factor = check_scale(scale)
            out = self._shot_path(path)
            self.backend.screenshot(str(out), reg)
            if not out.exists():
                raise CcdeskError(f"screenshot tool reported success but {out} was not written")
            width, height = png_size(out)
            if factor < 1:
                self.backend.scale_png(str(out), max(1, round(width * factor)), max(1, round(height * factor)))
                width, height = png_size(out)
            # Image input costs about width*height/750 tokens once the model reads it.
            result = {"path": str(out), "width": width, "height": height, "approx_tokens": round(width * height / 750)}
            if factor < 1:
                result["scale"] = factor
            if shot_of:
                result["window"] = {"id": shot_of.id, "title": shot_of.title}
            if reg:
                result["origin"] = {"x": reg.x, "y": reg.y}
            if reg and factor < 1:
                result["note"] = "pixel (px,py) in this image is screen (origin.x+px/scale, origin.y+py/scale)"
            elif reg:
                result["note"] = "pixel (px,py) in this image is screen (origin.x+px, origin.y+py)"
            elif factor < 1:
                result["note"] = "pixel (px,py) in this image is screen (px/scale, py/scale)"
            if unchecked:
                result["warning"] = ("could not check that this window is on show (on X11, install xwininfo); "
                                     "if the image shows something else, activate_window() it first")
            hint = self._crop_hint(reg, result["approx_tokens"])
            if hint:
                result["hint"] = hint
            tree_hint = self._tree_hint(reg, factor, result["approx_tokens"])
            if tree_hint:
                result["hint"] = tree_hint if not hint else hint + "; " + tree_hint
            return result

        return self._act("screenshot", {"region": region, "path": path, "scale": scale, "window": window}, do)

    def _tree_hint(self, reg: Region | None, factor: float, tokens: int) -> str | None:
        """A whole screen at full scale before any tree was read: the first shot is where the
        cheap path should have started, so say what it costs and what the alternative costs.
        Once per session: an agent that needs pixels after reading it has already chosen."""
        if reg is not None or factor < 1 or self.a11y._last is not None or self._tree_hinted:
            return None
        self._tree_hinted = True
        return (f"~{tokens} tokens for the whole screen, and ui_tree() has not been read yet: it returns "
                "a window that publishes an accessibility tree as lines of text for a few hundred tokens, "
                "and every action after it reports only the lines that changed. Pass region= or scale= "
                "when a picture is what you need")

    def _crop_hint(self, reg: Region | None, tokens: int) -> str | None:
        """Taken right after an action that changed a small area, a wide screenshot mostly pays
        for pixels that did not change: say what the crop would cost. Once per action."""
        area, self._last_area = self._last_area, None
        if area is None or tokens < HINT_MIN_TOKENS:
            return None
        crop = self._padded(area)
        cost = round(crop.width * crop.height / 750)
        if cost * 2 > tokens:  # a saving worth a sentence, at the scale actually paid
            return None
        return (f"the last action changed only {area.x},{area.y},{area.width},{area.height}: "
                f'region="{crop.x},{crop.y},{crop.width},{crop.height}" shows it for ~{cost} tokens '
                f"(this one: ~{tokens}), and show_change=true on the action shows it with no screenshot at all")

    def _shot_path(self, path: str | None) -> Path:
        if path:
            out = Path(path).expanduser()
            if out.suffix.lower() != ".png":
                raise ValidationError("path must end in .png")
            out.parent.mkdir(parents=True, exist_ok=True)
            return out.resolve()
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self._prune_shots()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        return self.shots_dir / f"screen-{stamp}.png"

    def _prune_shots(self) -> None:
        shots = sorted(self.shots_dir.glob("screen-*.png"))
        for old in shots[: max(0, len(shots) - KEEP_SCREENSHOTS + 1)]:
            try:
                old.unlink()
            except OSError:
                pass

    def click(self, x: int, y: int, button: str = "left", count: int = 1, force: bool = False,
              expect_title: str | None = None, show_change: bool = False) -> dict:
        def do():
            self._ready()
            px, py = self._point(x, y)
            btn = parse_button(button)
            n = check_int("count", count, 1, 10)
            window = self._guard(force, (px, py), expect_title)
            self.backend.click(px, py, btn, n)
            self._show_cursor(px, py)
            return self._changes(self._with_window({"clicked": {"x": px, "y": py}, "button": button, "count": n},
                                                   window), show_change)

        return self._act("click", {"x": x, "y": y, "button": button, "count": count, "force": force,
                                     "expect_title": expect_title, "show_change": show_change}, do)

    def move(self, x: int, y: int) -> dict:
        def do():
            self._ready()
            px, py = self._point(x, y)
            self._yielded = self._yield_to_user()
            self.backend.move(px, py)
            self._show_cursor(px, py)
            return self._with_window({"moved": {"x": px, "y": py}}, None)

        return self._act("move", {"x": x, "y": y}, do)

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int, duration_ms: int = 400,
             force: bool = False, show_change: bool = False) -> dict:
        def do():
            self._ready()
            x0, y0 = self._point(from_x, from_y)
            x1, y1 = self._point(to_x, to_y)
            ms = check_int("duration_ms", duration_ms, 0, 10_000)
            window = self._guard(force, (x0, y0))
            self.backend.drag(x0, y0, x1, y1, ms)
            self._show_cursor(x1, y1)
            return self._changes(self._with_window({"from": {"x": x0, "y": y0}, "to": {"x": x1, "y": y1},
                                                    "duration_ms": ms}, window), show_change)

        args = {"from_x": from_x, "from_y": from_y, "to_x": to_x, "to_y": to_y, "duration_ms": duration_ms,
                "force": force, "show_change": show_change}
        return self._act("drag", args, do)

    def scroll(self, x: int, y: int, dx: int = 0, dy: int = 0, force: bool = False, show_change: bool = False) -> dict:
        def do():
            self._ready()
            px, py = self._point(x, y)
            sx = check_int("dx", dx, -50, 50)
            sy = check_int("dy", dy, -50, 50)
            if not sx and not sy:
                raise ValidationError("dx and dy are both 0; nothing to scroll. Pass dy=3 to scroll down, dy=-3 up, "
                                      "dx=3 right (there is no direction or amount argument)")
            window = self._guard(force, (px, py))
            self.backend.scroll(px, py, sx, sy)
            self._show_cursor(px, py)
            return self._changes(self._with_window({"at": {"x": px, "y": py}, "dx": sx, "dy": sy}, window), show_change)

        return self._act("scroll", {"x": x, "y": y, "dx": dx, "dy": dy, "force": force, "show_change": show_change}, do)

    def type_text(self, text: str, submit: bool = False, replace: bool = False, force: bool = False,
                  expect_title: str | None = None, show_change: bool = False) -> dict:
        def do():
            self._ready()
            if not isinstance(text, str) or not text:
                raise ValidationError("text must be a non-empty string")
            if len(text) > MAX_TEXT:
                raise ValidationError(f"text is longer than {MAX_TEXT} characters; paste via clipboard instead")
            # A typed trailing newline did not submit Firefox's address bar: send a real Return instead.
            body = text[:-1] if text.endswith("\n") else text
            enter = bool(submit) or body != text
            if self._batching:
                self.a11y.refresh_secret()  # an earlier step may have clicked into a password field
            if self.a11y.on_secret_field():
                raise PolicyError("the focused field is a password field: ccdesk does not type into those. "
                                  "Ask the user to type it themselves")
            window = self._guard(force, expect_title=expect_title)
            if replace:
                self.backend.press_key(SELECT_ALL)
                self.backend.sleep(KEY_SETTLE)
            if body:
                self.backend.type_text(body)
            if enter:
                self.backend.sleep(KEY_SETTLE)  # autocomplete and validation catch up first
                self.backend.press_key(RETURN)
            result = {"typed_chars": len(body)}
            warning = self.backend.typing_warning(body)
            if warning:
                result["warning"] = warning
            if replace:
                result["replaced"] = True
            if enter:
                result["submitted"] = True
            return self._changes(self._with_window(result, window), show_change)

        return self._act("type_text", {"text": text, "submit": submit, "replace": replace, "force": force,
                                         "expect_title": expect_title, "show_change": show_change}, do)

    def press_key(self, key: str, modifiers: list[str] | None = None, confirm: bool = False,
                  force: bool = False, expect_title: str | None = None, show_change: bool = False) -> dict:
        def do():
            combo = parse_key_combo(key, modifiers or [])
            self.policy.check_key(combo, bool(confirm))
            self._ready()
            window = self._guard(force, expect_title=expect_title)
            self.backend.press_key(combo)
            if moves_focus(combo):
                self.backend.sleep(FOCUS_SETTLE)  # so a type_text right after lands in the new place
            return self._changes(self._with_window({"pressed": str(combo)}, window), show_change)

        args = {"key": key, "modifiers": modifiers or [], "confirm": confirm, "force": force,
                "expect_title": expect_title, "show_change": show_change}
        return self._act("press_key", args, do)

    def list_windows(self) -> dict:
        def do():
            self._ready()
            active = self.backend.active_window()
            # Same geometry on two desktops looks the same in the list: say which one is in front.
            windows = [{**w.to_dict(), "active": w.id == active} for w in self.backend.list_windows()]
            return {"windows": windows, "current_desktop": self.backend.current_desktop()}

        return self._act("list_windows", {}, do)

    def find_window(self, title_or_id) -> Window:
        kind, ref = parse_window_ref(title_or_id)
        windows = self.backend.list_windows()
        if kind == "id":
            matches = [w for w in windows if w.id.lower() == ref.lower() or w.id == str(title_or_id)]
            if not matches:
                raise ValidationError(f"no window with id {ref}; call list_windows() for current ids")
            return matches[0]
        exact = [w for w in windows if w.title == ref]
        if exact:
            return exact[0]
        partial = [w for w in windows if ref.lower() in w.title.lower()]
        if not partial:
            titles = ", ".join(repr(w.title) for w in windows[:15]) or "none"
            raise ValidationError(f"no window title contains {ref!r}; open windows: {titles}")
        return partial[0]

    def activate_window(self, title_or_id: str) -> dict:
        def do():
            self._ready()
            window = self.find_window(title_or_id)
            self._yielded = self._yield_to_user()
            self.backend.activate_window(window)
            self._target = window  # input tools now refuse to go anywhere else (see _guard)
            result = self._with_window({"activated": window.to_dict()}, None)
            note = self.backend.take_note()
            if note:
                result["note"] = note
            return result

        return self._act("activate_window", {"title_or_id": title_or_id}, do)

    def _window_state(self, window_id: str) -> tuple[Window | None, bool | None]:
        window = next((w for w in self.backend.list_windows() if w.id == window_id), None)
        return window, self.backend.window_on_show(window_id)

    @staticmethod
    def _placement(state: tuple[Window | None, bool | None]) -> tuple:
        """What manage_window can change. Not the title: apps change that on their own."""
        w, shown = state
        return (w.x, w.y, w.width, w.height, w.desktop, shown) if w else (None, shown)

    def manage_window(self, title_or_id: str, action: str, x: int | None = None, y: int | None = None,
                      width: int | None = None, height: int | None = None) -> dict:
        def do():
            self._ready()
            if action not in WINDOW_ACTIONS:
                raise ValidationError(f"action must be one of: {', '.join(WINDOW_ACTIONS)}")
            given = {"x": x, "y": y, "width": width, "height": height}
            wanted = {"move": ("x", "y"), "resize": ("width", "height")}.get(action, ())
            for name, value in given.items():
                if name in wanted and value is None:
                    raise ValidationError(f"{action} needs {' and '.join(wanted)}")
                if name not in wanted and value is not None:
                    raise ValidationError(f"{action} does not take {name}")
            if action == "move":
                args = (check_int("x", x, -MAX_WINDOW_COORD, MAX_WINDOW_COORD),
                        check_int("y", y, -MAX_WINDOW_COORD, MAX_WINDOW_COORD))
            elif action == "resize":
                args = (check_int("width", width, 1, MAX_WINDOW_COORD),
                        check_int("height", height, 1, MAX_WINDOW_COORD))
            else:
                args = ()
            window = self.find_window(title_or_id)
            self._yielded = self._yield_to_user()
            first = self._window_state(window.id)
            before = first[0]
            getattr(self.backend, f"{action}_window")(window, *args)
            state = first
            for _ in range(WINDOW_POLLS):  # until it has changed and stopped changing (animations)
                self.backend.sleep(POLL)
                previous, state = state, self._window_state(window.id)
                now = self._placement(state)
                if now != self._placement(first) and now == self._placement(previous):
                    break
            after, shown_after = state
            self._after = None  # the screen moved: wait_for_change() starts over
            changed = self._placement(state) != self._placement(first)
            result = {"action": action, "changed": changed, "before": before.to_dict() if before else None,
                      "after": after.to_dict() if after else None}
            if shown_after is not None:
                result["on_show"] = shown_after
            if after is None:
                result["note"] = "the window is gone: it closed during this action; call list_windows()"
            elif not changed:
                result["note"] = ("its box and visibility did not change: it was already like that, or the "
                                  "window manager or app ignored the request; take a screenshot to check")
            return self._with_window(result, window)

        args = {"title_or_id": title_or_id, "action": action, "x": x, "y": y, "width": width, "height": height}
        return self._act("manage_window", args, do)

    def list_monitors(self) -> dict:
        def do():
            self._ready()
            size = self.screen_size()
            result = {"monitors": self.backend.monitors(),
                      "screen": {"width": size[0], "height": size[1]} if size else None}
            if not result["monitors"]:
                result["note"] = ("monitors unknown: X11 needs xrandr; on Wayland only sway, Hyprland and "
                                  "Plasma with kdotool report them. The screen size is still usable")
            return result

        return self._act("list_monitors", {}, do)

    def ui_tree(self, window: str | None = None, query: str | None = None, page: bool = False,
                max_nodes: int = 300, shot_fallback: bool = True) -> dict:
        def do():
            limit = check_int("max_nodes", max_nodes, 1, 2000)
            q = (query or "").strip() or None
            title = window or self._own_window_title()
            try:
                result = self.a11y.tree(title or None, q, bool(page), limit)
            except WindowNotFound as exc:
                why = self._a11y_diagnosis(window or None)
                message = f"{exc}. {why}" if why else str(exc)
                if shot_fallback and not q and why:
                    # On screen but not on the bus: the window exists, so show it.
                    try:
                        shot = self._tree_fallback_shot(window or None)
                    except (CcdeskError, OSError, ValueError):
                        shot = None
                    if shot:
                        self.a11y.forget()  # a tree of another window: actions compare pixels now
                        return {"fallback_shot": shot, "note": f"{message}. {TREE_SHOT_NOTE}"}
                raise CcdeskError(message) from None
            text = result["tree"]
            result["approx_tokens"] = len(text) // 4
            if result["truncated"]:
                result["note"] = "truncated: narrow it with query=..., page=true or window=..."
            if self.backend.name in ("wayland", "kde") and "origin" not in result:
                result["note"] = ("on Wayland, @x,y may be relative to the window (this compositor does "
                                  "not say where it is); verify with a screenshot")
            if shot_fallback and not q and tree_is_empty(text):
                try:
                    result["fallback_shot"] = self._tree_fallback_shot(result.get("window"))
                except (CcdeskError, OSError, ValueError):
                    pass  # keep the (thin) tree; the model can still call screenshot()
                else:
                    self.a11y.forget()  # nothing to compare: actions report pixel changes instead
                    result["note"] = " ".join(filter(None, [result.get("note"),
                                                            f"the tree is nearly empty. {TREE_SHOT_NOTE}"]))
            return result

        args = {"window": window, "query": query, "page": page, "max_nodes": max_nodes,
                "shot_fallback": shot_fallback}
        return self._act("ui_tree", args, do,
                         loggable=lambda r: {k: v for k, v in r.items() if k != "tree"})

    def _own_window_title(self) -> str | None:
        """With an own pointer, the title of the window the agent's keyboard is in. The helper's
        default is the frame AT-SPI marks active, which follows the user's focus, not the agent's."""
        if not self.backend.own_pointer:
            return None
        try:
            wid = self.backend.active_window()
            if not wid:
                return None
            return next((w.title for w in self.backend.list_windows() if w.id == wid and w.title), None)
        except CcdeskError:
            return None

    def _a11y_diagnosis(self, title: str | None) -> str | None:
        """Why ui_tree() found no window: which window is missing from the bus, and how to fix it."""
        try:
            windows = self.backend.list_windows()
            if title:
                win = next((w for w in windows if title.lower() in w.title.lower()), None)
            else:
                wid = self.backend.active_window()
                win = next((w for w in windows if w.id == wid), None)
            apps = self.a11y.apps() if win else []
        except CcdeskError:
            return None
        if win is None or any(win.title in (a.get("windows") or []) for a in apps):
            return None
        on_bus = ", ".join(a["name"] for a in apps if a.get("windows")) or "nothing"
        # A headless screen has its own bus, and the apps on it are started with accessibility on.
        enabled = bus_enabled(self.backend.try_run) if self.headless is None else None
        return (f"{win.title!r} ({win.wm_class}) is not on the accessibility bus (on the bus: {on_bus}): "
                f"{missing_hint(win.wm_class, enabled, self.backend.window_role(win.id))}. Until then, work from screenshots")

    def click_element(self, ref: str, button: str = "left", count: int = 1, force: bool = False,
                      expect_title: str | None = None) -> dict:
        def do():
            self._ready()
            btn = parse_button(button)
            n = check_int("count", count, 1, 10)
            node = self.a11y.resolve(ref)
            label = f'{node["role"]} {node["name"]!r}'
            if not node.get("showing") or not node.get("box"):
                raise CcdeskError(f"{ref} ({label}) is not on screen; scroll it into view, then call ui_tree() again")
            # expect_title takes the pointer path: only _guard checks the window that receives the click.
            if (btn == 1 and n == 1 and expect_title is None and node["role"] in NATIVE_ROLES
                    and self._may_skip_pointer(force)):
                self._before_native()
                action = self.a11y.activate(str(ref).strip())
                if action:
                    self._cursor_to_node(node)
                    return self._changes(self._in_tree_window({"activated": action, "element": label}))
            x, y, w, h = node["box"]
            px, py = self._point(x + w // 2, y + h // 2)
            window = self._guard(force, (px, py), expect_title)
            self.backend.click(px, py, btn, n)
            self._show_cursor(px, py)
            return self._changes(self._with_window({"clicked": {"x": px, "y": py}, "element": label,
                                                    "button": button, "count": n}, window))

        return self._act("click_element", {"ref": ref, "button": button, "count": count, "force": force,
                                             "expect_title": expect_title}, do)

    def _may_skip_pointer(self, force: bool) -> bool:
        """A native action reaches its element wherever the pointer and focus are, so _guard has
        nothing to check, unless the tree is of another window than activate_window() chose."""
        return force or self._target is None or self.a11y.tree_window() == self._target.title

    def _before_native(self) -> None:
        self._yielded = self._yield_to_user()
        self._after = None
        if not self._batching:
            self._pending = None

    def _in_tree_window(self, result: dict) -> dict:
        if self._yielded:
            result["yielded_ms"] = self._yielded
        result["window"] = {"title": self.a11y.tree_window()}
        return result

    def set_value(self, ref: str, text: str, force: bool = False) -> dict:
        def do():
            self._ready()
            if not isinstance(text, str):
                raise ValidationError("text must be a string")
            if len(text) > MAX_TEXT:
                raise ValidationError(f"text is longer than {MAX_TEXT} characters")
            key = str(ref).strip()
            node = self.a11y.resolve(key)
            label = f'{node["role"]} {node["name"]!r}'
            if not node.get("showing"):
                raise CcdeskError(f"{key} ({label}) is not on screen; scroll it into view, then call ui_tree() again")
            if not self._may_skip_pointer(force):
                raise PolicyError(f"{key} is in {self.a11y.tree_window()!r}, not {self._target.title!r} from "
                                  f"activate_window(); call ui_tree() on that window, or pass force=true")
            self._before_native()
            answer = self.a11y.set_value(key, text)
            self._cursor_to_node(node)
            if not answer["verified"] and shown(answer.get("value")) == shown(node.get("value")) and node.get("box"):
                # The write was ignored, not changed: web pages (Firefox, Chromium) take the call
                # and keep the old value. Type it the way a person would instead.
                return self._type_into(key, node, label, text, force)
            if not answer["verified"]:
                raise CcdeskError(f"{key} ({label}) holds {answer.get('value')!r} after writing: the app changed or "
                                  f"rejected it. Check the field; don't repeat this blindly")
            return self._changes(self._in_tree_window({"set": label, "verified": True, "chars": len(text)}))

        return self._act("set_value", {"ref": ref, "text": text, "force": force}, do)

    def _type_into(self, key: str, node: dict, label: str, text: str, force: bool) -> dict:
        """set_value's fallback: click the field, select all, type, and read the value back."""
        x, y, w, h = node["box"]
        px, py = self._point(x + w // 2, y + h // 2)
        window = self._guard(force, (px, py))
        self.backend.click(px, py, 1, 1)
        self._show_cursor(px, py)
        self.backend.sleep(KEY_SETTLE)
        self.backend.press_key(SELECT_ALL)
        self.backend.sleep(KEY_SETTLE)
        if text:
            self.backend.type_text(text)
        else:
            self.backend.press_key(DELETE)
        got = None
        for _ in range(TYPED_POLLS):
            self.backend.sleep(POLL)
            got = self.a11y.resolve(key).get("value")
            if shown(got) == shown(text) or (not got and shown(text) == shown(node["name"])):
                result = {"set": label, "verified": True, "chars": len(text), "typed": True,
                          "note": "the app ignored the direct write, so ccdesk clicked the field and typed it"}
                return self._changes(self._with_window(result, window))
        raise CcdeskError(f"{key} ({label}) ignored the direct write, and after clicking it and typing it holds "
                          f"{got!r}. Check the field with a screenshot; don't repeat this blindly")

    def clipboard(self, action: str = "get", text: str | None = None) -> dict:
        action = str(action or "").strip().lower()

        def do():
            if action not in ("get", "set"):
                raise ValidationError('action must be "get" or "set"')
            self._ready()
            if action == "get":
                return {"text": self.backend.clipboard_get()}
            if text is None:
                raise ValidationError('clipboard(action="set") needs text')
            self.backend.clipboard_set(text)
            return {"set_chars": len(text)}

        # Clipboard contents often hold secrets: the audit log only records their length.
        args = {"action": action} if text is None else {"action": action, "text_len": len(text)}
        return self._act("clipboard", args, do, policy_tool="clipboard_set" if action == "set" else "clipboard",
                         loggable=lambda r: {k: (len(v) if k == "text" else v) for k, v in r.items()})

    def wait(self, seconds: float) -> dict:
        def do():
            try:
                s = float(seconds)
            except (TypeError, ValueError):
                raise ValidationError("seconds must be a number") from None
            if not 0 <= s <= MAX_WAIT:
                raise ValidationError(f"seconds must be between 0 and {MAX_WAIT:g}")
            prev = self._prev_tool
            time.sleep(s)
            result = {"waited": s}
            if s >= 1 and prev in DIFF_TOOLS and self.screen_diff and self.a11y._last is None:
                result["hint"] = ("after an action, wait_for_change() returns as soon as the screen has "
                                  "settled, at once if it already has, instead of guessing a delay")
            return result

        return self._act("wait", {"seconds": seconds}, do)

    def _batch_steps(self, steps) -> list[tuple[str, dict]]:
        """Check every step before any input is sent: a mistake in step 7 must not leave 1-6 done."""
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_BATCH_STEPS:
            raise ValidationError(f"steps must be a list of 1 to {MAX_BATCH_STEPS} steps")
        checked = []
        for i, step in enumerate(steps):
            if not isinstance(step, dict) or not isinstance(step.get("tool"), str):
                raise ValidationError(f"step {i}: expected an object with a \"tool\" name")
            name = step["tool"]
            args = {k: v for k, v in step.items() if k != "tool"}
            if name not in BATCH_TOOLS:
                raise ValidationError(f"step {i}: {name!r} cannot run in a batch (use one of {', '.join(BATCH_TOOLS)})")
            if "show_change" in args:
                raise ValidationError(f"step {i}: show_change belongs to the batch call, not to a step")
            try:
                inspect.signature(getattr(self, name)).bind(**args)
            except TypeError as exc:
                raise ValidationError(f"step {i} ({name}): {exc}") from None
            if name == "wait":
                try:
                    ok = 0 <= float(args["seconds"]) <= MAX_BATCH_WAIT
                except (TypeError, ValueError):
                    ok = False
                if not ok:
                    raise ValidationError(f"step {i}: wait in a batch takes 0 to {MAX_BATCH_WAIT:g} seconds")
            checked.append((name, args))
        typed = sum(len(a["text"]) for _, a in checked if isinstance(a.get("text"), str))
        if typed > MAX_TEXT:
            # Every other agent on the screen waits while a batch holds the seat.
            raise ValidationError(f"a batch types at most {MAX_TEXT} characters in all, got {typed}")
        return checked

    def batch(self, steps: list[dict], show_change: bool = False) -> dict:
        """Run several input steps in one call, holding the seat for all of them, and observe only
        the end state. Stops at the first step that fails."""
        checked = self._batch_steps(steps)
        logged = [{"tool": n, **_redact(a)} for n, a in checked]

        def do():
            done: list[dict] = []
            result: dict = {"done": 0, "steps": done}
            self._batching, self._pending = True, None
            try:
                self._run_steps(checked, result, done)
            finally:
                self._batching = False
            if self._deferred is not None:  # one observation for the whole batch, into this result
                self._deferred = (result, show_change)
            return result

        return self._act("batch", {"steps": logged}, do,
                         loggable=lambda r: {k: r[k] for k in ("done", "failed") if k in r})

    def _run_steps(self, checked, result: dict, done: list) -> None:
        for i, (name, args) in enumerate(checked):
            try:
                out = getattr(self, name)(**args)
            except Exception as exc:  # the steps before it were sent: report, then observe them
                msg = str(exc) if isinstance(exc, CcdeskError) else f"{type(exc).__name__}: {exc}"
                result["failed"] = {"index": i, "tool": name, "error": msg}
                break
            done.append({"tool": name, **{k: v for k, v in out.items() if k not in ("window", "hint")}})
            result["done"] = i + 1

    def wait_for_change(self, region=None, timeout: float = 10, settle_ms: int = 500) -> dict:
        """Wait until the screen differs from how it looked right after the latest action (or, with
        a region or no action yet, from now), then until it has been still for settle_ms."""
        def do():
            self._ready()
            reg = parse_region(region, self.screen_size())
            try:
                limit = float(timeout)
            except (TypeError, ValueError):
                raise ValidationError("timeout must be a number") from None
            if not 0 < limit <= MAX_WAIT_FOR_CHANGE:
                raise ValidationError(f"timeout must be between 0 and {MAX_WAIT_FOR_CHANGE:g} seconds")
            settle = check_int("settle_ms", settle_ms, 0, 60_000) / 1000
            start = self.clock()
            if reg is None and self._after is not None:
                reg, base = self._after  # a page that loaded while the model was thinking counts
            else:
                base = self._snapshot(reg)
            if base is None:
                raise UnsupportedError("wait_for_change needs ImageMagick (magick or convert) to compare "
                                       "screenshots; use wait(seconds) instead")
            prev, area, last_motion = base, None, None
            while (elapsed := self.clock() - start) < limit:
                self.backend.sleep(min(POLL, limit - elapsed))
                current = self._snapshot(reg)
                if current is None:
                    continue
                if changed_box(prev, current):
                    last_motion = self.clock()
                prev = current
                area = changed_box(base, current) or area
                if area and last_motion is not None and self.clock() - last_motion >= settle:
                    break
            self._after = (reg, prev)  # the next wait_for_change waits for the next change
            waited = round(min(self.clock() - start, limit) * 1000)
            if area is None:
                return {"changed": False, "waited_ms": waited,
                        "note": f"nothing changed in {limit:g}s; take a screenshot to see why"}
            settled = self.clock() - last_motion >= settle
            self._last_area = Region(*area)
            return {"changed": True, "settled": settled, "waited_ms": waited,
                    "changed_area": dict(zip(("x", "y", "width", "height"), area))}

        args = {"region": region, "timeout": timeout, "settle_ms": settle_ms}
        return self._act("wait_for_change", args, do)

    def launch(self, command: str) -> dict:
        def do():
            if not isinstance(command, str) or not command.strip():
                raise ValidationError("command must be a non-empty string")
            try:
                argv = shlex.split(command)
            except ValueError as exc:
                raise ValidationError(f"cannot parse command: {exc}") from None
            # No shell, so expand ~ here: `--profile ~/x` and `--user-data-dir=~/x` both work.
            argv = [re.sub(r"(^|=)~(?=/|$)", lambda m: m.group(1) + os.path.expanduser("~"), a) for a in argv]
            if not shutil.which(argv[0]):
                raise ValidationError(f"{argv[0]!r} not found on PATH")
            # No shell: the command is split into argv, so no pipes, redirects or `;` chaining.
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True,
                                    env=self.backend.env)
            return {"pid": proc.pid, "argv": argv}

        return self._act("launch", {"command": command}, do)

    def doctor(self) -> dict:
        from ccdesk.doctor import build_report

        def report():
            r = build_report(self.backend, self.policy, self.audit.path, self.a11y, self.headless, self.cursor)
            if self.seat.unavailable:
                r["warnings"].append(self.seat.unavailable)
            return r

        return self._act("doctor", {}, report,
                         loggable=lambda r: {"backend": r["backend"], "ready": r["ready"], "missing": r["missing"]})
