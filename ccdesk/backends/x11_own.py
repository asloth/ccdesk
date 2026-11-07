"""X11 backend for an agent with its own pointer and keyboard (`ccdesk mcp --own-pointer`).

Pointer, keys and focus go to the agent's XInput2 master pair (see mpx.py), so the user's pointer
and keyboard focus stay where they are. Windows, screenshots and the clipboard use the normal X11 path.
"""

from __future__ import annotations

from ccdesk.backends.base import interpolate
from ccdesk.backends.x11 import WHEEL_DOWN, WHEEL_LEFT, WHEEL_RIGHT, WHEEL_UP, X11Backend
from ccdesk.parsing import KeyCombo, Window

CLICK_DELAY = 0.08


class OwnPointerBackend(X11Backend):
    own_pointer = True

    def __init__(self, mpx, **kw):
        super().__init__(**kw)
        self.mpx = mpx
        self._note: str | None = None

    def take_note(self) -> str | None:
        note, self._note = self._note, None
        return note

    # -- pointer ---------------------------------------------------------------

    def move(self, x: int, y: int) -> None:
        self.mpx.warp(x, y)

    def _press(self, button: int) -> None:
        self.mpx.button(button, True)
        self.mpx.button(button, False)

    def click(self, x: int, y: int, button: int, count: int) -> None:
        self.mpx.warp(x, y)
        for i in range(count):
            if i:
                self.sleep(CLICK_DELAY)
            self._press(button)

    def drag(self, x0: int, y0: int, x1: int, y1: int, duration_ms: int) -> None:
        steps = self.drag_steps(duration_ms)
        pause = duration_ms / 1000 / steps
        self.mpx.warp(x0, y0)
        self.mpx.button(1, True)
        try:
            for x, y in interpolate(x0, y0, x1, y1, steps):
                self.sleep(pause)
                self.mpx.warp(x, y)
        finally:
            self.mpx.button(1, False)

    def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        self.mpx.warp(x, y)
        for _ in range(abs(dy)):
            self._press(WHEEL_DOWN if dy > 0 else WHEEL_UP)
            self.sleep(0.03)
        for _ in range(abs(dx)):
            self._press(WHEEL_RIGHT if dx > 0 else WHEEL_LEFT)
            self.sleep(0.03)

    # -- keyboard ---------------------------------------------------------------

    def type_text(self, text: str) -> None:
        self.mpx.type_text(text)

    def press_key(self, combo: KeyCombo) -> None:
        self.mpx.press_key(combo)

    # -- focus ------------------------------------------------------------------

    def activate_window(self, window: Window) -> None:
        """Give the agent keyboard this window. The window manager is only asked to raise or map
        it when it is not on screen (minimized, or on another desktop): that does move the user's focus."""
        shown = self.window_on_show(window.id)
        other_desktop = (window.desktop is not None and window.desktop >= 0
                         and self.current_desktop() not in (None, window.desktop))
        if shown is False or other_desktop:
            super().activate_window(window)
            self._note = ("the window was not on screen (minimized or on another desktop), so it was shown "
                          "with the normal activation; that moved your keyboard focus once")
        self.mpx.focus(int(window.id, 16))

    def active_window(self) -> str | None:
        wid = self.mpx.query_focus()
        return f"0x{wid:08x}" if wid else None

    def window_at(self, x: int, y: int) -> str | None:
        self.mpx.warp(x, y)
        chain = self.mpx.windows_under_pointer()
        if not chain:
            return None
        listed = {w.id for w in self.list_windows()}
        for wid in reversed(chain):  # the deepest window that is an app window
            if f"0x{wid:08x}" in listed:
                return f"0x{wid:08x}"
        return f"0x{chain[0]:08x}"
