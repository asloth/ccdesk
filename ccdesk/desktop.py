"""The action layer: validation, safety policy and audit around a backend.

Everything the MCP server exposes is a method here, so it can be tested without MCP.
"""

from __future__ import annotations

import inspect
import math
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from ccdesk.a11y import NATIVE_ROLES, A11y, WindowNotFound, bus_enabled, missing_hint
from ccdesk.backends import Backend, get_backend
from ccdesk.errors import (
    CcdeskError,
    PolicyError,
    StaleFrameError,
    TargetCoveredError,
    TargetHiddenError,
    TargetRequiredError,
    UnsupportedError,
    ValidationError,
)
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
# What an agent (--agent) may only do once it has chosen its window with activate_window().
NEEDS_TARGET = frozenset({"click", "drag", "scroll", "type_text", "press_key", "click_element", "set_value"})
MAX_BATCH_STEPS = 20
MAX_BATCH_WAIT = 5.0  # seconds per wait step: the seat is held meanwhile
DIFF_CELL = 8  # screen pixels per cell when comparing before/after captures
POLL = 0.2  # seconds between captures in wait_for_change
MAX_YIELD = 15.0  # seconds input waits for the user to pause (--yield-ms) before it is refused
OWN_INPUT_SLACK_MS = 150  # xdotool's events can land this long after it returns
KEEP_SCREENSHOTS = 200
SETTLE = 0.3  # seconds the helper watches for a change before an action reports "no change"
SETTLE_POLL = 0.03  # pause between direct captures while an action's pixel change settles
MAX_SETTLE = 1.5  # seconds an action that changed the screen waits for it to stop moving
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
MAX_SHOTS = 32  # pictures whose origin and scale are remembered, for shot=
DIFF_TOOLS = frozenset({"click", "drag", "scroll", "type_text", "press_key"})  # report screen_changed


def shown(value) -> str:
    """A field's text as a tree read shows it: whitespace collapsed, cut at 200 characters."""
    text = " ".join(str(value or "").split())
    return text[:200].removesuffix("…")


TREE_SHOT_NOTE = ("A picture of the window is attached: work from it. Screen pixel = "
                  "origin + pixel/scale, or pass its `shot` id as shot= with the picture's own pixels")
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


def describe_layout_change(old: tuple, new: tuple) -> str:
    """What differs between two _layout() fingerprints, in words a model can act on."""
    before = {r[0]: r for r in old[1]}
    after = {r[0]: r for r in new[1]}
    parts = []
    if old[0] != new[0]:
        parts.append(f"screen {old[0]} -> {new[0]}")
    for label, wids in (("opened", [w for w in after if w not in before]),
                        ("closed", [w for w in before if w not in after]),
                        ("moved or restacked", [w for w in after if w in before and before[w][2:] != after[w][2:]]),
                        ("retitled", [w for w in after if w in before and before[w][1] != after[w][1]
                                      and before[w][2:] == after[w][2:]])):
        if wids:
            rows = [(after.get(w) or before[w]) for w in wids]
            parts.append(f"{label}: " + ", ".join(f"{r[1]!r} ({r[0]})" for r in rows[:3])
                         + (f" and {len(rows) - 3} more" if len(rows) > 3 else ""))
    if old[2] != new[2]:
        parts.append(f"focus moved to {new[2]}")
    if old[3] != new[3]:
        parts.append(f"desktop {old[3]} -> {new[3]}")
    return "; ".join(parts) or "the layout changed"


