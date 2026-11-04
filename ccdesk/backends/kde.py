"""KDE Plasma backend: spectacle for pixels, kdotool for windows, ydotool for input.

KWin does not speak wlr-screencopy, so grim cannot grab the screen on Plasma: the compositor
answers "the screen capture protocol is not supported". spectacle does, through KDE's own
screenshot stack. A Plasma session on X11 does not come here: it has xdotool and the X11 backend.

Windows come from kdotool, which loads a small script into KWin over D-Bus and reads its answer
back. That is the only route to the window list on Plasma Wayland, where nothing outside the
compositor can enumerate windows. One script answers for every window at once.

Input is ydotool, which writes to /dev/uinput: Plasma offers neither the virtual-pointer nor the
virtual-keyboard protocol, so there is no unprivileged path. Absolute positioning then depends on
the pointer acceleration curve, so clicks have to be verified with a screenshot. ydotool also sends
US-layout keycodes and only knows ASCII, so other text is better pasted.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import replace

from ccdesk.backends.base import interpolate
from ccdesk.backends.keycodes import ydotool_key_sequence
from ccdesk.backends.wayland import POINTER_SETTLE, WaylandBackend
from ccdesk.errors import CcdeskError, CommandError, UnsupportedError
from ccdesk.parsing import KeyCombo, Region, Window, png_size

# One KWin script answers every window: id, title, class, pid, frame and content boxes, state, desktops,
# activity and parent. Two kinds of surface are left out of list_windows. `shell` is Plasma's own:
# panels, the desktop, notifications, on-screen displays and applet popups such as the launcher. They
# take input themselves, so the focus guard names them (shell_window). `popup` is an app's menu or
# tooltip, which belongs to the focused window, as menus do on every other backend.
# workspace.stackingOrder runs bottom to top, so the last window holding a point is the one that
# receives a click there. Each property is read so that a missing or renamed one fails to a known side:
# a flag that is not a boolean counts as false (the window stays listed and on show), a content box
# that is not a rectangle falls back to the frame, and an unknown activity counts as the current one.
# `ccdesk doctor` runs KWIN_CHECK to say when any of them is missing.
WINDOWS_SCRIPT = (
    "function is(v){return v===true;}"
    "var out=[];var ls=workspace.stackingOrder;var act=workspace.currentActivity;"
    "for(var i=0;i<ls.length;i++){var w=ls[i];var f=w.frameGeometry;var c=w.clientGeometry;"
    "if(typeof c==='function'){try{c=c.call(w);}catch(e){c=null;}}"
    "if(!c||typeof c.x!=='number'){c=f;}var ds=[];"
    "try{var dl=w.desktops||[];for(var j=0;j<dl.length;j++){ds.push(dl[j].x11DesktopNumber);}}catch(e){}"
    "var acts=w.activities||[];var t=null;"
    "try{if(w.transientFor){t=String(w.transientFor.internalId);}}catch(e){}"
    "out.push({id:String(w.internalId),title:w.caption,cls:w.resourceClass,pid:w.pid,"
    "x:f.x,y:f.y,width:f.width,height:f.height,cx:c.x,cy:c.y,minimized:is(w.minimized),hidden:is(w.hidden),"
    "active:(w===workspace.activeWindow),desktop:ds.length?ds[0]:null,desktops:ds,"
    "on_activity:(typeof act!=='string'||acts.length===0||acts.indexOf(act)>=0),transient_for:t,"
    "shell:(is(w.desktopWindow)||is(w.dock)||is(w.notification)||is(w.criticalNotification)"
    "||is(w.onScreenDisplay)||is(w.splash)||is(w.appletPopup)),popup:(is(w.tooltip)||is(w.popupWindow))});}"
    "output_result(JSON.stringify(out));"
)
# The KWin properties WINDOWS_SCRIPT relies on, checked on one window: their names, if missing.
KWIN_FLAGS = ("desktopWindow", "dock", "notification", "criticalNotification", "onScreenDisplay", "splash",
              "tooltip", "popupWindow", "appletPopup", "hidden", "minimized")
KWIN_CHECK = (
    "var miss=[];var ls=workspace.stackingOrder;"
    "if(!ls||typeof ls.length!=='number'){miss.push('workspace.stackingOrder');ls=[];}"
    "var w=workspace.activeWindow||ls[0];"
    "if(w){var fl=" + json.dumps(list(KWIN_FLAGS)) + ";"
    "for(var i=0;i<fl.length;i++){if(typeof w[fl[i]]!=='boolean'){miss.push(fl[i]);}}"
    "var c=w.clientGeometry;if(typeof c==='function'){c=c.call(w);}"
    "if(!c||typeof c.x!=='number'){miss.push('clientGeometry');}"
    "if(!Array.isArray(w.activities)&&!(w.activities&&typeof w.activities.length==='number')){miss.push('activities');}"
    "if(!w.desktops||typeof w.desktops.length!=='number'){miss.push('desktops');}}"
    "if(typeof workspace.currentActivity!=='string'){miss.push('workspace.currentActivity');}"
    "output_result(JSON.stringify(miss));"
)
SCREENS_SCRIPT = (
    "var o=[];var s=workspace.screens;"
    "for(var i=0;i<s.length;i++){var g=s[i].geometry;"
    "o.push({name:s[i].name,x:g.x,y:g.y,width:g.width,height:g.height,scale:s[i].devicePixelRatio||1});}"
    "output_result(JSON.stringify(o));"
)
POINTER_TOLERANCE = 2   # pixels: close enough that the click lands on what was aimed at
POINTER_TRIES = 8       # each is one relative step plus a position read (about 40 ms together)
POINTER_FAIL = 8        # further than this and the click would land somewhere else: say so
POINTER_GAIN = 1.6      # ydotool's relative step comes out larger while pointer acceleration is on
POINTER_GAIN_MAX = 4.0
POINTER_BOOST_MAX = 4   # a screen edge in the way: repeat the step up to this much larger
# The ids kdotool prints look like {xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx}. They go into a command
# line, so refuse anything else instead of letting it read as an option.
KDOTOOL_ID = re.compile(r"^\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}$")


def kwin_window(raw) -> Window:
    try:
        return Window(
            id=str(raw["id"]), title=raw.get("title") or "", wm_class=raw.get("cls") or "",
            x=int(raw.get("x") or 0), y=int(raw.get("y") or 0),
            width=int(raw.get("width") or 0), height=int(raw.get("height") or 0),
            desktop=None if raw.get("desktop") is None else int(raw["desktop"]),
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise CommandError(f"unexpected window from kdotool: {raw!r}") from exc


def unlisted(raw) -> bool:
    """A shell surface or an app's popup: in the stacking order, not in list_windows."""
    return bool(isinstance(raw, dict) and (raw.get("shell") or raw.get("popup")))


