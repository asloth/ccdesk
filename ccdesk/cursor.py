"""An agent's drawn cursor: a visual only. The real pointer is still what clicks.

`cursor_helper.py` (run by a system Python with PyGObject) draws an arrow and a name pill in a
click-through window. This side starts it on first use and sends it positions. Nothing here
raises into an action: if the helper cannot run, the cursor turns itself off and doctor() says so.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from ccdesk.a11y import helper_pythons

HELPER = Path(__file__).with_name("cursor_helper.py")
TITLE_PREFIX = "ccdesk-cursor:"
OFFSET = 2  # the overlay sits just below-right of the point, never on the pixel the click lands on
PALETTE = ("#e5484d", "#3e63dd", "#30a46c", "#f5a524", "#8e4ec6",
           "#0ea5b7", "#d6409f", "#ff6b35", "#6e56cf", "#5b8c00")


def pick_color(name: str, given: str | None = None) -> str:
    if given:
        return given
    return PALETTE[int(hashlib.sha256(name.encode()).hexdigest(), 16) % len(PALETTE)]


def valid_color(text: str) -> bool:
    return bool(re.fullmatch(r"#[0-9a-fA-F]{6}", text or ""))


class AgentCursor:
    def __init__(self, name: str, color: str | None = None, env: dict | None = None,
                 python: str | None = None, helper: Path | str | None = None):
        self.name = name
        self.color = pick_color(name, color)
        self.env = env
        self.python = python
        self.helper = str(helper or HELPER)  # tests point this at a fake
        self.failure: str | None = None  # why it turned itself off
        self._proc: subprocess.Popen | None = None
        self._err = None
        self._lock = threading.Lock()
        self.position: tuple[int, int] | None = None

    @property
    def enabled(self) -> bool:
        return self.failure is None

    def _exe(self) -> str | None:
        for py in ([self.python] if self.python else helper_pythons()):
            exe = py if os.path.isabs(py) else shutil.which(py)
            if exe and os.path.exists(exe):
                return exe
        return None

    def _start(self) -> None:
        exe = self._exe()
        if exe is None:
            raise OSError("no system python3 found")
        self._err = tempfile.TemporaryFile()
        self._proc = subprocess.Popen([exe, "-I", self.helper, self.name, self.color], stdin=subprocess.PIPE,
                                      stdout=subprocess.DEVNULL, stderr=self._err, env=self.env)
        atexit.register(self.close)

    def _send(self, line: str) -> None:
        with self._lock:
            if not self.enabled:
                return
            try:
                if self._proc is None:
                    self._start()
                if self._proc.poll() is not None:
                    raise BrokenPipeError("the helper exited")
                self._proc.stdin.write((line + "\n").encode())
                self._proc.stdin.flush()
            except (OSError, ValueError) as exc:
                self._fail(exc)

    def _fail(self, exc: Exception) -> None:
        detail = ""
        if self._proc is not None and self._err is not None:
            try:
                self._proc.wait(1)
                self._err.seek(0)
                last = self._err.read().decode(errors="replace").strip().splitlines()[-1:]
                detail = f": {last[0]}" if last else ""
            except Exception:  # best effort: the reason is a hint
                pass
        self.failure = f"{type(exc).__name__}: {exc}{detail}"
        self._kill()

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                proc.kill()

    def move(self, x: int, y: int) -> None:
        self.position = (x, y)
        self._send(f"move {int(x) + OFFSET} {int(y) + OFFSET}")

    def hide(self) -> None:
        self._send("hide")

    def close(self) -> None:
        with self._lock:
            self._kill()

    def status(self) -> str | None:
        """A line for doctor() when the cursor is off because of a failure."""
        if self.failure:
            return (f"the drawn cursor for agent {self.name!r} is off: {self.failure}. It needs a system Python "
                    f"with PyGObject and GTK 3 (set CCDESK_A11Y_PYTHON); actions still work")
        return None
