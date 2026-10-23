"""Accessibility tree (AT-SPI) client: what is on screen as text, for far fewer tokens than pixels.

The AT-SPI bindings live in PyGObject, which is a compiled system package. Rather than depend on
it, ccdesk runs `a11y_helper.py` with a system Python that has it and reads JSON back.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from ccdesk.errors import CcdeskError, UnsupportedError

HELPER = Path(__file__).with_name("a11y_helper.py")
PYTHONS = ("/usr/bin/python3", "python3")
MAX_CHANGES = 25
BUS_STATUS = ["busctl", "--user", "get-property", "org.a11y.Bus", "/org/a11y/bus", "org.a11y.Status", "IsEnabled"]
ENABLE_BUS = "busctl --user set-property org.a11y.Bus /org/a11y/bus org.a11y.Status IsEnabled b true"
TOOLKIT_HINT = (
    "apps missing from the accessibility tree need it switched on before they start: GTK apps and "
    "Firefox read `gsettings set org.gnome.desktop.interface toolkit-accessibility true` "
    "(or GNOME_ACCESSIBILITY=1); Chromium and Electron apps need --force-renderer-accessibility"
)
BUS_OFF_HINT = (
    "the accessibility bus is switched off (org.a11y.Status IsEnabled is false); outside GNOME nothing "
    f"turns it on, and Firefox, Chromium and GTK apps then publish no tree even with the gsettings key "
    f"on. Enable it with `{ENABLE_BUS}`, then restart the app (it checks only at startup). It lasts "
    "until logout; to keep it, run the command from your session startup (.xinitrc, bspwmrc)"
)


class WindowNotFound(CcdeskError):
    """The helper ran, but no window on the accessibility bus matched."""


def bus_enabled(run) -> bool | None:
    """org.a11y.Status.IsEnabled on the session bus, through run(argv) -> output or None.
    None when it cannot be read (no busctl, no bus)."""
    out = (run(BUS_STATUS) or "").split()
    if len(out) == 2 and out[0] == "b":
        return out[1] == "true"
    return None


def missing_hint(wm_class: str, enabled: bool | None) -> str:
    """How to get a window that is not on the accessibility bus onto it."""
    if enabled is False:
        return BUS_OFF_HINT
    if any(k in wm_class.lower() for k in ("chrom", "electron")):
        return "Chromium and Electron apps publish a tree only when started with --force-renderer-accessibility"
    return ("GTK apps and Firefox publish one only if accessibility was on when they started: "
            "`gsettings set org.gnome.desktop.interface toolkit-accessibility true` or GNOME_ACCESSIBILITY=1, "
            "then restart the app")


@dataclass(frozen=True)
class Ref:
    path: str
    role: str
    name: str
    dx: int = 0  # added to the element's box: the window's screen position on Wayland
    dy: int = 0


def _run_helper(argv: list[str], timeout: float, env: dict | None = None) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, env=env)
    if proc.returncode != 0:
        raise CcdeskError(f"accessibility helper failed: {(proc.stderr or proc.stdout).strip()[-300:]}")
    return proc.stdout


class A11y:
    def __init__(self, runner=None, pythons: tuple[str, ...] | None = None):
        # The helper talks to the accessibility bus of this env (a headless screen has its own).
        self.env: dict | None = None
        self._runner = runner or (lambda argv, timeout: _run_helper(argv, timeout, self.env))
        override = os.environ.get("CCDESK_A11Y_PYTHON")
        self._pythons = pythons or ((override,) if override else PYTHONS)
        self._python: str | None = None
        self.refs: dict[str, Ref] = {}
        self._by_path: dict[str, str] = {}
        self._last: dict | None = None  # the latest tree() request and its lines, for changes()
        self._lock = threading.Lock()
        # (pid, title) -> screen position of that window's content, or None. Set by Desktop to the
        # backend's window_origin, for toolkits that report boxes relative to their window.
        self.locate = None

    def call(self, request: dict, timeout: float = 20) -> dict:
        errors = []
        for py in ([self._python] if self._python else self._pythons):
            exe = py if os.path.isabs(py) else shutil.which(py)
            if not exe or not os.path.exists(exe):
                continue
            try:
                out = self._runner([exe, "-I", str(HELPER), json.dumps(request)], timeout)
            except subprocess.TimeoutExpired:
                raise CcdeskError("accessibility helper timed out; the app may be busy") from None
            try:
                result = json.loads(out)
            except ValueError:
                raise CcdeskError(f"accessibility helper printed non-JSON: {out[:200]!r}") from None
            if "PyGObject" in str(result.get("error", "")):
                errors.append(f"{exe}: {result['error']}")
                continue
            self._python = py
            if "error" in result:
                raise (WindowNotFound if result.get("no_window") else CcdeskError)(result["error"])
            return result
        hint = "; ".join(errors) or "no python3 found"
        raise UnsupportedError(
            f"accessibility tree needs a system Python with PyGObject (Arch: python-gobject, Debian: "
            f"python3-gi, plus at-spi2-core); set CCDESK_A11Y_PYTHON to pick one ({hint})"
        )

    def apps(self) -> list[dict]:
        return self.call({"op": "apps"})["apps"]

    def _ref(self, n: dict, offset: tuple[int, int]) -> str:
        """Same element (path and role), same ref across calls, so refs from an earlier tree stay valid."""
        ref = self._by_path.get(n["path"])
        if ref is None or self.refs[ref].role != n["role"]:
            ref = f"e{len(self.refs) + 1}"
            self._by_path[n["path"]] = ref
        self.refs[ref] = Ref(n["path"], n["role"], n["name"], *offset)
        return ref

    def _offset(self, result: dict) -> tuple[int, int] | None:
        """A frame at 0,0 means the toolkit reports window-relative boxes (Wayland): ask the
        compositor where the window is. On X11 the frame has its real screen position."""
        box = result.get("frame_box")
        if not self.locate or not box or box[0] or box[1]:
            return None
        try:
            return self.locate(result.get("pid"), result.get("window"))
        except CcdeskError:
            return None

    def _read(self, request: dict) -> tuple[dict, list[tuple[str, dict]]]:
        result = self.call(request)
        offset = self._offset(result)
        if offset:
            result["origin"] = {"x": offset[0], "y": offset[1]}
            for n in result["nodes"]:
                shift(n, offset)
        return result, [(self._ref(n, offset or (0, 0)), n) for n in result["nodes"]]

    def tree(self, window: str | None, query: str | None, page: bool, max_nodes: int) -> dict:
        request = {"op": "tree", "window": window, "query": query, "page": page, "max_nodes": max_nodes}
        with self._lock:
            result, nodes = self._read(request)
            self._last = {"request": request, "lines": {ref: format_node(ref, n, 0) for ref, n in nodes}}
        lines = []
        base = min((n["depth"] for _, n in nodes), default=0)
        for ref, n in nodes:
            lines.append(format_node(ref, n, min(n["depth"] - base, 8) if not query else 0))
        out = {"window": result["window"], "app": result["app"], "tree": "\n".join(lines),
               "count": len(nodes), "truncated": result["truncated"]}
        if "origin" in result:
            out["origin"] = result["origin"]
        if result.get("matches", 0) > 1:
            out["warning"] = (f"{result['matches']} windows match {window!r}; this is the active one if "
                              f"any is active, else the first. Check with a screenshot crop, or "
                              f"pass a more specific title")
        return out

    def changes(self) -> str | None:
        """Re-read the window of the latest tree() and return the lines that changed, or None if
        tree() was never called. Lets an action report its own effect without another ui_tree()."""
        with self._lock:
            if self._last is None:
                return None
            _, nodes = self._read(self._last["request"])
            before = self._last["lines"]
            after = {ref: format_node(ref, n, 0) for ref, n in nodes}
            self._last["lines"] = after
        changed = [line for ref, line in after.items() if before.get(ref) != line]
        gone = [ref for ref in before if ref not in after]
        if not changed and not gone:
            return "no change in the window yet (slow apps: wait(1), then ui_tree())"
        if len(changed) + len(gone) > MAX_CHANGES:
            return f"{len(changed)} elements changed, {len(gone)} gone: the view moved on; call ui_tree()"
        if gone:
            changed.append("gone: " + " ".join(gone))
        return "\n".join(changed)

    def resolve(self, ref: str) -> dict:
        known = self.refs.get(str(ref).strip())
        if known is None:
            raise CcdeskError(f"unknown element {ref!r}; call ui_tree() for the current refs")
        node = self.call({"op": "resolve", "path": known.path})["node"]
        if node is not None:
            shift(node, (known.dx, known.dy))
        if node is None or node["role"] != known.role or node["name"] != known.name:
            raise CcdeskError(f"{ref} changed since ui_tree() (the page moved on); call ui_tree() again")
        return node


def shift(node: dict, offset: tuple[int, int]) -> None:
    if node.get("box") and any(offset):
        x, y, w, h = node["box"]
        node["box"] = [x + offset[0], y + offset[1], w, h]


def format_node(ref: str, n: dict, indent: int) -> str:
    """`e7 check box "Peru" [checked] @32,232 14x14`: one short line per element."""
    parts = [f"{'  ' * indent}{ref} {n['role']}"]
    if n["name"]:
        parts.append(json.dumps(n["name"], ensure_ascii=False))
    if n.get("value"):
        parts.append("=" + json.dumps(n["value"], ensure_ascii=False))
    if n.get("states"):
        parts.append("[" + ",".join(n["states"]) + "]")
    if n.get("box"):
        x, y, w, h = n["box"]
        parts.append(f"@{x},{y} {w}x{h}")
    return " ".join(parts)
