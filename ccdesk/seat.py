"""One input lock per screen, shared by every ccdesk process that drives it.

A screen has one pointer and one keyboard focus. When several agents (several `ccdesk mcp`
servers) drive the same display, each input action takes this lock for its whole run: the focus
check in Desktop._guard, any re-focus, and the clicks or keys themselves. Without it, agent B's
activate_window can land between agent A's focus check and A's typing, and A's text goes into
B's window (bench spike, 2026-11-06: 2 x 40 words came out mixed without the lock, intact with it).

The lock is an flock on a file named after the display, so it also holds across processes that
know nothing about each other, and the kernel drops it if a process dies holding it.
"""

from __future__ import annotations

import fcntl
import os
import re
import time
from pathlib import Path

from ccdesk.errors import CcdeskError
from ccdesk.safety import cache_dir

DEFAULT_TIMEOUT = 30.0
POLL = 0.02


def lock_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return Path(runtime) / "ccdesk" if runtime else cache_dir() / "locks"


def seat_key(backend_name: str, env: dict) -> str:
    """The screen this backend drives: its X display, or its Wayland socket."""
    if backend_name == "x11":
        raw = env.get("DISPLAY")
    else:
        raw = env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")
    return re.sub(r"[^A-Za-z0-9_.-]", "_", raw or "default")


class SeatLock:
    """Reentrant within one process (nested input actions take it once), exclusive across processes."""

    def __init__(self, key: str, directory: Path | None = None, timeout: float = DEFAULT_TIMEOUT,
                 clock=time.monotonic, sleep=time.sleep):
        self.key = key
        self.path = (directory or lock_dir()) / f"input-{key}.lock"
        self.timeout = timeout
        self.clock = clock
        self.sleep = sleep
        self._fd: int | None = None
        self._depth = 0
        self.waited_ms = 0  # how long the latest outermost acquire waited for another agent

    def __enter__(self) -> SeatLock:
        if self._depth == 0:
            self._acquire()
        self._depth += 1
        return self

    def __exit__(self, *exc) -> None:
        self._depth -= 1
        if self._depth == 0 and self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def _acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        start = self.clock()
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if self.clock() - start >= self.timeout:
                    os.close(fd)
                    raise CcdeskError(f"another agent has held the input on {self.key} for "
                                      f"{self.timeout:.0f}s; wait() and retry") from None
                self.sleep(POLL)
        self.waited_ms = round((self.clock() - start) * 1000)
        self._fd = fd