def empty_page(text: str) -> bool:
    """A browser window whose web page has no elements: a `document` line with nothing under it.
    Firefox started before the accessibility bus was on publishes its toolbars and tabs but not
    the page, so the tree looks full (tens of lines) while what the agent needs is missing."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        parts = ln.split(None, 2)
        if len(parts) < 2 or parts[1] != "document":
            continue
        depth = len(ln) - len(ln.lstrip())
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if not nxt or len(nxt) - len(nxt.lstrip()) <= depth:
            return True
    return False


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
        # (region, grid, window id when the grid is that window's own pixels: see _own_pixels)
        self._pending: tuple[Region | None, Grid, str | None] | None = None
        self._grid_fast: bool | None = None  # the latest _snapshot() read the display directly (None: no snapshot yet)
        # Whether the current action reports pixels (it landed outside the tree's window) rather than
        # the tree's lines, and whether the latest tree re-read failed (until ui_tree() works again).
        self._pixel_report = False
        self._tree_unreadable = False
        self._moving = False  # the latest _settled_snapshot() ended with the screen still changing
        self._after: tuple[Region | None, Grid, str | None] | None = None
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
        self._deferred: tuple[dict, bool | None] | None = None  # what _act observes once the seat is free
        # The window layout when the latest screenshot was delivered (see _layout): coordinates
        # taken from that screenshot only hold while it is unchanged. None: nothing to compare with.
        self._layout_seen: tuple | None = None
        # The latest MAX_SHOTS pictures handed out: id -> (origin x, y, scale x, y, width, height), so
        # click(shot=...) can turn a picture's own pixels into screen pixels.
        self._shots: OrderedDict[str, tuple] = OrderedDict()
        # The layout fingerprint (see _layout) each of those pictures was taken in, by id. A picture
        # whose layout is unknown (None, or absent) is not checked.
        self._shot_layouts: dict[str, tuple | None] = {}
        self._note: str | None = None  # what the backend said while _guard took the agent's window back
        self._shot_count = 0
        self._shots_lock = threading.Lock()

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

    def _changes(self, result: dict, show_change: bool | None = None) -> dict:
        """What an action changed, filled in by _observe(). Inside an input action it runs after
        _act has released the seat lock; the result dict is filled in place then."""
        if self._deferring:
            self._deferred = (result, show_change)
            return result
        return self._observe(result, show_change)

    def _observe(self, result: dict, show_change: bool | None = None) -> dict:
        """What the action changed. With a tree of the window it landed in, the lines that changed;
        otherwise (no tree, another window than the tree's, or a tree that can no longer be read)
        whether the receiving window's pixels changed (see _guard and screendiff), and with
        show_change, a small capture of that area so no screenshot is needed to see it."""
        self._last_area = None
        before, self._pending = self._pending, None
        pixels, self._pixel_report = self._pixel_report, False
        tree = self.a11y._last is not None
        if before is None and not tree:
            if show_change:
                result["note"] = "show_change needs ImageMagick to see what changed; take a screenshot"
            return result
        if before is not None and (pixels or not tree):
            return self._observe_pixels(result, before, show_change)
        try:
            changes = self.a11y.changes(settle=SETTLE)
        except CcdeskError as exc:
            self._tree_unreadable = True  # the next actions compare pixels, until ui_tree() reads again
            if before is not None:
                return self._observe_pixels(result, before, show_change)
            result["note"] = f"could not re-read the window: {exc}. Take a screenshot to see the result"
            return result
        self._tree_unreadable = False
        if changes is not None:
            result["changes"] = changes
        return result

    def _observe_pixels(self, result: dict, before: tuple, show_change: bool | None) -> dict:
        """show_change None (the default): attach the capture of what changed; True: as None, and
        say so when it cannot be done; False: never."""
        region, grid, wid = before
        after = self._settled_snapshot(region, grid, wid)
        if wid is not None:
            result["covered"] = True  # compared and pictured from the window's own pixels
            if after is None:
                result["note"] = (f"your window is covered and its own pixels could not be read after the action "
                                  f"({self.backend.window_note}); take a screenshot(window=...) to see the result")
                return result
        if after is not None:
            self._after = (region, after, wid)
            box = changed_box(grid, after)
            result["screen_changed"] = box is not None
            if box:
                result["changed_area"] = dict(zip(("x", "y", "width", "height"), box))
                self._last_area = Region(*box)
                if self._moving:
                    result["settled"] = False  # still changing: wait_for_change() waits for it to stop
                if show_change is not False:
                    result["change_shot"] = self._change_shot(Region(*box), wid, region)
        if show_change and "change_shot" not in result and "screen_changed" not in result:
            result["note"] = "show_change needs ImageMagick to see what changed; take a screenshot"
        return result

    def _remember_shot(self, origin: tuple[int, int], size: tuple[int, int], shown: tuple[int, int],
                       layout: tuple | None = None) -> str:
        """Give a picture an id and keep what turns its pixels into screen pixels, and the layout it
        was taken in (None: unknown). size is the capture's size on screen, shown the size of the
        picture handed out."""
        with self._shots_lock:
            self._shot_count += 1
            sid = f"s{self._shot_count}"
            self._shots[sid] = (*origin, shown[0] / size[0], shown[1] / size[1], *shown)
            self._shot_layouts[sid] = layout
            while len(self._shots) > MAX_SHOTS:
                self._shot_layouts.pop(self._shots.popitem(last=False)[0], None)
        return sid

    def _from_shot(self, shot, x, y) -> tuple[int, int]:
        """Screen pixel of pixel (x, y) in a picture this server handed out: origin + pixel / scale."""
        x, y = check_int("x", x), check_int("y", y)
        with self._shots_lock:
            known = self._shots.get(shot) if isinstance(shot, str) else None
        if known is None:
            raise ValidationError(f"unknown or expired shot {shot!r} (the latest {MAX_SHOTS} are kept): "
                                  f"take a new screenshot and use its shot id")
        ox, oy, sx, sy, width, height = known
        if not (0 <= x < width and 0 <= y < height):
            raise ValidationError(f"({x},{y}) is outside shot {shot}, which is {width}x{height}: pass pixels "
                                  f"of that picture, or take a new screenshot")
        return math.floor(ox + x / sx + 0.5), math.floor(oy + y / sy + 0.5)

    def _screen_point(self, shot, x, y) -> tuple[int, int]:
        """x, y as screen pixels: with shot, pixels of that picture, converted; else as given."""
        if shot is not None:
            x, y = self._from_shot(shot, x, y)
        return self._point(x, y)

    @staticmethod
    def _from(result: dict, shot) -> dict:
        if shot is not None:
            result["from_shot"] = shot
        return result

    def _padded(self, box: Region) -> Region:
        """box with SHOT_PAD around it, clipped to the screen."""
        x0, y0 = max(0, box.x - SHOT_PAD), max(0, box.y - SHOT_PAD)
        x1, y1 = box.x + box.width + SHOT_PAD, box.y + box.height + SHOT_PAD
        size = self.screen_size()
        if size:
            x1, y1 = min(x1, size[0]), min(y1, size[1])
        return Region(x0, y0, max(1, x1 - x0), max(1, y1 - y0))

    def _own_pixels(self, window: Window | None) -> str | None:
        """The id to read this window's own pixels by, when a crop of the screen would show another
        window instead: an agent's window with something over it (the user's, or another agent's).
        None otherwise, also for a window nothing covers: the screen then shows the same pixels,
        plus the menus and dropdowns the window opens, which are windows of their own."""
        if not self.agent or window is None:
            return None
        try:
            return window.id if self.backend.window_covered(window.id) else None
        except CcdeskError:
            return None

    def _capture(self, out: Path, reg: Region | None, wid: str | None = None) -> None:
        """Write the picture of reg: a crop of the screen, or with wid that window's own pixels."""
        if wid is None:
            self.backend.screenshot(str(out), reg)
            return
        if not self.backend.window_screenshot(str(out), wid, reg):
            raise CcdeskError(f"the covered window's own pixels could not be read ({self.backend.window_note})")
        if reg is not None and png_size(out) != (reg.width, reg.height):
            # Clipped to the window: the picture's origin is no longer reg's, so its pixels cannot be placed.
            raise CcdeskError("the covered window's own pixels do not fill the area asked for")

    @staticmethod
    def _clip(reg: Region, to: Region | None) -> Region:
        if to is None:
            return reg
        x0, y0 = max(reg.x, to.x), max(reg.y, to.y)
        x1, y1 = min(reg.x + reg.width, to.x + to.width), min(reg.y + reg.height, to.y + to.height)
        return Region(x0, y0, max(1, x1 - x0), max(1, y1 - y0))

    def _change_shot(self, box: Region, wid: str | None = None, within: Region | None = None) -> dict:
        """A capture of what an action changed, scaled down to about SHOT_MAX_TOKENS. With wid, of
        that window's own pixels, inside `within` (the window's region)."""
        reg = self._padded(box)
        if wid is not None:
            reg = self._clip(reg, within)
        try:
            shot, factor = self._scaled_capture(reg, SHOT_MAX_TOKENS, wid)
        except (CcdeskError, OSError, ValueError) as exc:
            return {"error": f"could not capture the change: {exc}"}
        if factor < 1:
            shot["scale"] = round(factor, 3)
        return shot

    def _scaled_capture(self, reg: Region, max_tokens: int, wid: str | None = None) -> tuple[dict, float]:
        """Capture reg (with wid: from that window's own pixels), scaled down to about max_tokens
        (tokens ~ width*height/750).
        Returns the shot (path, width, height, approx_tokens, origin) and the scale factor used."""
        out = self._shot_path(None)
        self._capture(out, reg, wid)
        full = width, height = png_size(out)
        factor = min(1.0, (max_tokens * 750 / (width * height)) ** 0.5)
        if factor < 1:
            self.backend.scale_png(str(out), max(1, round(width * factor)), max(1, round(height * factor)))
            width, height = png_size(out)
        sid = self._remember_shot((reg.x, reg.y), full, (width, height), self._layout())
        return ({"path": str(out), "width": width, "height": height, "approx_tokens": round(width * height / 750),
                 "origin": {"x": reg.x, "y": reg.y}, "shot": sid}, factor)

    def _tree_fallback_shot(self, title: str | None) -> dict:
        """A picture of one window for ui_tree() when it has no useful tree. Raises CcdeskError if
        the window cannot be found or captured."""
        if title is None:
            wid = self.backend.active_window()
            if wid is None:
                raise CcdeskError("no active window to capture")
            title = wid
        window, reg, _ = self._shown_window_region(title)
        wid = self._own_pixels(window)
        try:
            shot, factor = self._scaled_capture(reg, TREE_SHOT_MAX_TOKENS, wid)
        except CcdeskError:
            if wid is None:
                raise
            wid = None
            shot, factor = self._scaled_capture(reg, TREE_SHOT_MAX_TOKENS)
            shot["warning"] = self._covered_warning()
        if wid is not None:
            shot["covered"] = True
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

    def _need_target(self) -> None:
        """An agent works in the window it chose: without one its input would go to whatever the
        user has in front. Not overridden by force."""
        if self.agent and self._target is None:
            raise TargetRequiredError("this agent has no window yet, so its input would reach the user's: call "
                                      "list_windows(), then activate_window() on your own window first "
                                      "(force=true does not change this)")

    def _hidden(self, window: Window) -> bool:
        """Whether the window is minimized or on another desktop, which is when showing it takes
        the window manager (and so the user's focus). The same test the own-pointer backend uses."""
        try:
            if self.backend.window_on_show(window.id) is False:
                return True
            desktop = self.backend.current_desktop()
        except CcdeskError:
            return False
        return (window.desktop is not None and window.desktop >= 0
                and desktop not in (None, window.desktop))

    def _guard(self, force: bool, point: tuple[int, int] | None = None,
               expect_title: str | None = None, layout: bool = False, shot: str | None = None) -> Window | None:
        """Refuse input when focus moved away from the window of the latest activate_window(), e.g.
        because the user switched to their terminal, or when the receiving window's title lacks
        expect_title (a browser tab: the window stays the same when the user switches tabs).
        With layout=True (input at coordinates taken from a screenshot, or from `shot`), also refuse
        when the window layout changed since that picture. An agent (--agent) is also refused
        without a window of its own, and never asks the window manager to show its window.
        Returns the receiving window for the result, and keeps a small copy of it for _changes()
        to compare with afterwards."""
        if expect_title is not None and (not isinstance(expect_title, str) or not expect_title.strip()):
            raise ValidationError("expect_title must be a non-empty string, or left out")
        self._need_target()
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
        hidden = None  # whether the agent's window is off screen, once it matters
        if self.agent and target and not force and target.id in windows:
            away = window is not None and not self._belongs_to_target(window)
            if away or (self.backend.own_pointer and point is not None):
                hidden = self._hidden(windows[target.id])
            if hidden and self.backend.own_pointer:
                # Showing it goes through the window manager, which takes the user's keyboard focus
                # and can switch their desktop: only activate_window() does that, on purpose.
                raise TargetHiddenError(
                    f"your window from activate_window() ({target.title!r}, {target.id}) is minimized or on "
                    f"another desktop, so nothing was sent. activate_window({target.id!r}) would bring it onto "
                    f"the user's screen and take their keyboard focus once: do that only if the task needs it")
            if away:
                # Another agent on this screen moved focus or raised its window over ours. We hold the
                # seat lock, so take our window back and check again. With an own pointer that is only
                # this agent's keyboard (the window is on show here); without one it is the shared focus.
                self.backend.activate_window(windows[target.id])
                note = self.backend.take_note()
                if note:
                    self._note = note
                window = self._receiver(windows, point)
        if target and not force and window is not None and not self._belongs_to_target(window):
            what = f"the pointer at {point[0]},{point[1]} is over" if point else "keyboard focus is on"
            if target.id not in windows:
                raise PolicyError(f"{what} {window.title!r} ({window.id}), and the window from "
                                  f"activate_window() ({target.title!r}, {target.id}) is gone; call "
                                  f"list_windows() and activate_window() again, or pass force=true")
            if self.agent and point is not None and hidden is False:
                raise TargetCoveredError(
                    f"the point {point[0]},{point[1]} of your window {target.title!r} ({target.id}) is covered "
                    f"by {window.title!r} ({window.id}), which belongs to the user or another agent. Do not "
                    f"raise your window: use click_element() (it reaches a covered window when the app has an "
                    f"accessibility tree) or the keyboard, or wait() and retry; force=true sends the click "
                    f"anyway")
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
        if layout and not force:
            self._check_layout(list(windows.values()) if windows else None, window or target, shot)
        # The tree answers for an action only when it lands in the tree's window and can be read;
        # anything else is reported from the pixels, so a stale tree never hides the result.
        pixels = self.a11y._last is None or self._tree_unreadable or not self._in_trees_window(window)
        self._pixel_report = (self._pixel_report or pixels) if self._batching else pixels  # a batch: any step
        if self._batching and self._pending is not None:
            return window  # a batch is compared with the screen before its first step
        self._pending = None
        # With a tree of the right window the baseline is only the fallback for a failed re-read:
        # skip it where a capture is known to be slow.
        if pixels or self._grid_fast is not False:
            region = self._window_region(window)
            wid = self._own_pixels(window) if region is not None else None
            grid = self._snapshot(region, wid) if wid is not None else None
            if grid is None:
                wid, grid = None, self._snapshot(region)
            self._pending = (region, grid, wid) if grid is not None else None
        return window

    def _in_trees_window(self, window: Window | None) -> bool:
        """Whether an action on this window is covered by the latest ui_tree(). The titles come from
        the window manager and the accessibility bus, which do not always spell them the same, so one
        containing the other counts. An unknown window or title: as before, the tree answers."""
        tree = (self.a11y.tree_window() or "").casefold().strip()
        title = (window.title if window is not None else "").casefold().strip()
        return not tree or not title or tree in title or title in tree

    def _layout(self, windows: list[Window] | None = None) -> tuple | None:
        """A fingerprint of what is on screen: its size, every window's id, title, geometry and
        place in the backend's list, the active window and the desktop. None if the backend
        cannot tell (then nothing is checked). For an agent (--agent) only its own window
        counts, see _agent_layout."""
        if self.agent:
            return self._agent_layout(windows)
        try:
            if windows is None:
                windows = self.backend.list_windows()
            active = self.backend.active_window()
            desktop = self.backend.current_desktop()
        except (CcdeskError, AttributeError):
            return None
        rows = tuple(sorted((w.id, w.title, w.x, w.y, w.width, w.height, i) for i, w in enumerate(windows)))
        return (self.screen_size(), rows, active, desktop)

    def _agent_layout(self, windows: list[Window] | None = None) -> tuple | None:
        """What an agent's coordinates depend on: ("agent", its window's id, the screen size, the
        window's box or None if it is not listed, whether it is on show). Other windows, focus and
        the current desktop are the user's business; the receiver check refuses when something covers
        the point. None without a window of its own, or when the backend cannot tell."""
        target = self._target
        if target is None:
            return None
        try:
            if windows is None:
                windows = self.backend.list_windows()
            now = next((w for w in windows if w.id == target.id), None)
            on_show = now is not None and not self._hidden(now)
        except (CcdeskError, AttributeError):
            return None
        box = (now.x, now.y, now.width, now.height) if now is not None else None
        return ("agent", target.id, self.screen_size(), box, on_show)

    @staticmethod
    def _geometry(layout: tuple) -> tuple:
        """The layout without any title: pages retitle their window as they load, and that does not
        move a single pixel, so it must not invalidate coordinates."""
        size, rows, active, desktop = layout
        return (size, tuple((r[0], *r[2:]) for r in rows), active, desktop)

    def _check_layout(self, windows: list[Window] | None, receiver: Window | None,
                      shot: str | None = None) -> None:
        """Refuse coordinate input, before sending anything, when the windows moved, opened, closed,
        restacked or changed focus since the picture the coordinates come from: `shot` if given, else
        the latest screenshot. A fresh screenshot comes with it. For an agent, see _agent_layout."""
        with self._shots_lock:
            seen = self._layout_seen if shot is None else self._shot_layouts.get(shot)
        if seen is None or windows is None:
            return
        now = self._layout(windows)
        if now is None:
            return
        since = "the latest screenshot" if shot is None else f"picture {shot}"
        if self.agent:
            self._check_agent_layout(seen, now, windows, since)
            return
        if self._target is not None and now[2] == self._target.id:
            # Focus came back to our own activate_window() window, e.g. another agent on this
            # screen had it when the screenshot was taken and _guard just took it back: the
            # receiver check already ensures the input lands there, so focus alone is no reason.
            seen = (seen[0], seen[1], now[2], seen[3])
        if self._geometry(seen) == self._geometry(now):
            return
        what = describe_layout_change(seen, now)
        fresh = self._fresh_screenshot(now)
        see = (f"Read the fresh screenshot {fresh} and plan again" if fresh
               else "Take a screenshot and plan again")
        raise StaleFrameError(f"the window layout changed since {since} ({what}), so its "
                              f"coordinates may now hit something else. {see} (do not reuse the old "
                              f"coordinates), or pass force=true to send it anyway")

    def _check_agent_layout(self, seen: tuple, now: tuple, windows: list[Window], since: str) -> None:
        """The agent's picture is stale when its window moved, was resized, went off screen or the
        screen changed size. The fresh picture is of that window's region, never of the whole screen."""
        if seen[1] != now[1] or seen == now:
            return  # a picture taken for another window of this agent, or nothing changed
        parts = []
        if seen[2] != now[2]:
            parts.append(f"screen {seen[2]} -> {now[2]}")
        if seen[3] != now[3]:
            parts.append(f"it moved or was resized: {self._box_text(seen[3])} -> {self._box_text(now[3])}")
        if seen[4] != now[4]:
            parts.append("it is now on show" if now[4] else "it is no longer on show")
        what = f"your window {self._target.title!r} ({now[1]}): " + "; ".join(parts)
        region = own = None
        if now[4]:
            mine = next((w for w in windows if w.id == now[1]), None)
            region = self._window_region(mine)
            own = self._own_pixels(mine)
        fresh = self._fresh_screenshot(now, region, own) if region else None
        see = (f"Read the fresh picture of your window {fresh} and plan again" if fresh
               else "Take a screenshot(window=...) of your window and plan again")
        raise StaleFrameError(f"the window layout changed since {since} ({what}), so its "
                              f"coordinates may now hit something else. {see} (do not reuse the old "
                              f"coordinates), or pass force=true to send it anyway")

    @staticmethod
    def _box_text(box: tuple | None) -> str:
        return "gone" if box is None else f"{box[0]},{box[1]} {box[2]}x{box[3]}"

    def _fresh_screenshot(self, layout: tuple, region: Region | None = None, wid: str | None = None) -> str | None:
        """Screenshot for a gui_frame_stale refusal (the whole screen, or `region`: an agent's
        window, from its own pixels when wid says it is covered); it becomes the latest delivered one."""
        try:
            out = self._shot_path(None)
            self._capture(out, region, wid)
        except (CcdeskError, OSError, ValueError):
            return None  # a covered window that cannot be read: no picture beats one of another window
        if not out.exists():
            return None
        self._layout_seen = layout
        try:
            size = png_size(out)
        except (CcdeskError, OSError, ValueError):
            return str(out)
        origin = (region.x, region.y) if region else (0, 0)
        sid = self._remember_shot(origin, size, size, layout)
        return f"{out} (shot {sid}" + (f", origin {origin[0]},{origin[1]})" if region else ")")

    def _with_window(self, result: dict, window: Window | None) -> dict:
        if window:
            result["window"] = {"id": window.id, "title": window.title}
        if self._yielded:
            result["yielded_ms"] = self._yielded
        if self._note:  # what the backend said while this action took the agent's window back
            result["note"] = " ".join(filter(None, [result.get("note"), self._note]))
            self._note = None
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

    def _settled_snapshot(self, region: Region | None, before: Grid, wid: str | None = None) -> Grid | None:
        """The screen after an action. With a direct capture (a few tens of ms): nothing changed by
        SETTLE, return it; something changed, keep capturing until two captures in a row are equal,
        up to MAX_SETTLE, and say in self._moving if it was still moving then. With the PNG path
        (two hundred ms a capture), wait SETTLE and capture once."""
        self._moving = False
        if not self._grid_fast:
            self.backend.sleep(SETTLE)
            return self._snapshot(region, wid)
        start, prev = self.clock(), None
        while True:
            self.backend.sleep(SETTLE_POLL)
            cur = self._snapshot(region, wid)
            if cur is None:
                return cur
            elapsed = self.clock() - start
            if changed_box(before, cur) is None:
                if elapsed >= SETTLE:
                    return cur
            elif prev is not None and cur.gray == prev.gray:
                return cur
            elif elapsed >= MAX_SETTLE:
                self._moving = True
                return cur
            prev = cur

    def _snapshot(self, region: Region | None, wid: str | None = None) -> Grid | None:
        """A grayscale copy of the screen at 1/DIFF_CELL size, or None (no ImageMagick, or it failed).
        With wid, of that window's own pixels, or None when they cannot be read: never the screen
        instead, which would be compared with a picture of something else."""
        if not self.screen_diff:
            return None
        if wid is not None:
            fast = self.backend.window_gray_grid(wid, region, DIFF_CELL)
            if fast is None:
                return None
            self._grid_fast = True
        else:
            fast = self.backend.gray_grid(region, DIFF_CELL)
            self._grid_fast = fast is not None
        if fast is not None:
            cols, rows, gray, origin = fast
            return Grid(cols, rows, gray, origin, scale=(float(DIFF_CELL), float(DIFF_CELL)))
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
            own, warning = self._own_pixels(shot_of), None
            if own is not None:
                try:
                    self._capture(out, reg, own)
                except (CcdeskError, OSError, ValueError):
                    own, warning = None, self._covered_warning()
            if own is None:
                self.backend.screenshot(str(out), reg)
            if not out.exists():
                raise CcdeskError(f"screenshot tool reported success but {out} was not written")
            # Coordinates measured on this picture are only valid while the layout holds.
            layout = self._layout_seen = self._layout()
            full = width, height = png_size(out)
            if factor < 1:
                self.backend.scale_png(str(out), max(1, round(width * factor)), max(1, round(height * factor)))
                width, height = png_size(out)
            sid = self._remember_shot((reg.x, reg.y) if reg else (0, 0), full, (width, height), layout)
            # Image input costs about width*height/750 tokens once the model reads it.
            result = {"path": str(out), "width": width, "height": height, "approx_tokens": round(width * height / 750),
                      "shot": sid}
            if factor < 1:
                result["scale"] = factor
            if shot_of:
                result["window"] = {"id": shot_of.id, "title": shot_of.title}
            if own is not None:
                result["covered"] = True  # the window's own pixels: what is on top of it is not in the picture
            if warning:
                result["warning"] = warning
            if reg:
                result["origin"] = {"x": reg.x, "y": reg.y}
            if reg or factor < 1:
                result["note"] = f'pass shot="{sid}" with this picture\'s own pixel coordinates'
            if unchecked and not warning:
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

    def _covered_warning(self) -> str:
        return (f"this window is covered and its own pixels could not be read ({self.backend.window_note}): "
                f"the picture is a crop of the screen and shows what is on top of it")

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
                f"(this one: ~{tokens}); the action's own answer shows it already, unless show_change=false")

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
              expect_title: str | None = None, show_change: bool | None = None, shot: str | None = None) -> dict:
        def do():
            self._ready()
            px, py = self._screen_point(shot, x, y)
            btn = parse_button(button)
            n = check_int("count", count, 1, 10)
            window = self._guard(force, (px, py), expect_title, layout=True, shot=shot)
            self.backend.click(px, py, btn, n)
            self._show_cursor(px, py)
            return self._changes(self._from(self._with_window(
                {"clicked": {"x": px, "y": py}, "button": button, "count": n}, window), shot), show_change)

        args = {"x": x, "y": y, "button": button, "count": count, "force": force,
                "expect_title": expect_title, "show_change": show_change}
        return self._act("click", args | ({"shot": shot} if shot is not None else {}), do)

    def move(self, x: int, y: int, shot: str | None = None) -> dict:
        def do():
            self._ready()
            px, py = self._screen_point(shot, x, y)
            self._yielded = self._yield_to_user()
            self.backend.move(px, py)
            self._show_cursor(px, py)
            return self._from(self._with_window({"moved": {"x": px, "y": py}}, None), shot)

        return self._act("move", {"x": x, "y": y} | ({"shot": shot} if shot is not None else {}), do)

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int, duration_ms: int = 400,
             force: bool = False, show_change: bool | None = None, shot: str | None = None) -> dict:
        def do():
            self._ready()
            x0, y0 = self._screen_point(shot, from_x, from_y)
            x1, y1 = self._screen_point(shot, to_x, to_y)
            ms = check_int("duration_ms", duration_ms, 0, 10_000)
            window = self._guard(force, (x0, y0), layout=True, shot=shot)
            self.backend.drag(x0, y0, x1, y1, ms)
            self._show_cursor(x1, y1)
            return self._changes(self._from(self._with_window(
                {"from": {"x": x0, "y": y0}, "to": {"x": x1, "y": y1}, "duration_ms": ms}, window), shot),
                show_change)

        args = {"from_x": from_x, "from_y": from_y, "to_x": to_x, "to_y": to_y, "duration_ms": duration_ms,
                "force": force, "show_change": show_change}
        return self._act("drag", args | ({"shot": shot} if shot is not None else {}), do)

    def scroll(self, x: int, y: int, dx: int = 0, dy: int = 0, force: bool = False,
               show_change: bool | None = None, shot: str | None = None) -> dict:
        def do():
            self._ready()
            px, py = self._screen_point(shot, x, y)
            sx = check_int("dx", dx, -50, 50)
            sy = check_int("dy", dy, -50, 50)
            if not sx and not sy:
                raise ValidationError("dx and dy are both 0; nothing to scroll. Pass dy=3 to scroll down, dy=-3 up, "
                                      "dx=3 right (there is no direction or amount argument)")
            window = self._guard(force, (px, py), layout=True, shot=shot)
            self.backend.scroll(px, py, sx, sy)
            self._show_cursor(px, py)
            return self._changes(self._from(self._with_window(
                {"at": {"x": px, "y": py}, "dx": sx, "dy": sy}, window), shot), show_change)

        args = {"x": x, "y": y, "dx": dx, "dy": dy, "force": force, "show_change": show_change}
        return self._act("scroll", args | ({"shot": shot} if shot is not None else {}), do)

    def type_text(self, text: str, submit: bool = False, replace: bool = False, force: bool = False,
                  expect_title: str | None = None, show_change: bool | None = None) -> dict:
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
                  force: bool = False, expect_title: str | None = None, show_change: bool | None = None) -> dict:
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
            # Raising a window restacks and refocuses: any screenshot taken before this is stale.
            # (An agent's picture only depends on its own window, which this does not move.)
            if not self.agent:
                self._layout_seen = None
                with self._shots_lock:
                    self._shot_layouts.clear()
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
            if window is None:
                self._need_target()  # no window named: it would read the one the user has in front
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
            self._tree_unreadable = False
            text = result["tree"]
            result["approx_tokens"] = len(text) // 4
            if result["truncated"]:
                result["note"] = "truncated: narrow it with query=..., page=true or window=..."
            if self.backend.name in ("wayland", "kde") and "origin" not in result:
                result["note"] = ("on Wayland, @x,y may be relative to the window (this compositor does "
                                  "not say where it is); verify with a screenshot")
            thin = tree_is_empty(text)
            blank_page = not thin and empty_page(text)
            if shot_fallback and not q and (thin or blank_page):
                why = ("the web page in it has no tree (a browser started before the accessibility "
                       "bus was on shows only its toolbars; restarting the browser fixes it)"
                       if blank_page else "the tree is nearly empty")
                try:
                    result["fallback_shot"] = self._tree_fallback_shot(result.get("window"))
                except (CcdeskError, OSError, ValueError):
                    pass  # keep the (thin) tree; the model can still call screenshot()
                else:
                    self.a11y.forget()  # nothing to compare: actions report pixel changes instead
                    result["note"] = " ".join(filter(None, [result.get("note"),
                                                            f"{why}. {TREE_SHOT_NOTE}"]))
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
            self._need_target()
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
            # An agent's force never carries a click into a window that is not its own: with a tree
            # of another window the guard runs in full, and the click goes out only if it lands in
            # the agent's window.
            forced = force and not (self.agent and not self._may_skip_pointer(False))
            window = self._guard(forced, (px, py), expect_title)
            self.backend.click(px, py, btn, n)
            self._show_cursor(px, py)
            return self._changes(self._with_window({"clicked": {"x": px, "y": py}, "element": label,
                                                    "button": button, "count": n}, window))

        return self._act("click_element", {"ref": ref, "button": button, "count": count, "force": force,
                                             "expect_title": expect_title}, do)

    def _may_skip_pointer(self, force: bool) -> bool:
        """A native action reaches its element wherever the pointer and focus are, so _guard has
        nothing to check, unless the tree is of another window than activate_window() chose. For an
        agent, force does not skip it: a tree of a window that is not its own never does."""
        if self.agent:
            return self._target is not None and self.a11y.tree_window() == self._target.title
        return force or self._target is None or self.a11y.tree_window() == self._target.title

    def _before_native(self) -> None:
        self._yielded = self._yield_to_user()
        self._after = None
        if not self._batching:
            self._pending = None
            self._pixel_report = False

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
            self._need_target()
            key = str(ref).strip()
            node = self.a11y.resolve(key)
            label = f'{node["role"]} {node["name"]!r}'
            if not node.get("showing"):
                raise CcdeskError(f"{key} ({label}) is not on screen; scroll it into view, then call ui_tree() again")
            if not self._may_skip_pointer(force):
                raise PolicyError(f"{key} is in {self.a11y.tree_window()!r}, not {self._target.title!r} from "
                                  f"activate_window(); call ui_tree() on that window"
                                  + ("" if self.agent else ", or pass force=true"))
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
            if args.get("shot") is not None:  # a picture that is unknown or too small stops the batch before step 1
                ends = ([(args["from_x"], args["from_y"]), (args["to_x"], args["to_y"])] if name == "drag"
                        else [(args["x"], args["y"])])
                try:
                    for px, py in ends:
                        self._from_shot(args["shot"], px, py)
                except ValidationError as exc:
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

    def batch(self, steps: list[dict], show_change: bool | None = None) -> dict:
        """Run several input steps in one call, holding the seat for all of them, and observe only
        the end state. Stops at the first step that fails."""
        checked = self._batch_steps(steps)
        logged = [{"tool": n, **_redact(a)} for n, a in checked]

        def do():
            if self.agent and self._target is None:
                for name, _ in checked:  # before any step is sent, not halfway
                    if name == "activate_window":
                        break
                    if name in NEEDS_TARGET:
                        self._need_target()
            done: list[dict] = []
            result: dict = {"done": 0, "steps": done}
            self._batching, self._pending, self._pixel_report = True, None, False
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
            wid = None
            if reg is None and self._after is not None:
                reg, base, wid = self._after  # a page that loaded while the model was thinking counts
            else:
                if reg is None and self.agent and self._target is not None:
                    # An agent whose window is covered waits for its window, not for the user's on top.
                    try:
                        mine = self.find_window(self._target.id)
                    except CcdeskError:
                        mine = None
                    own, box = self._own_pixels(mine), self._window_region(mine)
                    if own is not None and box is not None:
                        base = self._snapshot(box, own)
                        if base is not None:
                            reg, wid = box, own
                if wid is None:
                    base = self._snapshot(reg)
            if base is None:
                raise UnsupportedError("wait_for_change needs ImageMagick (magick or convert) to compare "
                                       "screenshots; use wait(seconds) instead")
            prev, area, last_motion = base, None, None
            while (elapsed := self.clock() - start) < limit:
                self.backend.sleep(min(POLL, limit - elapsed))
                current = self._snapshot(reg, wid)
                if current is None:
                    continue
                if changed_box(prev, current):
                    last_motion = self.clock()
                prev = current
                area = changed_box(base, current) or area
                if area and last_motion is not None and self.clock() - last_motion >= settle:
                    break
            self._after = (reg, prev, wid)  # the next wait_for_change waits for the next change
            waited = round(min(self.clock() - start, limit) * 1000)
            covered = {"covered": True} if wid is not None else {}
            if area is None:
                return {"changed": False, "waited_ms": waited, **covered,
                        "note": f"nothing changed in {limit:g}s; take a screenshot to see why"}
            settled = self.clock() - last_motion >= settle
            self._last_area = Region(*area)
            return {"changed": True, "settled": settled, "waited_ms": waited, **covered,
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
