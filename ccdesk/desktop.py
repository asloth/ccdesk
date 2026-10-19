"""The action layer: validation, safety policy and audit around a backend.

Everything the MCP server exposes is a method here, so it can be tested without MCP.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from ccdesk.a11y import A11y
from ccdesk.backends import Backend, get_backend
from ccdesk.errors import CcdeskError, PolicyError, ValidationError
from ccdesk.parsing import (
    Window,
    check_int,
    check_point,
    check_scale,
    parse_button,
    parse_key_combo,
    parse_region,
    parse_window_ref,
    png_size,
)
from ccdesk.safety import AuditLog, Policy, cache_dir

MAX_TEXT = 10_000
MAX_WAIT = 60.0
KEEP_SCREENSHOTS = 200


class Desktop:
    def __init__(self, backend: Backend | None = None, policy: Policy | None = None,
                 audit: AuditLog | None = None, shots_dir: str | os.PathLike | None = None,
                 a11y: A11y | None = None):
        self.backend = backend or get_backend()
        self.a11y = a11y or A11y()
        self.policy = policy or Policy()
        self.audit = audit or AuditLog()
        self.shots_dir = Path(shots_dir) if shots_dir else cache_dir() / "screenshots"
        self._screen: tuple[int, int] | None = None

    # -- plumbing -------------------------------------------------------------

    def _act(self, tool: str, args: dict, fn, policy_tool: str | None = None, loggable=None):
        start = time.monotonic()
        try:
            self.policy.check(policy_tool or tool)
            result = fn()
        except PolicyError as exc:
            self.audit.record(tool, args, "denied", error=str(exc))
            raise
        except CcdeskError as exc:
            self.audit.record(tool, args, "error", error=str(exc), duration_ms=(time.monotonic() - start) * 1000)
            raise
        except Exception as exc:
            self.audit.record(tool, args, "error", error=f"{type(exc).__name__}: {exc}",
                              duration_ms=(time.monotonic() - start) * 1000)
            raise
        self.audit.record(tool, args, "ok", result=loggable(result) if loggable else result,
                          duration_ms=(time.monotonic() - start) * 1000)
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

    def click(self, x: int, y: int, button: str = "left", count: int = 1) -> dict:
        def do():
            self._ready()
            px, py = self._point(x, y)
            btn = parse_button(button)
            n = check_int("count", count, 1, 10)
            self.backend.click(px, py, btn, n)
            return {"clicked": {"x": px, "y": py}, "button": button, "count": n}

        return self._act("click", {"x": x, "y": y, "button": button, "count": count}, do)

    def move(self, x: int, y: int) -> dict:
        def do():
            self._ready()
            px, py = self._point(x, y)
            self.backend.move(px, py)
            return {"moved": {"x": px, "y": py}}

        return self._act("move", {"x": x, "y": y}, do)

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int, duration_ms: int = 400) -> dict:
        def do():
            self._ready()
            x0, y0 = self._point(from_x, from_y)
            x1, y1 = self._point(to_x, to_y)
            ms = check_int("duration_ms", duration_ms, 0, 10_000)
            self.backend.drag(x0, y0, x1, y1, ms)
            return {"from": {"x": x0, "y": y0}, "to": {"x": x1, "y": y1}, "duration_ms": ms}

        args = {"from_x": from_x, "from_y": from_y, "to_x": to_x, "to_y": to_y, "duration_ms": duration_ms}
        return self._act("drag", args, do)

    def scroll(self, x: int, y: int, dx: int = 0, dy: int = 0) -> dict:
        def do():
            self._ready()
            px, py = self._point(x, y)
            sx = check_int("dx", dx, -50, 50)
            sy = check_int("dy", dy, -50, 50)
            if not sx and not sy:
                raise ValidationError("dx and dy are both 0; nothing to scroll")
            self.backend.scroll(px, py, sx, sy)
            return {"at": {"x": px, "y": py}, "dx": sx, "dy": sy}

        return self._act("scroll", {"x": x, "y": y, "dx": dx, "dy": dy}, do)

    def type_text(self, text: str) -> dict:
        def do():
            self._ready()
            if not isinstance(text, str) or not text:
                raise ValidationError("text must be a non-empty string")
            if len(text) > MAX_TEXT:
                raise ValidationError(f"text is longer than {MAX_TEXT} characters; paste via clipboard instead")
            self.backend.type_text(text)
            return {"typed_chars": len(text)}

        return self._act("type_text", {"text": text}, do)

    def press_key(self, key: str, modifiers: list[str] | None = None, confirm: bool = False) -> dict:
        def do():
            combo = parse_key_combo(key, modifiers or [])
            self.policy.check_key(combo, bool(confirm))
            self._ready()
            self.backend.press_key(combo)
            return {"pressed": str(combo)}

        return self._act("press_key", {"key": key, "modifiers": modifiers or [], "confirm": confirm}, do)

    def list_windows(self) -> dict:
        def do():
            self._ready()
            return {"windows": [w.to_dict() for w in self.backend.list_windows()]}

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
            self.backend.activate_window(window)
            return {"activated": window.to_dict()}

        return self._act("activate_window", {"title_or_id": title_or_id}, do)

    def ui_tree(self, window: str | None = None, query: str | None = None, page: bool = False,
                max_nodes: int = 300) -> dict:
        def do():
            limit = check_int("max_nodes", max_nodes, 1, 2000)
            result = self.a11y.tree(window or None, (query or "").strip() or None, bool(page), limit)
            text = result["tree"]
            result["approx_tokens"] = len(text) // 4
            if result["truncated"]:
                result["note"] = "truncated: narrow it with query=..., page=true or window=..."
            if self.backend.name == "wayland":
                result["note"] = "on Wayland, @x,y may be relative to the window; verify with a screenshot"
            return result

        args = {"window": window, "query": query, "page": page, "max_nodes": max_nodes}
        return self._act("ui_tree", args, do,
                         loggable=lambda r: {k: v for k, v in r.items() if k != "tree"})

    def click_element(self, ref: str, button: str = "left", count: int = 1) -> dict:
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
            self.backend.click(px, py, btn, n)
            return {"clicked": {"x": px, "y": py}, "element": label, "button": button, "count": n}

        return self._act("click_element", {"ref": ref, "button": button, "count": count}, do)

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

    def launch(self, command: str) -> dict:
        def do():
            if not isinstance(command, str) or not command.strip():
                raise ValidationError("command must be a non-empty string")
            try:
                argv = shlex.split(command)
            except ValueError as exc:
                raise ValidationError(f"cannot parse command: {exc}") from None
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

        return self._act("doctor", {}, lambda: build_report(self.backend, self.policy, self.audit.path, self.a11y),
                         loggable=lambda r: {"backend": r["backend"], "ready": r["ready"], "missing": r["missing"]})
