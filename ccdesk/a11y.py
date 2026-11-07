"""Accessibility tree (AT-SPI) client: what is on screen as text, for far fewer tokens than pixels.

The AT-SPI bindings live in PyGObject, which is a compiled system package. Rather than depend on
it, ccdesk runs `a11y_helper.py` with a system Python that has it and reads JSON back.
"""

from __future__ import annotations

import atexit
import json
import os
import select
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ccdesk.errors import CcdeskError, UnsupportedError

HELPER = Path(__file__).with_name("a11y_helper.py")
PYTHONS = ("/usr/bin/python3", "python3")
MAX_CHANGES = 25
ACTION_TIMEOUT = 5.0  # a dialog an action opens can hold the helper's reply back
# Repeating these after the helper died is harmless; a click or a write may have run already.
RETRY_OPS = frozenset({"apps", "tree", "resolve", "changes"})
# Roles whose native action does what a left click does. Not fields: a click there is for focus.
NATIVE_ROLES = frozenset({"push button", "toggle button", "button", "check box", "radio button", "link",
                          "menu item", "check menu item", "radio menu item", "page tab", "switch"})
# Toolkits' names for it: GTK click, Qt Press and Toggle, Firefox press, jump, check and uncheck.
NATIVE_ACTIONS = ("click", "press", "toggle", "jump", "check", "uncheck")
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


class HelperProcess:
    """A long-lived a11y_helper.py: one JSON request per line, one answer per line.

    A fresh system Python plus `import gi` costs ~100 ms, which doubled the latency of a small
    tree; this starts one helper per session instead. stdin closing (or `shutdown`) ends it, so
    it cannot outlive ccdesk.
    """

    def __init__(self, argv: list[str], env: dict | None = None):
        self.argv = argv
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, bufsize=0, env=env)
        self._buf = b""

    def request(self, payload: str, timeout: float) -> str:
        data = memoryview(payload.encode() + b"\n")
        try:
            while data:
                data = data[self.proc.stdin.write(data):]
        except (BrokenPipeError, ValueError) as exc:
            raise EOFError(str(exc)) from None
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise subprocess.TimeoutExpired(self.argv, timeout)
            ready, _, _ = select.select([self.proc.stdout], [], [], left)
            if not ready:
                raise subprocess.TimeoutExpired(self.argv, timeout)
            chunk = os.read(self.proc.stdout.fileno(), 1 << 16)
            if not chunk:
                raise EOFError("the accessibility helper exited")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line.decode("utf-8", "replace")

    def close(self, kill: bool = False) -> None:
        if self.proc.poll() is not None:
            return
        try:
            if kill:
                self.proc.kill()
            else:
                self.proc.stdin.close()
            self.proc.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass


def helper_pythons() -> tuple[str, ...]:
    """The interpreters that may have PyGObject: CCDESK_A11Y_PYTHON if set, else the usual ones."""
    override = os.environ.get("CCDESK_A11Y_PYTHON")
    return (override,) if override else PYTHONS