def plasma_surface(raw: dict, by_id: dict[str, dict]) -> bool:
    """A shell surface, or a menu Plasma itself opened (on a panel, the tray or the desktop): a popup
    from plasmashell, or one whose parents lead to a shell surface. An app's own menu is neither."""
    for _ in range(4):
        if raw.get("shell") or (raw.get("popup") and str(raw.get("cls") or "").lower() == "plasmashell"):
            return True
        raw = by_id.get(str(raw.get("transient_for")))
        if raw is None:
            return False
    return False


def parse_kwin_windows(payload) -> list[Window]:
    if not isinstance(payload, list):
        raise CommandError("kdotool did not return a window list")
    return [kwin_window(raw) for raw in payload if not unlisted(raw)]


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


def kwin_on_show(raw: dict, current_desktop: int | None) -> bool:
    """Whether a window from the KWin script can be seen now. No desktops means all of them."""
    if raw.get("minimized") or raw.get("hidden") or raw.get("on_activity") is False:
        return False
    desktops = raw.get("desktops") or ([raw["desktop"]] if raw.get("desktop") is not None else [])
    numbers = {d for d in desktops if isinstance(d, int)}  # an entry KWin left out counts as unknown
    return not numbers or current_desktop is None or current_desktop in numbers


def kwin_window_at(windows: list[dict], x: int, y: int, current_desktop: int | None = None) -> str | None:
    """Id of the window that would receive a click at (x, y), from the stacking order KWin reports.

    This is inferred, like the Hyprland version: KWin exposes no query for it."""
    for raw in reversed(windows):
        try:
            if not kwin_on_show(raw, current_desktop):
                continue
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
            self._to_logical(path)
            return
        # spectacle has no way to pass a region: grab the screen and cut the part asked for.
        tool = self.first_tool("magick", "convert")
        if not tool:
            raise UnsupportedError("region screenshots on KDE need ImageMagick (magick or convert)")
        fd, tmp = tempfile.mkstemp(prefix="ccdesk-kde-", suffix=".png")
        os.close(fd)
        try:
            self._spectacle(tmp)
            self._to_logical(tmp)
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

    def _to_logical(self, path: str) -> None:
        """Bring a grab to the logical pixels windows and the pointer use, what grim -s 1 does. A scaled
        monitor comes out of spectacle bigger than KWin says it is. Without kdotool there is no way to
        know, and the image is the only size there is."""
        monitors = self.monitors()
        if not monitors:
            return
        # ccdesk's screen starts at 0,0 (screen_size). A monitor left of or above that widens the grab,
        # so the part of it past 0,0 is cut off afterwards and pixels stay screen coordinates.
        ox, oy = min(0, *(m["x"] for m in monitors)), min(0, *(m["y"] for m in monitors))
        screen = (max(m["x"] + m["width"] for m in monitors), max(m["y"] + m["height"] for m in monitors))
        size = (screen[0] - ox, screen[1] - oy)
        width, height = png_size(path)
        if (width, height) != size:
            self._rescale(path, (width, height), size)
        if ox or oy:
            self.run([self.need("screenshots past 0,0 on KDE", "magick", "convert"), path, "-crop",
                      f"{screen[0]}x{screen[1]}+{-ox}+{-oy}", "+repage", f"PNG24:{path}"], timeout=30)

    def _rescale(self, path: str, got: tuple[int, int], size: tuple[int, int]) -> None:
        width, height = got
        # KWin draws every output at one scale, so a good grab is the layout times one factor. Two
        # factors (outputs at their own scales, side by side) or a grab smaller than the layout can't
        # be mapped back by one resize: say so rather than hand back wrong coordinates.
        sx, sy = width / size[0], height / size[1]
        if sx < 1 or sy < 1 or abs(sx - sy) > 0.02 * max(sx, sy):
            raise CommandError(f"spectacle's {width}x{height} grab does not match KWin's {size[0]}x{size[1]} "
                               "layout at one scale (monitors at different scales?): coordinates would be wrong")
        if not self.first_tool("magick", "convert"):
            raise UnsupportedError("screenshots on a scaled KDE display need ImageMagick (magick or convert)")
        self.scale_png(path, *size)

    def kwin_missing(self) -> list[str] | None:
        """KWin properties the window script needs and this KWin lacks; None without kdotool. Raises
        CommandError when kdotool is there but cannot run a script (older than 0.3, or KWin refused)."""
        if not self.which("kdotool"):
            return None
        missing = self._kwin_json(KWIN_CHECK)
        if not (isinstance(missing, list) and all(isinstance(m, str) for m in missing)):
            raise CommandError(f"kdotool's KWin check answered {str(missing)[:120]!r}")
        return missing

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

    def pointer_position(self) -> tuple[int, int] | None:
        """Where KWin has the pointer, or None when kdotool cannot say. One client call, ~5 ms."""
        if not self.which("kdotool"):
            return None
        out = self.try_run(["kdotool", "getmouselocation"]) or ""
        x, y = re.search(r"(?:^|\s)x:(-?\d+)", out), re.search(r"(?:^|\s)y:(-?\d+)", out)
        return (int(x.group(1)), int(y.group(1))) if x and y else None

    @staticmethod
    def _pointer_step(delta: int, gain: float) -> int:
        """How far to move the pointer so a gain of `gain` leaves `delta` of it."""
        if not delta:
            return 0
        return max(1, round(abs(delta) / max(1.0, gain))) * (1 if delta > 0 else -1)

    @staticmethod
    def _pointer_gain(step: int, moved: int, gain: float) -> float:
        """Learn this machine's acceleration from what the last step actually did."""
        if not step or not moved:
            return gain
        return min(POINTER_GAIN_MAX, max(1.0, abs(moved / step)))

    def _settle(self, x: int, y: int) -> tuple[int, int] | None:
        """Drive the pointer onto (x, y), correcting against what the compositor reports."""
        here = self.pointer_position()
        if here is None:
            return None
        gain_x = gain_y = POINTER_GAIN
        boost = 1  # KDE's edge barrier between monitors swallows small steps: push harder
        for _ in range(POINTER_TRIES):
            dx, dy = x - here[0], y - here[1]
            if abs(dx) <= POINTER_TOLERANCE and abs(dy) <= POINTER_TOLERANCE:
                return here
            step_x = self._pointer_step(dx, gain_x) * boost
            step_y = self._pointer_step(dy, gain_y) * boost
            self._ydotool("mousemove", "-x", str(step_x), "-y", str(step_y))
            self.sleep(POINTER_SETTLE)
            now = self.pointer_position()
            if now is None:
                return None
            moved = (now[0] - here[0], now[1] - here[1])
            if moved == (0, 0):
                boost = min(POINTER_BOOST_MAX, boost * 2)
            else:
                boost = 1
                gain_x = self._pointer_gain(step_x, moved[0], gain_x)
                gain_y = self._pointer_gain(step_y, moved[1], gain_y)
            here = now
        return here

    def move(self, x: int, y: int) -> None:
        """Put the pointer on (x, y).

        ydotool's --absolute lands in a corner under KWin (every value clamps to the minimum of
        the axis), so the pointer is driven with relative steps and corrected against the position
        KWin reports. Relative steps come out larger while pointer acceleration is on, which is why
        the gain is measured rather than assumed."""
        self.need("pointer input on KDE", "ydotool")
        here = self._settle(x, y)
        if here is None:  # no kdotool: nothing to measure, so take the one absolute shot
            self._ydotool("mousemove", "--absolute", "-x", str(x), "-y", str(y))
            return
        if max(abs(x - here[0]), abs(y - here[1])) > POINTER_FAIL:
            raise CommandError(f"could not place the pointer on {x},{y}: KWin reports it at "
                               f"{here[0]},{here[1]}; the click would land somewhere else")
        self.sleep(POINTER_SETTLE)  # let the last motion event arrive before the button

    def drag(self, x0: int, y0: int, x1: int, y1: int, duration_ms: int) -> None:
        """Drag with relative steps, then correct the end point with the button still down."""
        self.move(x0, y0)
        steps = self.drag_steps(duration_ms)
        pause = duration_ms / 1000 / steps
        here = self.pointer_position() or (x0, y0)
        self._press(1, True)
        try:
            for x, y in interpolate(x0, y0, x1, y1, steps):
                dx, dy = x - here[0], y - here[1]
                if dx or dy:
                    self._ydotool("mousemove", "-x", str(self._pointer_step(dx, POINTER_GAIN)),
                                  "-y", str(self._pointer_step(dy, POINTER_GAIN)))
                here = (x, y)
                self.sleep(pause)
            self._settle(x1, y1)
        finally:
            self._press(1, False)

    def type_text(self, text: str) -> None:
        # Not wtype: Plasma has no virtual-keyboard protocol, so wtype would fail on every call.
        self.need("keyboard input on KDE", "ydotool")
        self.run(["ydotool", "type", "--key-delay", "12", "--file", "-"], input=text,
                 timeout=max(15, len(text) * 0.05 + 5))

    def typing_warning(self, text: str) -> str | None:
        if text.isascii():
            return None
        return ("ydotool only types ASCII: other characters may be lost or wrong. Check the field. If wrong: "
                "clipboard(action=\"set\", text=...) and ctrl+v")

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

    def window_origin(self, pid: int | None, title: str | None) -> tuple[int, int] | None:
        found = [w for w in self._safe_windows() if not unlisted(w) and (pid is None or w.get("pid") == pid)]
        if title is not None and len(found) > 1:
            found = [w for w in found if w.get("title") == title] or found
        if not found:
            return None
        return int(found[0].get("cx", found[0].get("x", 0))), int(found[0].get("cy", found[0].get("y", 0)))

    def transient_for(self, window_id: str) -> str | None:
        raw = next((w for w in self._safe_windows() if str(w.get("id")) == window_id), None)
        return raw.get("transient_for") if raw else None

    def window_on_show(self, window_id: str) -> bool | None:
        raw = next((w for w in self._safe_windows() if str(w.get("id")) == window_id), None)
        if raw is None:
            return None
        return kwin_on_show(raw, None if raw.get("minimized") else self.current_desktop())

    def active_window(self) -> str | None:
        return next((str(w.get("id")) for w in self._safe_windows() if w.get("active")), None)

    def window_at(self, x: int, y: int) -> str | None:
        return kwin_window_at(self._safe_windows(), x, y, self.current_desktop())

    def shell_window(self, window_id: str) -> Window | None:
        by_id = {str(w.get("id")): w for w in self._safe_windows() if isinstance(w, dict)}
        raw = by_id.get(window_id)
        if raw is None or not plasma_surface(raw, by_id):
            return None
        window = kwin_window(raw)
        return window if window.title else replace(window, title=f"a Plasma {raw.get('cls') or 'shell'} surface")

    def current_desktop(self) -> int | None:
        if not self.which("kdotool"):
            return None
        try:
            return int((self.run(["kdotool", "get_desktop"]) or "").strip())
        except (CcdeskError, ValueError):
            return None
