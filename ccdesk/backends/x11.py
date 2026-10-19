"""X11 backend: xdotool for input, scrot/import for pixels, wmctrl for windows, xclip for clipboard."""

from __future__ import annotations

from ccdesk.backends.base import Backend, interpolate
from ccdesk.errors import CommandError
from ccdesk.parsing import (
    KeyCombo,
    Region,
    Window,
    parse_display_geometry,
    parse_shell_vars,
    parse_wmctrl_lgx,
    parse_xprop_wm_class,
    parse_xrdb_dpi,
)

# X button numbers for the scroll wheel.
WHEEL_UP, WHEEL_DOWN, WHEEL_LEFT, WHEEL_RIGHT = 4, 5, 6, 7


class X11Backend(Backend):
    name = "x11"
    TOOLS = {
        "screenshot": ("scrot", "import"),
        "input": ("xdotool",),
        "windows": ("wmctrl", "xdotool", "xprop"),
        "clipboard": ("xclip", "xsel"),
        "scale": ("xrdb",),
    }

    def available(self) -> str | None:
        if not self.env.get("DISPLAY"):
            return "DISPLAY is not set; no X11 session to control"
        return None

    def _xdotool(self, *args: str, **kw) -> str:
        self.need("input", "xdotool")
        return self.run(["xdotool", *args], **kw)

    def screen_size(self) -> tuple[int, int]:
        return parse_display_geometry(self._xdotool("getdisplaygeometry"))

    def scale_info(self) -> dict:
        info = super().scale_info()
        if self.which("xrdb"):
            dpi = parse_xrdb_dpi(self.try_run(["xrdb", "-query"]) or "")
            if dpi:
                info["Xft.dpi"] = dpi
        return info

    def screenshot(self, path: str, region: Region | None) -> None:
        attempts = []
        if self.which("scrot"):
            argv = ["scrot", "-o", "-z"]
            if region:
                argv += ["-a",f"{region.x},{region.y},{region.width},{region.height}"]
            attempts.append(argv + [path])
        if self.which("import"):
            argv = ["import", "-silent", "-window", "root"]
            if region:
                argv += ["-crop", region.geometry(), "+repage"]
            attempts.append(argv + [f"png:{path}"])
        if not attempts:
            self.need("screenshot", "scrot", "import")
        errors = []
        for argv in attempts:
            try:
                self.run(argv, timeout=30)
                return
            except CommandError as exc:
                errors.append(str(exc))
        raise CommandError("; ".join(errors))

    def move(self, x: int, y: int) -> None:
        self._xdotool("mousemove", str(x), str(y))

    def click(self, x: int, y: int, button: int, count: int) -> None:
        self._xdotool("mousemove", str(x), str(y), "click", "--repeat", str(count), "--delay", "80", str(button))

    def drag(self, x0: int, y0: int, x1: int, y1: int, duration_ms: int) -> None:
        steps = self.drag_steps(duration_ms)
        pause = duration_ms / 1000 / steps
        self._xdotool("mousemove", str(x0), str(y0), "mousedown", "1")
        try:
            for x, y in interpolate(x0, y0, x1, y1, steps):
                self.sleep(pause)
                self._xdotool("mousemove", str(x), str(y))
        finally:
            self._xdotool("mouseup", "1")

    def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        argv = ["mousemove", str(x), str(y)]
        if dy:
            argv += ["click", "--repeat", str(abs(dy)), "--delay", "30", str(WHEEL_DOWN if dy > 0 else WHEEL_UP)]
        if dx:
            argv += ["click", "--repeat", str(abs(dx)), "--delay", "30", str(WHEEL_RIGHT if dx > 0 else WHEEL_LEFT)]
        self._xdotool(*argv)

    def type_text(self, text: str) -> None:
        self._xdotool("type", "--clearmodifiers", "--delay", "12", "--", text, timeout=max(15, len(text) * 0.05 + 5))

    def press_key(self, combo: KeyCombo) -> None:
        self._xdotool("key", "--clearmodifiers", combo.xdotool())

    # -- windows ------------------------------------------------------------

    def list_windows(self) -> list[Window]:
        if self.which("wmctrl"):
            out = self.try_run(["wmctrl", "-lGx"])
            if out is not None:
                return parse_wmctrl_lgx(out)
        # No window manager (e.g. bare Xvfb) or no wmctrl: ask xdotool directly.
        return self._xdotool_windows()

    def _xdotool_windows(self) -> list[Window]:
        try:
            out = self._xdotool("search", "--onlyvisible", "--name", ".")
        except CommandError:
            return []  # xdotool search exits 1 when nothing matches
        windows = []
        for wid in dict.fromkeys(out.split()):
            title = (self.try_run(["xdotool", "getwindowname", wid]) or "").strip()
            if not title:
                continue
            geo = parse_shell_vars(self.try_run(["xdotool", "getwindowgeometry", "--shell", wid]) or "")
            wm_class = self._window_class(wid)
            try:
                windows.append(Window(
                    id=f"0x{int(wid):08x}", title=title, wm_class=wm_class,
                    x=int(geo.get("X", 0)), y=int(geo.get("Y", 0)),
                    width=int(geo.get("WIDTH", 0)), height=int(geo.get("HEIGHT", 0)),
                ))
            except ValueError:
                continue
        return windows

    def _window_class(self, wid: str) -> str:
        if self.which("xprop"):
            cls = parse_xprop_wm_class(self.try_run(["xprop", "-id", wid, "WM_CLASS"]) or "")
            if cls:
                return cls
        # getwindowclassname only exists in newer xdotool releases.
        return (self.try_run(["xdotool", "getwindowclassname", wid]) or "").strip()

    def activate_window(self, window: Window) -> None:
        if self.which("wmctrl") and self.try_run(["wmctrl", "-i", "-a", window.id]) is not None:
            return
        wid = str(int(window.id, 16))
        if self.try_run(["xdotool", "windowactivate", "--sync", wid], timeout=5) is not None:
            return
        # Without an EWMH window manager there is no "active window": raise and focus instead.
        self._xdotool("windowraise", wid, "windowfocus", wid)

    # -- clipboard ----------------------------------------------------------

    def clipboard_get(self) -> str:
        tool = self.need("clipboard", "xclip", "xsel")
        argv = ["xclip", "-selection", "clipboard", "-o"] if tool == "xclip" else ["xsel", "--clipboard", "--output"]
        try:
            return self.run(argv, timeout=5)
        except CommandError as exc:
            if "not available" in str(exc) or "exit 1" in str(exc):
                return ""  # empty clipboard
            raise

    def clipboard_set(self, text: str) -> None:
        tool = self.need("clipboard", "xclip", "xsel")
        argv = ["xclip", "-selection", "clipboard", "-i"] if tool == "xclip" else ["xsel", "--clipboard", "--input"]
        self.run(argv, input=text, timeout=5, detach=True)
