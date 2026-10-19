"""Wayland backend: grim for pixels, ydotool/wtype for input, wl-clipboard, sway/Hyprland IPC for windows.

Wayland deliberately hides other clients from each other, so coverage depends on the compositor:
grim needs wlr-screencopy (sway, Hyprland, river, ...), ydotool needs access to /dev/uinput and
its daemon, and window listing only works through compositor IPC (swaymsg, hyprctl) or, for
XWayland apps only, through xdotool.
"""

from __future__ import annotations

import json
import os
import tempfile

from ccdesk.backends.base import Backend, interpolate
from ccdesk.backends.x11 import X11Backend
from ccdesk.errors import CommandError, UnsupportedError
from ccdesk.parsing import KeyCombo, Region, Window, png_size

# ydotool 1.x click codes: 0x40 = down, 0x80 = up, 0xC0 = down+up; low nibble = button.
YDOTOOL_BUTTON = {1: 0x00, 2: 0x02, 3: 0x01}
WTYPE_MODIFIER = {"ctrl": "ctrl", "alt": "alt", "shift": "shift", "super": "logo"}


def parse_sway_tree(tree: dict) -> list[Window]:
    windows: list[Window] = []

    def walk(node: dict) -> None:
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            if child.get("type") in ("con", "floating_con") and (child.get("pid") or child.get("window")):
                rect = child.get("rect", {})
                wm_class = child.get("app_id") or (child.get("window_properties") or {}).get("class") or ""
                windows.append(Window(
                    id=str(child["id"]), title=child.get("name") or "", wm_class=wm_class,
                    x=rect.get("x", 0), y=rect.get("y", 0),
                    width=rect.get("width", 0), height=rect.get("height", 0),
                ))
            walk(child)

    walk(tree)
    return windows


def parse_hyprctl_clients(clients: list) -> list[Window]:
    out = []
    for c in clients:
        at, size = c.get("at", [0, 0]), c.get("size", [0, 0])
        out.append(Window(
            id=c.get("address", ""), title=c.get("title", ""), wm_class=c.get("class", ""),
            x=at[0], y=at[1], width=size[0], height=size[1],
            desktop=(c.get("workspace") or {}).get("id"),
        ))
    return out


class WaylandBackend(Backend):
    name = "wayland"
    TOOLS = {
        "screenshot": ("grim", "gnome-screenshot"),
        "pointer": ("ydotool",),
        "keyboard": ("wtype", "ydotool"),
        "windows": ("swaymsg", "hyprctl", "xdotool"),
        "clipboard": ("wl-copy", "wl-paste"),
    }

    def available(self) -> str | None:
        if not self.env.get("WAYLAND_DISPLAY"):
            return "WAYLAND_DISPLAY is not set; no Wayland session to control"
        return None

    def _xwayland(self) -> X11Backend | None:
        """XWayland fallback for window listing; only sees X11 clients."""
        if self.env.get("DISPLAY") and self.which("xdotool"):
            return X11Backend(env=self.env, which=self._which, runner=self._runner)
        return None

    def screen_size(self) -> tuple[int, int]:
        # Compositors disagree on how to report outputs; a full grab is the portable answer.
        fd, tmp = tempfile.mkstemp(prefix="ccdesk-size-", suffix=".png")
        os.close(fd)
        try:
            self.screenshot(tmp, None)
            return png_size(tmp)
        finally:
            os.unlink(tmp)

    def screenshot(self, path: str, region: Region | None) -> None:
        if self.which("grim"):
            argv = ["grim"]
            if region:
                argv += ["-g", f"{region.x},{region.y} {region.width}x{region.height}"]
            self.run(argv + [path], timeout=30)
            return
        if self.which("gnome-screenshot"):
            if region:
                raise UnsupportedError("region screenshots need grim; gnome-screenshot can only grab the full screen")
            self.run(["gnome-screenshot", "-f", path], timeout=30)
            return
        self.need("screenshot", "grim", "gnome-screenshot")

    def _ydotool(self, *args: str) -> str:
        self.need("pointer input", "ydotool")
        return self.run(["ydotool", *args])

    def move(self, x: int, y: int) -> None:
        self._ydotool("mousemove", "--absolute", "-x", str(x), "-y", str(y))

    def click(self, x: int, y: int, button: int, count: int) -> None:
        self.move(x, y)
        code = 0xC0 | YDOTOOL_BUTTON[button]
        self._ydotool("click", "--repeat", str(count), "--next-delay", "80", hex(code))

    def drag(self, x0: int, y0: int, x1: int, y1: int, duration_ms: int) -> None:
        steps = self.drag_steps(duration_ms)
        pause = duration_ms / 1000 / steps
        self.move(x0, y0)
        self._ydotool("click", hex(0x40))
        try:
            for x, y in interpolate(x0, y0, x1, y1, steps):
                self.sleep(pause)
                self.move(x, y)
        finally:
            self._ydotool("click", hex(0x80))

    def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        self.move(x, y)
        # ydotool wheel: positive y scrolls up, so invert to keep "dy > 0 means down".
        self._ydotool("mousemove", "--wheel", "-x", str(dx), "-y", str(-dy))

    def type_text(self, text: str) -> None:
        if self.which("wtype"):
            self.run(["wtype", "-d", "12", "--", text], timeout=max(15, len(text) * 0.05 + 5))
        else:
            self._ydotool("type", "--key-delay", "12", "--", text)

    def press_key(self, combo: KeyCombo) -> None:
        if not self.which("wtype"):
            raise UnsupportedError("press_key on Wayland needs wtype (ydotool only speaks raw keycodes)")
        argv = ["wtype"]
        for m in combo.modifiers:
            argv += ["-M", WTYPE_MODIFIER[m]]
        argv += ["-k", combo.key]
        for m in reversed(combo.modifiers):
            argv += ["-m", WTYPE_MODIFIER[m]]
        self.run(argv)

    def list_windows(self) -> list[Window]:
        if self.which("swaymsg") and self.env.get("SWAYSOCK"):
            return parse_sway_tree(json.loads(self.run(["swaymsg", "-t", "get_tree", "-r"])))
        if self.which("hyprctl") and self.env.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return parse_hyprctl_clients(json.loads(self.run(["hyprctl", "clients", "-j"])))
        x = self._xwayland()
        if x:
            return x.list_windows()
        raise UnsupportedError(
            "this Wayland compositor does not expose a window list; use screenshot() and click instead"
        )

    def activate_window(self, window: Window) -> None:
        if self.which("swaymsg") and self.env.get("SWAYSOCK"):
            self.run(["swaymsg", f"[con_id={window.id}]", "focus"])
        elif self.which("hyprctl") and self.env.get("HYPRLAND_INSTANCE_SIGNATURE"):
            self.run(["hyprctl", "dispatch", "focuswindow", f"address:{window.id}"])
        elif (x := self._xwayland()) is not None:
            x.activate_window(window)
        else:
            raise UnsupportedError("this Wayland compositor does not allow activating windows from outside")

    def clipboard_get(self) -> str:
        self.need("clipboard", "wl-paste")
        try:
            return self.run(["wl-paste", "--no-newline"], timeout=5)
        except CommandError as exc:
            if "No selection" in str(exc) or "nothing" in str(exc).lower():
                return ""
            raise

    def clipboard_set(self, text: str) -> None:
        self.need("clipboard", "wl-copy")
        self.run(["wl-copy"], input=text, timeout=5, detach=True)
