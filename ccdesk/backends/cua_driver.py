"""Window-addressed background input through an owned cua-driver MCP child.

Session inspection and window management stay on the wrapped backend. No action
uses desktop input scope, foreground delivery, or the session's input fallback.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading

from ccdesk.errors import (CommandError, CuaDriverError, TargetHiddenError,
                           TargetRequiredError, UnsupportedError)
from ccdesk.parsing import KeyCombo, Window

HOVER_NOTE = ("hover is not delivered to the app in background mode; a task that needs hover "
              "(tooltip, hover-only toolbar) will later go to a private screen")
SUPPORTED_VERSION = "0.38.0"


def version_of(binary: str, env: dict, timeout: float = 20) -> str:
    return subprocess.run([binary, "--version"], env=env, capture_output=True,
                          text=True, timeout=timeout, check=True).stdout.strip()


class Driver:
    """Synchronous JSON-RPC client. One direct runtime, one serialized request at a time.

    A lost response is never retried: input may already have reached the application.
    Stderr goes to a temporary file so a noisy child cannot block the protocol.
    """

    def __init__(self, binary: str, env: dict, *, timeout: float = 20, overlay: bool = True):
        self.timeout = timeout
        self.lock = threading.RLock()
        self.messages = queue.Queue()
        self.serial = 0
        self.proc = None
        self.stderr = tempfile.TemporaryFile()
        self.closed = False
        try:
            version = version_of(binary, env, timeout)
            if version.split()[-1:] != [SUPPORTED_VERSION]:
                raise CommandError(f"cua-driver {SUPPORTED_VERSION} is required; found {version!r}. "
                                   "Install the supported release and use --cua-driver PATH.")
            self.version = version
            argv = [binary, "mcp", "--direct"]
            if not overlay:
                argv.append("--no-overlay")
            self.proc = subprocess.Popen(argv, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=self.stderr, text=True, encoding="utf-8", bufsize=1)
            self.reader = threading.Thread(target=self._read, daemon=True)
            self.reader.start()
            self._request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                        "clientInfo": {"name": "ccdesk", "version": "1"}})
            self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            self.proc.stdin.flush()
        except (OSError, subprocess.SubprocessError) as exc:
            self.close()
            raise CommandError("Cannot start cua-driver. Check --cua-driver PATH and run ccdesk doctor.") from exc
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.proc.stdout:
                self.messages.put(json.loads(line))
        except (ValueError, OSError) as exc:
            self.messages.put(exc)
        finally:
            self.messages.put(EOFError("cua-driver closed stdout"))

    def _request(self, method: str, params: dict, *, input_call: bool = False):
        with self.lock:
            if self.closed:
                raise CuaDriverError("cua_driver_disconnected", input_emitted=False)
            self.serial += 1
            try:
                self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.serial,
                                                  "method": method, "params": params}) + "\n")
                self.proc.stdin.flush()
                while True:
                    message = self.messages.get(timeout=self.timeout)
                    if isinstance(message, Exception):
                        raise message
                    if not isinstance(message, dict):
                        raise ValueError("invalid JSON-RPC response")
                    if "id" not in message:  # MCP logging/progress notification
                        continue
                    if message["id"] != self.serial:
                        raise ValueError("unexpected response id")
                    if "error" in message:
                        raise ValueError("cua-driver rejected the protocol request")
                    return message["result"]
            except (OSError, ValueError, KeyError, queue.Empty, EOFError) as exc:
                self.close()
                raise CuaDriverError("cua_driver_disconnected", input_emitted=input_call) from exc

    def call(self, name: str, args: dict, *, input_call: bool = False) -> dict:
        result = self._request("tools/call", {"name": name, "arguments": args}, input_call=input_call)
        structured = result.get("structuredContent") or {}
        if result.get("isError") or structured.get("ok") is False:
            raise CuaDriverError.from_result(structured, input_call=input_call)
        return structured

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.proc is not None:
                if self.proc.poll() is None:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                        self.proc.wait(timeout=2)
                for pipe in (self.proc.stdin, self.proc.stdout):
                    if pipe:
                        pipe.close()
            self.stderr.close()


def driver_env(session) -> dict:
    env = dict(session.env)
    # The chosen ccdesk backend determines which input protocol the child uses.
    env["CUA_DRIVER_RS_ENABLE_WAYLAND"] = "0" if session.name == "x11" else "1"
    env["CUA_DRIVER_ALLOW_BRIEF_FOREGROUND"] = "0"
    return env


def binary_path(session, binary: str | None = None) -> str:
    path = (shutil.which(binary, path=session.env.get("PATH")) if binary else session.which("cua-driver"))
    if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise CommandError("cua-driver is missing or not executable. Install cua-driver 0.38.0 "
                           "and pass --cua-driver PATH.")
    return path


def background_status(session, driver: Driver) -> dict:
    # This probe emits no input and never requests portal consent. Availability
    # is conditional on the target/toolkit; every actual call may still refuse.
    permissions = driver.call("check_permissions", {"prompt": False})
    if session.name == "x11":
        available = permissions.get("xsend_event") is True
        reason = "X11 background routes are available; delivery depends on the target toolkit."
    else:
        available = permissions.get("wayland_enabled") is True and permissions.get("atspi") is True
        reason = ("Wayland background AT-SPI is available; addressed compositor routes may also work. "
                  "Virtual-pointer and portal/libei input alone cannot target an unfocused window.")
    if permissions.get("background_available") is False:
        available = False
    return {"available": available, "reason": reason if available else
            "Background input is unavailable. Check the session and accessibility bus with ccdesk doctor; "
            "use a private screen for unsupported tasks."}


def report(session, binary: str | None = None) -> dict:
    info = {"binary": None, "version": None, "available": False, "reason": None}
    driver = None
    try:
        info["binary"] = binary_path(session, binary)
        info["version"] = version_of(info["binary"], driver_env(session))
        driver = Driver(info["binary"], driver_env(session), overlay=False)
        info["version"] = driver.version
        info.update(background_status(session, driver))
    except (CommandError, CuaDriverError, OSError, subprocess.SubprocessError) as exc:
        info["reason"] = str(exc)
    finally:
        if driver:
            driver.close()
    return info


class CuaDriverBackend:
    # Desktop's own-pointer contract is independent input/focus, not MPX
    # specifically. It supplies private seat locking and the right AT-SPI window.
    own_pointer = True
    background_input = True

    def __init__(self, session, binary: str | None = None, *, overlay: bool = True):
        self.session = session
        self._target: str | None = None
        self._note = None
        self.driver = Driver(binary_path(session, binary), driver_env(session), overlay=overlay)
        try:
            self.status = background_status(session, self.driver)
            if not self.status["available"]:
                raise CuaDriverError("background_unavailable", input_emitted=False)
        except BaseException:
            self.close()
            raise

    def __getattr__(self, name):
        return getattr(self.session, name)

    def close(self):
        self.driver.close()

    def take_note(self):
        note, self._note = self._note, None
        return note

    def activate_window(self, window: Window):
        current = next((w for w in self.session.list_windows() if w.id == window.id), None)
        if current is None:
            raise TargetRequiredError("Window disappeared. Read list_windows() and choose a window again.")
        desktop = self.session.current_desktop()
        if (self.session.window_on_show(window.id) is False or
                (current.desktop is not None and current.desktop >= 0 and desktop is not None
                 and current.desktop != desktop)):
            raise TargetHiddenError("Window is hidden or on another desktop. Choose an on-screen window "
                                    "or use a private screen; no activation or input was sent.")
        self._target = window.id

    def active_window(self):
        return self._target

    def window_at(self, x: int, y: int):
        if self.session.name == "x11":
            # X11Backend.window_at's CLI fallback WARPS THE USER POINTER.
            # Use only the read-only X tree traversal, and refuse if it fails.
            from ccdesk.backends.xquery import XQueryUnavailable

            xq = self.session._xq()
            if xq is None:
                raise UnsupportedError("Safe window targeting requires libX11. Install it and retry.")
            try:
                return f"0x{xq.window_at(x, y):08x}"
            except XQueryUnavailable as exc:
                raise UnsupportedError("Cannot read window stacking. Refresh the session or use a private screen.") from exc
        return self.session.window_at(x, y)

    def _address(self, window: Window) -> dict:
        # ccdesk owns the window list and hit testing. Driver enumeration supplies
        # the PID and its native numeric handle (Wayland handles differ by API).
        records = self.driver.call("list_windows", {})["windows"]
        if self.session.name == "x11":
            found = [r for r in records if r.get("window_id") == int(window.id, 16)]
        else:
            # No array-order guess or PID-only match: one app can have many windows.
            found = [r for r in records if r.get("title") == window.title and
                     r.get("bounds") == {"x": window.x, "y": window.y,
                                         "width": window.width, "height": window.height}]
        if len(found) != 1 or not found[0].get("pid") or not found[0].get("window_id"):
            raise CuaDriverError("background_unavailable", input_emitted=False)
        return {"pid": found[0]["pid"], "window_id": found[0]["window_id"], "delivery_mode": "background"}

    def _window(self, wid: str | None) -> Window:
        window = next((w for w in self.session.list_windows() if w.id == wid), None)
        if window is None:
            raise TargetRequiredError("Choose an existing window with activate_window(); no input was sent.")
        return window

    def _point(self, x: int, y: int):
        window = self._window(self.window_at(x, y))
        return window, self._address(window) | {"x": x - window.x, "y": y - window.y}

    def move(self, x: int, y: int):
        # This tool is the deliberate exception: overlay-only, screen coordinates.
        self.driver.call("move_cursor", {"x": x, "y": y, "scope": "window"}, input_call=True)
        self._note = HOVER_NOTE

    def click(self, x: int, y: int, button: int, count: int):
        _, args = self._point(x, y)
        self.driver.call("click", args | {"button": {1: "left", 2: "middle", 3: "right"}[button],
                                          "count": count}, input_call=True)

    def drag(self, x0: int, y0: int, x1: int, y1: int, duration_ms: int):
        window, args = self._point(x0, y0)
        args.pop("x")
        args.pop("y")
        self.driver.call("drag", args | {"from_x": x0 - window.x, "from_y": y0 - window.y,
                                         "to_x": x1 - window.x, "to_y": y1 - window.y,
                                         "duration_ms": duration_ms}, input_call=True)

    def scroll(self, x: int, y: int, dx: int, dy: int):
        _, args = self._point(x, y)
        emitted = False
        for delta, positive, negative in ((dy, "down", "up"), (dx, "right", "left")):
            remaining = abs(delta)
            while remaining:
                amount = min(remaining, 50)
                try:
                    self.driver.call("scroll", args | {"direction": positive if delta > 0 else negative,
                                                       "amount": amount, "by": "line"}, input_call=True)
                except CuaDriverError as exc:
                    exc.input_emitted |= emitted
                    raise
                emitted = True
                remaining -= amount

    def type_text(self, text: str):
        self.driver.call("type_text", self._address(self._window(self._target)) | {"text": text}, input_call=True)

    def press_key(self, combo: KeyCombo):
        # ccdesk normalizes to X keysyms; the driver accepts friendly key names.
        key = combo.key.lower() if len(combo.key) > 1 else combo.key
        key = {"Return": "enter", "Page_Up": "pageup", "Page_Down": "pagedown",
               "BackSpace": "backspace", "plus": "+", "minus": "-", "comma": ",",
               "period": ".", "slash": "/"}.get(combo.key, key)
        self.driver.call("press_key", self._address(self._window(self._target)) |
                         {"key": key, "modifiers": list(combo.modifiers)}, input_call=True)
