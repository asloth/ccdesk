"""Keep bspwm from moving the user's keyboard focus when an agent clicks.

bspwm grabs button 1 (a core grab) on every window that is not focused. An agent's click on such a
window activates that grab, and bspwm then calls SetInputFocus twice; the second call lands on the
USER's core keyboard (20 of 20 runs in a spike on Xvfb). The only setting that stops it is
`click_to_focus none`, so every agent button press runs with it set and the saved value is put back
right after, under a display-wide file lock so that two agents never overwrite each other's saved value.

Crash safety: the original value is written to a state file before it is changed and deleted after it
is restored. At server start and in `ccdesk doctor`, a state file with a free lock means a process died
in between: recover() puts the value back.

Only bspwm is handled. With another window manager an agent's click may still move the user's focus.
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from pathlib import Path

from ccdesk.errors import CcdeskError
from ccdesk.seat import POLL, _screen_key, lock_dir

SETTLE = 0.1  # seconds after the release: bspwm handles the grab asynchronously (the spike's value)
KEY = "click_to_focus"
LOCK_TIMEOUT = 30.0


def _paths(env: dict, directory: Path | None) -> tuple[Path, Path]:
    d = directory or lock_dir()
    key = _screen_key("x11", env)
    return d / f"wm-click-{key}.lock", d / f"wm-click-{key}.state"


class ClickGuard:
    """`with guard.quiet():` around a button press. `run(argv)` returns stdout or None on failure."""

    def __init__(self, run, env: dict, which=None, directory: Path | None = None, sleep=time.sleep,
                 settle: float = SETTLE):
        self._run, self.env, self._which = run, env, which
        self.lock_path, self.state_path = _paths(env, directory)
        self.sleep, self.settle = sleep, settle
        self._active: bool | None = None
        self._threads = threading.RLock()
        self._depth = 0
        self._fd: int | None = None
        self._original: str | None = None
        self.unavailable: str | None = None

    def active(self) -> bool:
        """Is the window manager a reachable bspwm? Decided once."""
        if self._active is None:
            self._active = bool((self._which is None or self._which("bspc"))
                                and self._get() is not None)
        return self._active

    def _get(self) -> str | None:
        out = self._run(["bspc", "config", KEY])
        return out.strip() if out is not None and out.strip() else None

    def _set(self, value: str) -> bool:
        return self._run(["bspc", "config", KEY, value]) is not None

    def quiet(self) -> _Quiet:
        return _Quiet(self)

    # -- enter / exit ----------------------------------------------------------------------

    def _enter(self) -> None:
        self._threads.acquire()
        try:
            if self._depth == 0 and self.active():
                self._lock_and_silence()
        except BaseException:
            self._threads.release()
            raise
        self._depth += 1

    def _lock_and_silence(self) -> None:
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            self.unavailable = f"no window-manager click lock ({exc}); bspwm may move your keyboard focus"
            return
        start = time.monotonic()
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - start >= LOCK_TIMEOUT:
                    os.close(fd)
                    raise CcdeskError("another agent has been changing bspwm's click_to_focus for "
                                      f"{LOCK_TIMEOUT:.0f}s; wait() and retry") from None
                time.sleep(POLL)
        try:
            saved = self._read_state()
            # A state file means an earlier process died with "none" set: that file holds the
            # real value, and what bspwm reports now is the "none" we left.
            original = saved if saved is not None else self._get()
            if original is None:
                raise CcdeskError("cannot read bspwm's click_to_focus")
            if saved is None:
                self.state_path.write_text(original)
            self._original = original
            self._fd = fd
            if not self._set("none"):
                raise CcdeskError("cannot set bspwm's click_to_focus to none")
        except BaseException:
            self._restore()
            self._unlock(fd)
            self._fd = None
            raise

    def _read_state(self) -> str | None:
        try:
            return self.state_path.read_text().strip() or None
        except OSError:
            return None

    def _restore(self) -> None:
        if self._original is None:
            return
        if self._set(self._original):
            try:
                self.state_path.unlink()
            except OSError:
                pass
        self._original = None  # on failure the state file stays for recover()

    @staticmethod
    def _unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _exit(self) -> None:
        self._depth -= 1
        try:
            if self._depth == 0 and self._fd is not None:
                fd, self._fd = self._fd, None
                try:
                    self.sleep(self.settle)
                finally:
                    try:
                        self._restore()
                    finally:
                        self._unlock(fd)
        finally:
            self._threads.release()

    def abort(self) -> None:
        """From a signal handler: put the value back now, without waiting for the worker thread."""
        if self._original is not None:
            self._restore()

    # -- crash recovery ---------------------------------------------------------------------

    def recover(self) -> str | None:
        """A state file whose lock is free: restore its value. Returns the value put back."""
        try:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None  # someone is mid-click: not stale
            saved = self._read_state()
            if saved is None or not self.active():
                return None
            if self._set(saved):
                try:
                    self.state_path.unlink()
                except OSError:
                    pass
                return saved
            return None
        finally:
            os.close(fd)


class _Quiet:
    def __init__(self, guard: ClickGuard):
        self.guard = guard

    def __enter__(self):
        self.guard._enter()
        return self

    def __exit__(self, *exc) -> None:
        self.guard._exit()
