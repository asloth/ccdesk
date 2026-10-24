"""The action layer: validation, safety policy and audit around a backend.

Everything the MCP server exposes is a method here, so it can be tested without MCP.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from ccdesk.a11y import A11y, WindowNotFound, bus_enabled, missing_hint
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
from ccdesk.safety import INPUT_TOOLS, AuditLog, Policy, cache_dir
from ccdesk.screendiff import Grid, changed_box

MAX_TEXT = 10_000
MAX_WAIT = 300.0
MAX_WAIT_FOR_CHANGE = 300.0
DIFF_CELL = 8  # screen pixels per cell when comparing before/after captures
POLL = 0.2  # seconds between captures in wait_for_change
MAX_YIELD = 15.0  # seconds input waits for the user to pause (--yield-ms) before it is refused
OWN_INPUT_SLACK_MS = 150  # xdotool's events can land this long after it returns
KEEP_SCREENSHOTS = 200
SETTLE = 0.3  # seconds an app gets to react before an action reports its changes
KEY_SETTLE = 0.15  # between select-all, the text and Return in type_text
FOCUS_SETTLE = 0.3  # after a shortcut that moves focus (ctrl+t, ctrl+l, ...)
SELECT_ALL = KeyCombo("a", ("ctrl",))
RETURN = KeyCombo("Return")


class Desktop:
    def __init__(self, backend: Backend | None = None, policy: Policy | None = None,
                 audit: AuditLog | None = None, shots_dir: str | os.PathLike | None = None,
                 a11y: A11y | None = None, headless=None, screen_diff: bool = True):
        self.backend = backend or get_backend()
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
        self.clock = time.monotonic
        self._own_input: float | None = None  # clock() when ccdesk's latest input action finished
        self._yielded = 0  # ms the current action waited for the user (--yield-ms)

    # -- plumbing -------------------------------------------------------------

    def _act(self, tool: str, args: dict, fn, policy_tool: str | None = None, loggable=None):
        start = time.monotonic()
        try:
            self.policy.check(policy_tool or tool)
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
        self.audit.record(tool, args, "ok", result=loggable(result) if loggable else result,
                          duration_ms=(time.monotonic() - start) * 1000)
        return result

    def _input_done(self, tool: str) -> None:
        if tool in INPUT_TOOLS:
            self._own_input = self.clock()

    def _yield_to_user(self) -> int:
        """With --yield-ms, hold input while the user is using the mouse or keyboard: wait until
        they have been idle that long, up to MAX_YIELD. Returns how long it waited, in ms."""
        need = self.policy.yield_ms
        if not need:
            return 0
        waited = 0.0
        while True:
            try:
                idle = self.backend.idle_ms()
            except CcdeskError:
                idle = None
            if idle is None:
                return round(waited * 1000)  # cannot tell: don't block
            # Our own xdotool events reset the counter too: only input after ours is the user's.
            since_ours = (self.clock() - self._own_input) * 1000 if self._own_input is not None else float("inf")
            if idle >= need or idle >= since_ours - OWN_INPUT_SLACK_MS:
                return round(waited * 1000)
            if waited >= MAX_YIELD:
                raise PolicyError(f"the user is using the mouse or keyboard (last input {idle} ms ago; "
                                  f"waited {waited:.0f}s for a {need} ms pause). Don't race them: wait() "
                                  f"and retry, or ask them to step away")
            pause = min(max((need - idle) / 1000, 0.1), MAX_YIELD - waited)
            self.backend.sleep(pause)
            waited += pause

    def _changes(self, result: dict) -> dict:
        """After ui_tree() has been used, add what the action changed in that window. Without a
        tree, say whether the receiving window's pixels changed (see _guard and screendiff)."""
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
            return result
        time.sleep(SETTLE)
        try:
            changes = self.a11y.changes()
        except CcdeskError as exc:
            changes = f"could not re-read the window: {exc}"
        if changes is not None:
            result["changes"] = changes
        return result

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
            if wid not in windows:  # menus and dropdowns are not in list_windows: they belong to the focus
                wid = self.backend.active_window()
        except CcdeskError:
            return None
        return windows.get(wid)

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

    def _guard(self, force: bool, point: tuple[int, int] | None = None) -> Window | None:
        """Refuse input when focus moved away from the window of the latest activate_window(), e.g.
        because the user switched to their terminal. Returns the receiving window for the result,
        and keeps a small copy of it for _changes() to compare with afterwards."""
        self._yielded = self._yield_to_user()  # first: focus may move while the user finishes
        try:
            windows = {w.id: w for w in self.backend.list_windows()}
        except CcdeskError:
            windows = {}
        window = self._receiver(windows, point) if windows else None
        target = self._target
        if target and not force and window is not None and not self._belongs_to_target(window):
            what = f"the pointer at {point[0]},{point[1]} is over" if point else "keyboard focus is on"
            if target.id not in windows:
                raise PolicyError(f"{what} {window.title!r} ({window.id}), and the window from "
                                  f"activate_window() ({target.title!r}, {target.id}) is gone; call "
                                  f"list_windows() and activate_window() again, or pass force=true")
            raise PolicyError(f"{what} {window.title!r} ({window.id}), not {target.title!r} from "
                              f"activate_window(): the user may have switched windows. Call "
                              f"activate_window({target.id!r}) again, or pass force=true to send it anyway")
        self._after = None  # wait_for_change() compares with the screen after this action, not before
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

    def screenshot(self, region=None, path: str | None = None, scale: float = 1.0) -> dict:
        def do():
            self._ready()
            reg = parse_region(region, self.screen_size())
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
            if reg:
                result["origin"] = {"x": reg.x, "y": reg.y}
            if reg and factor < 1:
                result["note"] = "pixel (px,py) in this image is screen (origin.x+px/scale, origin.y+py/scale)"
            elif reg:
                result["note"] = "pixel (px,py) in this image is screen (origin.x+px, origin.y+py)"
            elif factor < 1:
                result["note"] = "pixel (px,py) in this image is screen (px/scale, py/scale)"
            return result

        return self._act("screenshot", {"region": region, "path": path, "scale": scale}, do)

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

    def click(self, x: int, y: int, button: str = "left", count: int = 1, force: bool = False) -> dict:
        def do():
            self._ready()
            px, py = self._point(x, y)
            btn = parse_button(button)
            n = check_int("count", count, 1, 10)
            window = self._guard(force, (px, py))
            self.backend.click(px, py, btn, n)
            return self._changes(self._with_window({"clicked": {"x": px, "y": py}, "button": button, "count": n},
                                                   window))

        return self._act("click", {"x": x, "y": y, "button": button, "count": count, "force": force}, do)

    def move(self, x: int, y: int) -> dict:
        def do():
            self._ready()
            px, py = self._point(x, y)
            self._yielded = self._yield_to_user()
            self.backend.move(px, py)
            return self._with_window({"moved": {"x": px, "y": py}}, None)

        return self._act("move", {"x": x, "y": y}, do)

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int, duration_ms: int = 400,
             force: bool = False) -> dict:
        def do():
            self._ready()
            x0, y0 = self._point(from_x, from_y)
            x1, y1 = self._point(to_x, to_y)
            ms = check_int("duration_ms", duration_ms, 0, 10_000)
            window = self._guard(force, (x0, y0))
            self.backend.drag(x0, y0, x1, y1, ms)
            return self._changes(self._with_window({"from": {"x": x0, "y": y0}, "to": {"x": x1, "y": y1},
                                                    "duration_ms": ms}, window))

        args = {"from_x": from_x, "from_y": from_y, "to_x": to_x, "to_y": to_y, "duration_ms": duration_ms,
                "force": force}
        return self._act("drag", args, do)

    def scroll(self, x: int, y: int, dx: int = 0, dy: int = 0, force: bool = False) -> dict:
        def do():
            self._ready()
            px, py = self._point(x, y)
            sx = check_int("dx", dx, -50, 50)
            sy = check_int("dy", dy, -50, 50)
            if not sx and not sy:
                raise ValidationError("dx and dy are both 0; nothing to scroll")
            window = self._guard(force, (px, py))
            self.backend.scroll(px, py, sx, sy)
            return self._changes(self._with_window({"at": {"x": px, "y": py}, "dx": sx, "dy": sy}, window))

        return self._act("scroll", {"x": x, "y": y, "dx": dx, "dy": dy, "force": force}, do)

    def type_text(self, text: str, submit: bool = False, replace: bool = False, force: bool = False) -> dict:
        def do():
            self._ready()
            if not isinstance(text, str) or not text:
                raise ValidationError("text must be a non-empty string")
            if len(text) > MAX_TEXT:
                raise ValidationError(f"text is longer than {MAX_TEXT} characters; paste via clipboard instead")
            # A typed trailing newline did not submit Firefox's address bar: send a real Return instead.
            body = text[:-1] if text.endswith("\n") else text
            enter = bool(submit) or body != text
            window = self._guard(force)
            if replace:
                self.backend.press_key(SELECT_ALL)
                self.backend.sleep(KEY_SETTLE)
            if body:
                self.backend.type_text(body)
            if enter:
                self.backend.sleep(KEY_SETTLE)  # autocomplete and validation catch up first
                self.backend.press_key(RETURN)
            result = {"typed_chars": len(body)}
            if replace:
                result["replaced"] = True
            if enter:
                result["submitted"] = True
            return self._changes(self._with_window(result, window))

        return self._act("type_text", {"text": text, "submit": submit, "replace": replace, "force": force}, do)

    def press_key(self, key: str, modifiers: list[str] | None = None, confirm: bool = False,
                  force: bool = False) -> dict:
        def do():
            combo = parse_key_combo(key, modifiers or [])
            self.policy.check_key(combo, bool(confirm))
            self._ready()
            window = self._guard(force)
            self.backend.press_key(combo)
            if moves_focus(combo):
                self.backend.sleep(FOCUS_SETTLE)  # so a type_text right after lands in the new place
            return self._changes(self._with_window({"pressed": str(combo)}, window))

        args = {"key": key, "modifiers": modifiers or [], "confirm": confirm, "force": force}
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
            return self._with_window({"activated": window.to_dict()}, None)

        return self._act("activate_window", {"title_or_id": title_or_id}, do)

    def ui_tree(self, window: str | None = None, query: str | None = None, page: bool = False,
                max_nodes: int = 300) -> dict:
        def do():
            limit = check_int("max_nodes", max_nodes, 1, 2000)
            try:
                result = self.a11y.tree(window or None, (query or "").strip() or None, bool(page), limit)
            except WindowNotFound as exc:
                why = self._a11y_diagnosis(window or None)
                raise CcdeskError(f"{exc}. {why}" if why else str(exc)) from None
            text = result["tree"]
            result["approx_tokens"] = len(text) // 4
            if result["truncated"]:
                result["note"] = "truncated: narrow it with query=..., page=true or window=..."
            if self.backend.name == "wayland" and "origin" not in result:
                result["note"] = ("on Wayland, @x,y may be relative to the window (this compositor does "
                                  "not say where it is); verify with a screenshot")
            return result

        args = {"window": window, "query": query, "page": page, "max_nodes": max_nodes}
        return self._act("ui_tree", args, do,
                         loggable=lambda r: {k: v for k, v in r.items() if k != "tree"})

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
                f"{missing_hint(win.wm_class, enabled)}. Until then, work from screenshots")

    def click_element(self, ref: str, button: str = "left", count: int = 1, force: bool = False) -> dict:
        def do():
            self._ready()
            btn = parse_button(button)
            n = check_int("count", count, 1, 10)
            node = self.a11y.resolve(ref)
            label = f'{node["role"]} {node["name"]!r}'
            if not node.get("showing") or not node.get("box"):
                raise CcdeskError(f"{ref} ({label}) is not on screen; scroll it into view, then call ui_tree() again")
            x, y, w, h = node["box"]
            px, py = self._point(x + w // 2, y + h // 2)
            window = self._guard(force, (px, py))
            self.backend.click(px, py, btn, n)
            return self._changes(self._with_window({"clicked": {"x": px, "y": py}, "element": label,
                                                    "button": button, "count": n}, window))

        return self._act("click_element", {"ref": ref, "button": button, "count": count, "force": force}, do)

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
            time.sleep(s)
            return {"waited": s}

        return self._act("wait", {"seconds": seconds}, do)

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

        return self._act("doctor", {}, lambda: build_report(self.backend, self.policy, self.audit.path, self.a11y,
                                                     self.headless),
                         loggable=lambda r: {"backend": r["backend"], "ready": r["ready"], "missing": r["missing"]})
