"""Backend base class: thin wrappers around system CLI tools."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Callable

from ccdesk.errors import CommandError, UnsupportedError
from ccdesk.parsing import KeyCombo, Region, Window

DEFAULT_TIMEOUT = 15.0


def interpolate(x0: int, y0: int, x1: int, y1: int, steps: int) -> list[tuple[int, int]]:
    """Points from (x0,y0) to (x1,y1), excluding the start and including the end."""
    steps = max(1, steps)
    return [(round(x0 + (x1 - x0) * i / steps), round(y0 + (y1 - y0) * i / steps)) for i in range(1, steps + 1)]


class Backend:
    name = "none"
    # Tools this backend knows how to use, grouped by capability, for `doctor`.
    TOOLS: dict[str, tuple[str, ...]] = {}

    def __init__(self, env: dict | None = None, which: Callable[[str], str | None] = shutil.which,
                 runner: Callable | None = None):
        self.env = dict(os.environ if env is None else env)
        self._which = which
        self._runner = runner or self._subprocess_run

    # -- process helpers -------------------------------------------------

    def which(self, name: str) -> str | None:
        return self._which(name)

    def first_tool(self, *names: str) -> str | None:
        for n in names:
            if self.which(n):
                return n
        return None

    def need(self, capability: str, *names: str) -> str:
        tool = self.first_tool(*names)
        if not tool:
            raise CommandError(f"{capability} needs one of: {', '.join(names)} (install it and run `ccdesk doctor`)")
        return tool

    @staticmethod
    def _subprocess_run(argv: list[str], input: str | None = None, timeout: float = DEFAULT_TIMEOUT,
                        detach: bool = False) -> str:
        try:
            if detach:
                # Clipboard owners (xclip, wl-copy) fork and keep serving the selection; never
                # capture their output or we would wait for them forever.
                proc = subprocess.run(argv, input=input, text=True, timeout=timeout,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                out, err = "", ""
            else:
                proc = subprocess.run(argv, input=input, text=True, timeout=timeout, capture_output=True)
                out, err = proc.stdout, proc.stderr
        except FileNotFoundError:
            raise CommandError(f"{argv[0]} is not installed") from None
        except subprocess.TimeoutExpired:
            raise CommandError(f"{argv[0]} timed out after {timeout:.0f}s") from None
        if proc.returncode != 0:
            msg = (err or out).strip().splitlines()
            raise CommandError(f"{' '.join(argv[:2])} failed (exit {proc.returncode}): {msg[-1] if msg else 'no output'}")
        return out

    def run(self, argv: list[str], input: str | None = None, timeout: float = DEFAULT_TIMEOUT,
            detach: bool = False) -> str:
        return self._runner(argv, input=input, timeout=timeout, detach=detach)

    def try_run(self, argv: list[str], **kw) -> str | None:
        try:
            return self.run(argv, **kw)
        except CommandError:
            return None

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)

    # -- capability surface ---------------------------------------------

    def available(self) -> str | None:
        """Return None if usable, else a human-readable reason."""
        return "no graphical session found (DISPLAY and WAYLAND_DISPLAY are unset)"

    def ensure_available(self) -> None:
        reason = self.available()
        if reason:
            raise UnsupportedError(reason)

    def tools_report(self) -> dict[str, dict[str, str | None]]:
        return {cap: {n: self.which(n) for n in names} for cap, names in self.TOOLS.items()}

    def _unsupported(self, what: str):
        raise UnsupportedError(f"{what} is not supported by the {self.name} backend")

    def screen_size(self) -> tuple[int, int]:
        self._unsupported("screen size")

    def scale_info(self) -> dict:
        info: dict = {}
        for var in ("GDK_SCALE", "GDK_DPI_SCALE", "QT_SCALE_FACTOR", "QT_SCREEN_SCALE_FACTORS"):
            if self.env.get(var):
                info[var] = self.env[var]
        return info

    def monitors(self) -> list[dict]:
        """Monitor rectangles in screen coordinates, or [] when unknown."""
        return []

    def screenshot(self, path: str, region: Region | None) -> None:
        self._unsupported("screenshot")

    def move(self, x: int, y: int) -> None:
        self._unsupported("move")

    def click(self, x: int, y: int, button: int, count: int) -> None:
        self._unsupported("click")

    def drag(self, x0: int, y0: int, x1: int, y1: int, duration_ms: int) -> None:
        self._unsupported("drag")

    def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        self._unsupported("scroll")

    def type_text(self, text: str) -> None:
        self._unsupported("type_text")

    def press_key(self, combo: KeyCombo) -> None:
        self._unsupported("press_key")

    def list_windows(self) -> list[Window]:
        self._unsupported("list_windows")

    def activate_window(self, window: Window) -> None:
        self._unsupported("activate_window")

    def clipboard_get(self) -> str:
        self._unsupported("clipboard")

    def clipboard_set(self, text: str) -> None:
        self._unsupported("clipboard")

    @staticmethod
    def drag_steps(duration_ms: int) -> int:
        return max(2, min(60, duration_ms // 16))
