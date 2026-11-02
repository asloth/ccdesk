"""KDE Plasma backend: spectacle for pixels, kdotool for windows, ydotool for input.

KWin does not speak wlr-screencopy, so grim cannot grab the screen on Plasma: the compositor
answers "the screen capture protocol is not supported". spectacle does, through KDE's own
screenshot stack, on Wayland and on X11.

Windows come from kdotool, which loads a small script into KWin over D-Bus and reads its answer
back. That is the only route to the window list on Plasma Wayland, where nothing outside the
compositor can enumerate windows. One script answers for every window at once.

Input is ydotool, which writes to /dev/uinput: Plasma offers neither the virtual-pointer nor the
virtual-keyboard protocol, so there is no unprivileged path. Absolute positioning then depends on
the pointer acceleration curve, so clicks have to be verified with a screenshot.
"""

from __future__ import annotations

import json
import os
import re
import tempfile

from ccdesk.backends.keycodes import ydotool_key_sequence
from ccdesk.backends.wayland import WaylandBackend
from ccdesk.errors import CcdeskError, CommandError, UnsupportedError
from ccdesk.parsing import KeyCombo, Region, Window

# One KWin script answers every window: id, title, class, pid, frame box, state and desktop.
# workspace.stackingOrder runs bottom to top, so the last window holding a point is the one that
# receives a click there.
WINDOWS_SCRIPT = (
    "var out=[];var ls=workspace.stackingOrder;"
    "for(var i=0;i<ls.length;i++){var w=ls[i];var f=w.frameGeometry;var d=null;"
    "try{if(w.desktops&&w.desktops.length){d=w.desktops[0].x11DesktopNumber;}}catch(e){}"
    "out.push({id:String(w.internalId),title:w.caption,cls:w.resourceClass,pid:w.pid,"
    "x:f.x,y:f.y,width:f.width,height:f.height,minimized:w.minimized,"
    "active:(w===workspace.activeWindow),desktop:d});}"
    "output_result(JSON.stringify(out));"
)
SCREENS_SCRIPT = (
    "var o=[];var s=workspace.screens;"
    "for(var i=0;i<s.length;i++){var g=s[i].geometry;"
    "o.push({name:s[i].name,x:g.x,y:g.y,width:g.width,height:g.height,scale:s[i].devicePixelRatio||1});}"
    "output_result(JSON.stringify(o));"
)
# The ids kdotool prints look like {xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx}. They go into a command
# line, so refuse anything else instead of letting it read as an option.
KDOTOOL_ID = re.compile(r"^\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}$")