class A11y:
    def __init__(self, runner=None, pythons: tuple[str, ...] | None = None):
        # The helper talks to the accessibility bus of this env (a headless screen has its own).
        self.env: dict | None = None
        # Tests (and anything that wants a one-shot process) pass a callable; otherwise one helper
        # stays alive for the session, which is what makes a read cheap.
        self._runner = runner
        self._proc: HelperProcess | None = None
        self._proc_python: str | None = None
        self._io = threading.Lock()
        self._pythons = pythons or helper_pythons()
        self._python: str | None = None
        self.refs: dict[str, Ref] = {}
        self._by_path: dict[str, str] = {}
        self._last: dict | None = None  # the latest tree() request and its lines, for changes()
        self._lock = threading.Lock()
        # (pid, title) -> screen position of that window's content, or None. Set by Desktop to the
        # backend's window_origin, for toolkits that report boxes relative to their window.
        self.locate = None
        atexit.register(self.close)

    def call(self, request: dict, timeout: float = 20, private: bool = False,
             on_timeout: str = "accessibility helper timed out; the app may be busy") -> dict:
        """private: the request goes through stdin, because it holds what the user's field will.

        One long-lived helper answers every call. If it died, a read is retried once on a fresh
        one; a click or a write is never replayed, because it may have run before the crash."""
        errors = []
        payload = json.dumps(request)
        retryable = request.get("op") in RETRY_OPS
        candidates = [self._python] if self._python else list(self._pythons)
        for index, py in enumerate(candidates):
            exe = py if os.path.isabs(py) else shutil.which(py)
            if not exe or not os.path.exists(exe):
                continue
            try:
                out = self._ask(exe, payload, timeout, private, request.get("op"))
            except subprocess.TimeoutExpired:
                raise CcdeskError(on_timeout) from None
            except OSError as exc:  # could not start it: a broken or vanished interpreter
                self._died()
                errors.append(f"{exe}: {exc}")
                continue
            except EOFError:
                # An interpreter without PyGObject prints its error and exits, sometimes before it
                # even reads the request: a read is safe on the next candidate, an action is not.
                if retryable and index < len(candidates) - 1:
                    errors.append(f"{exe}: stopped before answering")
                    continue
                what = ("" if retryable else
                        ": the action may have run; check with ui_tree() or a screenshot, don't repeat it")
                raise CcdeskError(f"the accessibility helper stopped before answering{what}") from None
            try:
                result = json.loads(out)
            except ValueError:
                self._died()
                raise CcdeskError(f"accessibility helper printed non-JSON: {out[:200]!r}") from None
            if "PyGObject" in str(result.get("error", "")):
                self._died()  # that helper exits right after saying so
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

    def _ask(self, exe: str, payload: str, timeout: float, private: bool, op: str | None) -> str:
        """Send one request to the helper, starting it first if there is none."""
        if self._runner is not None:  # injected (tests): one process per request, as before
            if private:
                return self._runner([exe, "-I", str(HELPER), "-"], timeout, payload)
            return self._runner([exe, "-I", str(HELPER), payload], timeout)
        with self._io:
            for attempt in (0, 1):
                if self._proc is None or self._proc_python != exe:
                    # An OSError here means it could not start: nothing was sent, so call() may
                    # try the next interpreter. One raised by the request below is different.
                    self._start(exe)
                try:
                    return self._proc.request(payload, timeout)
                except subprocess.TimeoutExpired:
                    # A late answer would arrive in the middle of the next request: drop the process.
                    self._died(kill=True)
                    raise
                except OSError as exc:  # died while handling it: never replay the payload
                    self._died()
                    raise EOFError(str(exc)) from None
                except EOFError:
                    self._died()
                    if attempt or op not in RETRY_OPS:
                        raise
        raise AssertionError("unreachable")

    def _start(self, exe: str) -> None:
        self._died()
        self._proc = HelperProcess([exe, "-I", str(HELPER)], env=self.env)
        self._proc_python = exe

    def _died(self, kill: bool = False) -> None:
        proc, self._proc, self._proc_python = self._proc, None, None
        if proc is not None:
            proc.close(kill=kill)

    def close(self) -> None:
        """End the helper: it exits when its stdin closes, and cannot outlive ccdesk."""
        with self._io:
            self._died()

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
            self._last = {"request": request, "lines": {ref: format_node(ref, n, 0) for ref, n in nodes},
                          "window": result["window"], "secret": secret_focused(nodes)}
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

    def changes(self, settle: float = 0) -> str | None:
        """Re-read the window of the latest tree() and return the lines that changed, or None if
        tree() was never called. Lets an action report its own effect without another ui_tree().

        settle: seconds the helper watches the window for a change before answering, so a fast
        app reports its effect at once instead of after a fixed pause."""
        with self._lock:
            if self._last is None:
                return None
            request = dict(self._last["request"], settle_ms=max(0, round(settle * 1000)))
            _, nodes = self._read(request)
            before = self._last["lines"]
            after = {ref: format_node(ref, n, 0) for ref, n in nodes}
            self._last["lines"] = after
            self._last["secret"] = secret_focused(nodes)
        changed = [line for ref, line in after.items() if before.get(ref) != line]
        gone = [ref for ref in before if ref not in after]
        if not changed and not gone:
            return "no change in the window yet (slow apps: wait(1), then ui_tree())"
        if len(changed) + len(gone) > MAX_CHANGES:
            return f"{len(changed)} elements changed, {len(gone)} gone: the view moved on; call ui_tree()"
        if gone:
            changed.append("gone: " + " ".join(gone))
        return "\n".join(changed)

    def on_secret_field(self) -> bool:
        """Whether the latest read of the tree had a password field focused. Costs no extra read,
        so it knows nothing without a tree, or if focus moved by other means since."""
        return bool(self._last and self._last["secret"])

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

    def activate(self, ref: str) -> str | None:
        """Run the element's native click-like action. Returns its name, or None if none ran."""
        unsure = ("the app did not answer within the time limit: the action may have run (a dialog it "
                  "opened can hold the answer back). Check with ui_tree() or a screenshot; don't repeat it blindly")
        request = {"op": "action", "path": self.refs[ref].path, "names": list(NATIVE_ACTIONS)}
        return self.call(request, timeout=ACTION_TIMEOUT, on_timeout=unsure)["action"]

    def set_value(self, ref: str, text: str) -> dict:
        unsure = ("the app did not answer within the time limit: the value may have been set. "
                  "Check with ui_tree() before trying again")
        request = {"op": "set", "path": self.refs[ref].path, "text": text}
        return self.call(request, timeout=ACTION_TIMEOUT, private=True, on_timeout=unsure)

    def tree_window(self) -> str | None:
        return self._last["window"] if self._last else None


def secret_focused(nodes: list[tuple[str, dict]]) -> bool:
    return any(n["role"] == "password text" and "focused" in n.get("states", ()) for _, n in nodes)


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
