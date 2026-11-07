"""One input lock per screen, shared by every ccdesk process that drives it.

A screen has one pointer and one keyboard focus. When several agents (several `ccdesk mcp`
servers) drive the same display, each input action takes this lock for its whole run: the focus
check in Desktop._guard, any re-focus, and the clicks or keys themselves. Without it, agent B's
activate_window can land between agent A's focus check and A's typing, and A's text goes into
B's window (bench spike, 2026-11-06: 2 x 40 words came out mixed without the lock, intact with it).
What comes after the input (waiting for the app to settle, re-reading the window, comparing pixels)
only reads the screen, so it runs once the lock is released (Desktop._act).

The lock is an flock on a file named after the display, so it also holds across processes that
know nothing about each other, and the kernel drops it if a process dies holding it.
"""

from __future__ import annotations

import fcntl
import os
import re
import threading
import time
from pathlib import Path

from ccdesk.errors import CcdeskError
from ccdesk.safety import cache_dir

# One type_text of the longest allowed text takes ~2 minutes at xdotool's 12 ms per character:
# a waiting agent must outlast that. CCDESK_SEAT_TIMEOUT overrides it (seconds).
DEFAULT_TIMEOUT = 180.0
POLL = 0.02


def lock_dir() -> Path:
    """The same folder for every ccdesk of this user, however it was started: a server run from a
    systemd unit or `env -i` without XDG_RUNTIME_DIR must still find the terminal one's lock."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "ccdesk" if os.path.isdir(runtime) else cache_dir() / "locks"


def default_timeout() -> float:
    try:
        return max(1.0, float(os.environ["CCDESK_SEAT_TIMEOUT"]))
    except (KeyError, ValueError):
        return DEFAULT_TIMEOUT


def seat_key(backend_name: str, env: dict) -> str:
    """The screen this backend drives: its X display, or its Wayland socket."""
    if backend_name == "x11":
        raw = env.get("DISPLAY")
    else:
        raw = env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")
    return re.sub(r"[^A-Za-z0-9_.-]", "_", raw or "default")


class SeatLock:
    """Exclusive across processes and across the threads of one process (the MCP server runs each
    tool call on a worker thread, so one agent's calls can overlap); reentrant for the thread that
    holds it, so a nested input action takes it once."""

    def __init__(self, key: str, directory: Path | None = None, timeout: float | None = None,
                 clock=time.monotonic, sleep=time.sleep):
        self.key = key
        self.path = (directory or lock_dir()) / f"input-{key}.lock"
        self.timeout = default_timeout() if timeout is None else timeout
        self.clock = clock
        self.sleep = sleep
        self._fd: int | None = None
        self._depth = 0
        self._threads = threading.RLock()
        self.waited_ms = 0  # how long the latest outermost acquire waited for another agent
        self.unavailable: str | None = None  # why there is no lock file: input then goes unlocked

    def __enter__(self) -> SeatLock:
        start = self.clock()
        if not self._threads.acquire(timeout=self.timeout):
            raise CcdeskError(f"another call of this agent has held the input on {self.key} for "
                              f"{self.timeout:.0f}s; wait() and retry")
        try:
            if self._depth == 0:
                self._acquire(start)
        except BaseException:
            self._threads.release()
            raise
        self._depth += 1
        return self

    def __exit__(self, *exc) -> None:
        self._depth -= 1
        try:
            if self._depth == 0 and self._fd is not None:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                finally:
                    os.close(self._fd)
                    self._fd = None
        finally:
            self._threads.release()

    def _acquire(self, start: float) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            # A read-only home or a container without a runtime dir: one agent alone still works
            # as it did before there was a lock. doctor() reports it.
            self.unavailable = f"no input lock ({exc}); several agents on this screen may mix their input"
            return
        self.unavailable = None
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

    # When any agent on this screen last sent input, so --yield-ms can tell the user's input from
    # the agents' (X's idle counter is reset by both). Written into the lock file itself; the value
    # is a CLOCK_MONOTONIC reading, which is the same clock in every process.

    def note_input(self, when: float) -> None:
        if self._fd is None:
            return
        try:
            os.ftruncate(self._fd, 0)
            os.pwrite(self._fd, repr(float(when)).encode(), 0)
        except OSError:
            pass

    def last_input(self) -> float | None:
        try:
            with open(self.path, "rb") as f:
                return float(f.read(64) or b"nan") if self.path.exists() else None
        except (OSError, ValueError):
            return None