def parse_kwin_windows(payload) -> list[Window]:
    if not isinstance(payload, list):
        raise CommandError("kdotool did not return a window list")
    windows: list[Window] = []
    for raw in payload:
        try:
            windows.append(Window(
                id=str(raw["id"]), title=raw.get("title") or "", wm_class=raw.get("cls") or "",
                x=int(raw.get("x") or 0), y=int(raw.get("y") or 0),
                width=int(raw.get("width") or 0), height=int(raw.get("height") or 0),
                desktop=None if raw.get("desktop") is None else int(raw["desktop"]),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            raise CommandError(f"unexpected window from kdotool: {raw!r}") from exc
    return windows


def parse_kwin_screens(payload) -> list[dict]:
    monitors = []
    for raw in payload if isinstance(payload, list) else []:
        try:
            monitors.append({
                "name": raw.get("name") or "", "x": int(raw.get("x") or 0), "y": int(raw.get("y") or 0),
                "width": int(raw.get("width") or 0), "height": int(raw.get("height") or 0),
                "scale": float(raw.get("scale") or 1),
            })
        except (TypeError, ValueError) as exc:
            raise CommandError(f"unexpected screen from kdotool: {raw!r}") from exc
    return monitors


def kwin_window_at(windows: list[dict], x: int, y: int, current_desktop: int | None = None) -> str | None:
    """Id of the window that would receive a click at (x, y), from the stacking order KWin reports.

    This is inferred, like the Hyprland version: KWin exposes no query for it."""
    for raw in reversed(windows):
        if raw.get("minimized"):
            continue
        desktop = raw.get("desktop")
        if desktop is not None and current_desktop is not None and int(desktop) != current_desktop:
            continue
        try:
            wx, wy = int(raw.get("x")), int(raw.get("y"))
            ww, wh = int(raw.get("width")), int(raw.get("height"))
        except (TypeError, ValueError):
            continue
        if wx <= x < wx + ww and wy <= y < wy + wh:
            return str(raw.get("id")) or None
    return None


def ydotoold_socket(env: dict) -> str | None:
    """The socket of a running ydotoold, wherever the daemon put it."""
    for path in (env.get("YDOTOOL_SOCKET"), f"{env.get('XDG_RUNTIME_DIR') or ''}/.ydotool_socket",
                 "/tmp/.ydotool_socket"):
        if path and os.path.exists(path):
            return path
    return None


class KdeBackend(WaylandBackend):
    name = "kde"
    TOOLS = {
        "screenshot": ("spectacle",),
        "pointer": ("ydotool",),
        "keyboard": ("ydotool",),
        "windows": ("kdotool",),
        "clipboard": ("wl-copy", "wl-paste"),
    }

    # -- helpers ---------------------------------------------------------

    def _kdotool(self, *args: str, **kw) -> str:
        self.need("window control on KDE", "kdotool")
        return self.run(["kdotool", *args], **kw)

    def _kwin_json(self, script: str):
        out = self._kdotool("kwinscript", "--inline", script, timeout=30).strip()
        try:
            return json.loads(out or "null")
        except json.JSONDecodeError as exc:
            raise CommandError(f"kdotool did not answer with JSON: {out[:120]!r}") from exc

    def _windows_raw(self) -> list[dict]:
        data = self._kwin_json(WINDOWS_SCRIPT)
        if not isinstance(data, list):
            raise CommandError("kdotool did not return a window list")
        return data

    def _safe_windows(self) -> list[dict]:
        """The window list, or [] where kdotool can't be asked: the focus checks then stay unknown."""
        if not self.which("kdotool"):
            return []
        try:
            return self._windows_raw()
        except CcdeskError:
            return []

    @staticmethod
    def _window_id(window: Window) -> str:
        if not KDOTOOL_ID.match(window.id):
            raise CommandError(f"{window.id!r} is not a KWin window id")
        return window.id

    # -- pixels ----------------------------------------------------------

    def screenshot(self, path: str, region: Region | None) -> None:
        if region is None:
            self._spectacle(path)
            return
        # spectacle has no way to pass a region: grab the screen and cut the part asked for.
        fd, tmp = tempfile.mkstemp(prefix="ccdesk-kde-", suffix=".png")
        os.close(fd)
        try:
            self._spectacle(tmp)
            tool = self.first_tool("magick", "convert")
            if not tool:
                raise UnsupportedError("region screenshots on KDE need ImageMagick (magick or convert)")
            self.run([tool, tmp, "-crop", f"{region.width}x{region.height}+{region.x}+{region.y}",
                      "+repage", f"PNG24:{path}"], timeout=30)
        finally:
            os.unlink(tmp)

    def _spectacle(self, path: str) -> None:
        self.need("screenshots on KDE", "spectacle")
        # --background returns instead of keeping spectacle running; --nonotify skips its toast.
        self.run(["spectacle", "--fullscreen", "--background", "--nonotify", "--output", path], timeout=45)
        if not os.path.exists(path) or not os.path.getsize(path):
            raise CommandError("spectacle wrote no screenshot (is the screen locked?)")

    def monitors(self) -> list[dict]:
        if not self.which("kdotool"):
            return []
        try:
            return parse_kwin_screens(self._kwin_json(SCREENS_SCRIPT))
        except CcdeskError:
            return []

    def screen_size(self) -> tuple[int, int]:
        monitors = self.monitors()
        if monitors:
            return (max(m["x"] + m["width"] for m in monitors),
                    max(m["y"] + m["height"] for m in monitors))
        return super().screen_size()  # no kdotool: a full grab is the answer

    # -- input -----------------------------------------------------------

    def _pointer(self):
        return None  # KWin offers no virtual-pointer protocol: ydotool has to do it

    def type_text(self, text: str) -> None:
        # Not wtype: Plasma has no virtual-keyboard protocol, so wtype would fail on every call.
        self.need("keyboard input on KDE", "ydotool")
        self.run(["ydotool", "type", "--key-delay", "12", "--file", "-"], input=text,
                 timeout=max(15, len(text) * 0.05 + 5))

    def press_key(self, combo: KeyCombo) -> None:
        self.need("keyboard input on KDE", "ydotool")
        self.run(["ydotool", "key", "--key-delay", "12", *ydotool_key_sequence(combo)])

    # -- windows ---------------------------------------------------------

    def list_windows(self) -> list[Window]:
        return parse_kwin_windows(self._windows_raw())

    def activate_window(self, window: Window) -> None:
        self._kdotool("windowactivate", self._window_id(window))

    def move_window(self, window: Window, x: int, y: int) -> None:
        self._kdotool("windowmove", self._window_id(window), str(x), str(y))

    def resize_window(self, window: Window, width: int, height: int) -> None:
        self._kdotool("windowsize", self._window_id(window), str(width), str(height))

    def minimize_window(self, window: Window) -> None:
        self._kdotool("windowminimize", self._window_id(window))

    def maximize_window(self, window: Window) -> None:
        self._kdotool("windowstate", self._window_id(window), "--add", "maximized")

    def restore_window(self, window: Window) -> None:
        target = self._window_id(window)
        for state in ("maximized", "fullscreen", "minimized"):
            self._kdotool("windowstate", target, "--remove", state)
        self._kdotool("windowactivate", target)

    def window_on_show(self, window_id: str) -> bool | None:
        raw = next((w for w in self._safe_windows() if str(w.get("id")) == window_id), None)
        if raw is None:
            return None
        if raw.get("minimized"):
            return False
        desktop, current = raw.get("desktop"), self.current_desktop()
        return desktop is None or current is None or int(desktop) == current

    def active_window(self) -> str | None:
        return next((str(w.get("id")) for w in self._safe_windows() if w.get("active")), None)

    def window_at(self, x: int, y: int) -> str | None:
        return kwin_window_at(self._safe_windows(), x, y, self.current_desktop())

    def current_desktop(self) -> int | None:
        if not self.which("kdotool"):
            return None
        try:
            return int((self.run(["kdotool", "get_desktop"]) or "").strip())
        except (CcdeskError, ValueError):
            return None
