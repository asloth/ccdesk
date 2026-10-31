"""Wayland backend: grim for pixels, sway's seat, a virtual pointer or ydotool for the mouse,
wtype, Hyprland or ydotool for the keyboard, wl-clipboard, sway/Hyprland IPC for windows.

Wayland deliberately hides other clients from each other, so coverage depends on the compositor:
grim needs wlr-screencopy (sway, Hyprland, river, ...). On sway the pointer goes through
`swaymsg seat - cursor`, which needs no root; elsewhere a virtual pointer (see vpointer.py) does
the same, and ydotool, which needs /dev/uinput and its daemon, is the last resort. Window listing
only works through compositor IPC (swaymsg, hyprctl) or, for XWayland apps only, through xdotool.

Hyprland's `hyprctl dispatch` takes Lua since it moved to a Lua config: tested on 0.56.2.
"""

from __future__ import annotations

import json
import os
import re
import tempfile

from ccdesk.backends.base import Backend, interpolate
from ccdesk.backends.vpointer import NoVirtualPointer, VirtualPointer, VirtualPointerError
from ccdesk.backends.x11 import X11Backend
from ccdesk.errors import CommandError, UnsupportedError
from ccdesk.parsing import KeyCombo, Region, Window, png_size

# ydotool 1.x click codes: 0x40 = down, 0x80 = up, 0xC0 = down+up; low nibble = button.
YDOTOOL_BUTTON = {1: 0x00, 2: 0x02, 3: 0x01}
WTYPE_MODIFIER = {"ctrl": "ctrl", "alt": "alt", "shift": "shift", "super": "logo"}
# X11-style button numbers that sway's `cursor press` understands; 4-7 are the wheel.
WHEEL = {"up": 4, "down": 5, "left": 6, "right": 7}
HYPR_MODIFIER = {"ctrl": "CTRL", "alt": "ALT", "shift": "SHIFT", "super": "SUPER"}
HYPR_MINIMIZED = "special:ccdesk"  # Hyprland has no minimized state: a hidden workspace stands in for it
POINTER_SETTLE = 0.03  # between arriving somewhere and pressing, so the app has seen the motion
# wtype uploads its own keymap first; apps that have not applied it yet drop the first key.
WTYPE_SETTLE = ["-s", "60"]


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


def sway_content_origin(tree: dict, pid: int | None, title: str | None) -> tuple[int, int] | None:
    """Screen position of a window's content (inside border and title bar), found by pid and title.

    GTK and Qt on Wayland report accessible boxes relative to their own window; adding this
    turns them into screen coordinates."""
    found = []

    def walk(node: dict) -> None:
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            if child.get("pid") and (pid is None or child["pid"] == pid):
                found.append(child)
            walk(child)

    walk(tree)
    if title is not None and len(found) > 1:
        found = [c for c in found if c.get("name") == title] or found
    if not found:
        return None
    rect, inner = found[0].get("rect", {}), found[0].get("window_rect", {})
    return rect.get("x", 0) + inner.get("x", 0), rect.get("y", 0) + inner.get("y", 0)


def sway_window_place(tree: dict, window_id: str) -> tuple[bool, bool] | None:
    """(visible, in the scratchpad) for a window in the tree, or None if it is not there."""
    def walk(node: dict, workspace: str | None):
        if node.get("type") == "workspace":
            workspace = node.get("name")
        if str(node.get("id")) == window_id:
            return bool(node.get("visible")), workspace == "__i3_scratch"
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            found = walk(child, workspace)
            if found:
                return found
        return None

    return walk(tree, None)


def parse_sway_outputs(outputs: list) -> list[dict]:
    return [{"name": o.get("name", ""), **{k: o["rect"][k] for k in ("x", "y", "width", "height")},
             "scale": o.get("scale", 1), "focused": bool(o.get("focused"))}
            for o in outputs if o.get("active") and o.get("rect")]


def parse_hyprctl_monitors(monitors: list) -> list[dict]:
    """hyprctl gives the mode in physical pixels; windows and the pointer use logical ones."""
    out = []
    for m in monitors:
        scale = m.get("scale") or 1
        width, height = round(m.get("width", 0) / scale), round(m.get("height", 0) / scale)
        if m.get("transform", 0) % 2:  # rotated 90 or 270 degrees
            width, height = height, width
        out.append({"name": m.get("name", ""), "x": m.get("x", 0), "y": m.get("y", 0), "width": width,
                    "height": height, "scale": scale, "focused": bool(m.get("focused"))})
    return out


def hyprland_window_at(clients: list, monitors: list, x: int, y: int) -> str | None:
    """Address of the window on top at (x, y). Hyprland does not report the stacking order, so
    this goes by what stacks higher: special workspace, fullscreen, floating, then recent focus."""
    shown = {(m.get(k) or {}).get("id") for m in monitors for k in ("activeWorkspace", "specialWorkspace")}
    under = [c for c in clients if (c.get("workspace") or {}).get("id") in shown and not c.get("hidden")
             and c["at"][0] <= x < c["at"][0] + c["size"][0] and c["at"][1] <= y < c["at"][1] + c["size"][1]]
    under.sort(key=lambda c: (c["workspace"]["id"] >= 0, not c.get("fullscreen"), not c.get("floating"),
                              c.get("focusHistoryID", 0)))
    return under[0].get("address") if under else None


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
        "pointer": ("swaymsg", "hyprctl", "ydotool"),
        "keyboard": ("wtype", "ydotool"),
        "windows": ("swaymsg", "hyprctl", "xdotool"),
        "clipboard": ("wl-copy", "wl-paste"),
    }

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._floated: set[str] = set()  # stableIds of Hyprland windows ccdesk made float, for restore
        self._no_vpointer = False  # the compositor does not offer the protocol: stop asking
        self._layout: tuple[int, int] | None = None

    def available(self) -> str | None:
        if not self.env.get("WAYLAND_DISPLAY"):
            return "WAYLAND_DISPLAY is not set; no Wayland session to control"
        return None

    def _xwayland(self) -> X11Backend | None:
        """XWayland fallback for window listing; only sees X11 clients."""
        if self.env.get("DISPLAY") and self.which("xdotool"):
            return X11Backend(env=self.env, which=self._which, runner=self._runner)
        return None

    def _sway(self) -> bool:
        return bool(self.which("swaymsg") and self.env.get("SWAYSOCK"))

    def _swaymsg(self, *args: str) -> str:
        return self.run(["swaymsg", *args])

    def _seat(self, command: str) -> None:
        self._swaymsg(f"seat - cursor {command}")

    def screen_size(self) -> tuple[int, int]:
        if self._sway():
            outputs = [o["rect"] for o in json.loads(self._swaymsg("-t", "get_outputs", "-r"))
                       if o.get("active")]
            if outputs:
                return (max(r["x"] + r["width"] for r in outputs), max(r["y"] + r["height"] for r in outputs))
        if self._hyprland():
            monitors = self.monitors()
            if monitors:
                return (max(m["x"] + m["width"] for m in monitors), max(m["y"] + m["height"] for m in monitors))
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
            # -s 1: logical pixels, the space windows and the pointer use. By default a monitor
            # scaled by 1.5 next to one at 1 makes every coordinate in the image wrong.
            argv = ["grim", "-s", "1"]
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

    def _pointer(self) -> VirtualPointer | None:
        """A virtual pointer, or None where sway's seat or ydotool has to do it."""
        if self._sway() or self._no_vpointer:
            return None
        try:
            return VirtualPointer(self.env)
        except NoVirtualPointer:
            self._no_vpointer = True
            return None
        except VirtualPointerError:
            return None

    def _extent(self) -> tuple[int, int]:
        """The layout's size for a virtual pointer. Without IPC that takes a full grab, so keep it."""
        if self._hyprland():
            return self.screen_size()
        if self._layout is None:
            self._layout = self.screen_size()
        return self._layout

    def move(self, x: int, y: int) -> None:
        if self._sway():
            self._seat(f"set {x} {y}")
        elif (pointer := self._pointer()) is not None:
            with pointer:
                pointer.move(x, y, *self._extent())
        else:
            self._ydotool("mousemove", "--absolute", "-x", str(x), "-y", str(y))

    def _press(self, button: int, down: bool) -> None:
        if self._sway():
            self._seat(f"{'press' if down else 'release'} button{button}")
        else:
            self._ydotool("click", hex((0x40 if down else 0x80) | YDOTOOL_BUTTON[button]))

    def click(self, x: int, y: int, button: int, count: int) -> None:
        if (pointer := self._pointer()) is not None:
            with pointer:
                pointer.move(x, y, *self._extent())
                self.sleep(POINTER_SETTLE)
                for i in range(count):
                    if i:
                        self.sleep(0.08)
                    pointer.button(button, True)
                    pointer.button(button, False)
            return
        self.move(x, y)
        if self._sway():
            for i in range(count):
                if i:
                    self.sleep(0.08)
                self._press(button, True)
                self._press(button, False)
            return
        code = 0xC0 | YDOTOOL_BUTTON[button]
        self._ydotool("click", "--repeat", str(count), "--next-delay", "80", hex(code))

    def drag(self, x0: int, y0: int, x1: int, y1: int, duration_ms: int) -> None:
        steps = self.drag_steps(duration_ms)
        pause = duration_ms / 1000 / steps
        if (pointer := self._pointer()) is not None:
            size = self._extent()
            with pointer:  # one connection: closing it would let go of the button
                pointer.move(x0, y0, *size)
                self.sleep(POINTER_SETTLE)
                pointer.button(1, True)
                try:
                    for x, y in interpolate(x0, y0, x1, y1, steps):
                        self.sleep(pause)
                        pointer.move(x, y, *size)
                finally:
                    pointer.button(1, False)
            return
        self.move(x0, y0)
        self._press(1, True)
        try:
            for x, y in interpolate(x0, y0, x1, y1, steps):
                self.sleep(pause)
                self.move(x, y)
        finally:
            self._press(1, False)

    def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        if (pointer := self._pointer()) is not None:
            with pointer:
                pointer.move(x, y, *self._extent())
                self.sleep(POINTER_SETTLE)
                pointer.scroll(dx, dy)
            return
        self.move(x, y)
        if self._sway():
            for amount, neg, pos in ((dy, "up", "down"), (dx, "left", "right")):
                for _ in range(abs(amount)):
                    self._seat(f"press button{WHEEL[pos if amount > 0 else neg]}")
                    self._seat(f"release button{WHEEL[pos if amount > 0 else neg]}")
            return
        # ydotool wheel: positive y scrolls up, so invert to keep "dy > 0 means down".
        self._ydotool("mousemove", "--wheel", "-x", str(dx), "-y", str(-dy))

    def type_text(self, text: str) -> None:
        if self.which("wtype"):
            self.run(["wtype", *WTYPE_SETTLE, "-d", "12", "--", text], timeout=max(15, len(text) * 0.05 + 5))
        else:
            self._ydotool("type", "--key-delay", "12", "--", text)

    def press_key(self, combo: KeyCombo) -> None:
        if self._hyprland():  # a named key from wtype on its own never reaches GTK 3
            mods = " ".join(HYPR_MODIFIER[m] for m in combo.modifiers)
            # json.dumps quotes the way Lua does: a key may be any one character, " and \\ included.
            self._dispatch(f'hl.dsp.send_shortcut({{ mods = "{mods}", key = {json.dumps(combo.key)} }})')
            return
        if not self.which("wtype"):
            raise UnsupportedError("press_key on Wayland needs wtype (ydotool only speaks raw keycodes)")
        argv = ["wtype", *WTYPE_SETTLE]
        for m in combo.modifiers:
            argv += ["-M", WTYPE_MODIFIER[m]]
        argv += ["-k", combo.key]
        for m in reversed(combo.modifiers):
            argv += ["-m", WTYPE_MODIFIER[m]]
        self.run(argv)

    def window_origin(self, pid: int | None, title: str | None) -> tuple[int, int] | None:
        if self._sway():
            return sway_content_origin(json.loads(self._swaymsg("-t", "get_tree", "-r")), pid, title)
        if self._hyprland():
            found = [c for c in self._hypr_clients() if pid is None or c.get("pid") == pid]
            if title is not None and len(found) > 1:
                found = [c for c in found if c.get("title") == title] or found
            return tuple(found[0]["at"]) if found else None
        return None

    def list_windows(self) -> list[Window]:
        if self._sway():
            return parse_sway_tree(json.loads(self.run(["swaymsg", "-t", "get_tree", "-r"])))
        if self._hyprland():
            return parse_hyprctl_clients(json.loads(self.run(["hyprctl", "clients", "-j"])))
        x = self._xwayland()
        if x:
            return x.list_windows()
        raise UnsupportedError(
            "this Wayland compositor does not expose a window list; use screenshot() and click instead"
        )

    def activate_window(self, window: Window) -> None:
        if self._sway():
            self.run(["swaymsg", f"[con_id={window.id}]", "focus"])
        elif self._hyprland():
            target = self._hypr_address(window)
            try:
                self._dispatch(f'hl.dsp.focus({{ window = "{target}" }})')
            except CommandError:  # before Lua
                self.run(["hyprctl", "dispatch", "focuswindow", f"address:{window.id}"])
        elif (x := self._xwayland()) is not None:
            x.activate_window(window)
        else:
            raise UnsupportedError("this Wayland compositor does not allow activating windows from outside")

    def monitors(self) -> list[dict]:
        if self._sway():
            return parse_sway_outputs(json.loads(self.try_run(["swaymsg", "-t", "get_outputs", "-r"]) or "[]"))
        if self._hyprland():
            return parse_hyprctl_monitors(json.loads(self.try_run(["hyprctl", "-j", "monitors"]) or "[]"))
        return []

    def _sway_window(self, what: str, window: Window, command: str) -> None:
        if not self._sway():
            raise UnsupportedError(f"{what} windows on Wayland is only implemented on sway and Hyprland; this "
                                   f"compositor has no portable way to do it from outside")
        self.run(["swaymsg", f"[con_id={window.id}]", command])

    def _dispatch(self, lua: str) -> None:
        out = self.run(["hyprctl", "dispatch", lua]).strip()
        if out != "ok":
            raise CommandError(f"hyprctl dispatch failed: {out or 'no output'}")

    @staticmethod
    def _hypr_address(window: Window) -> str:
        if not re.fullmatch(r"0x[0-9a-fA-F]+", window.id):  # it goes into Lua source
            raise CommandError(f"{window.id!r} is not a Hyprland window address")
        return f"address:{window.id}"

    def _hypr_clients(self) -> list:
        return json.loads(self.try_run(["hyprctl", "clients", "-j"]) or "[]")

    def _hypr_client(self, window: Window) -> dict:
        return next((c for c in self._hypr_clients() if c.get("address") == window.id), {})

    def _hypr_float(self, window: Window) -> str:
        """Geometry requests need a floating window; tiled ones are placed by the layout."""
        target = self._hypr_address(window)
        client = self._hypr_client(window)
        if not client.get("floating"):
            self._dispatch(f'hl.dsp.window.float({{ window = "{target}", action = "enable" }})')
            # Not the address: Hyprland can give a closed window's one to a new window.
            self._floated.add(client.get("stableId", window.id))
        return target

    def _hypr_fullscreen(self, window: Window, action: str, state: int) -> None:
        mode = ', mode = "maximized"' if state == 1 else ""
        self._dispatch(f'hl.dsp.window.fullscreen({{ window = "{self._hypr_address(window)}", '
                       f'action = "{action}"{mode} }})')

    def _hypr_restore(self, window: Window) -> None:
        target = self._hypr_address(window)
        client = self._hypr_client(window)
        if client.get("fullscreen"):
            self._hypr_fullscreen(window, "unset", client["fullscreen"])
        if (client.get("workspace") or {}).get("name") == HYPR_MINIMIZED:
            current = self.current_desktop()
            if current is None:
                raise CommandError("hyprctl did not say which workspace is on show")
            self._dispatch(f'hl.dsp.window.move({{ window = "{target}", workspace = "{current}", follow = false }})')
        key = client.get("stableId", window.id)
        if key in self._floated:
            self._floated.discard(key)
            self._dispatch(f'hl.dsp.window.float({{ window = "{target}", action = "disable" }})')
        self._dispatch(f'hl.dsp.focus({{ window = "{target}" }})')

    def active_window(self) -> str | None:
        if not self._hyprland():
            return None
        out = json.loads(self.try_run(["hyprctl", "-j", "activewindow"]) or "{}")
        return out.get("address") or None

    def window_at(self, x: int, y: int) -> str | None:
        if not self._hyprland():
            return None
        return hyprland_window_at(self._hypr_clients(), json.loads(self.try_run(["hyprctl", "-j", "monitors"]) or "[]"),
                                  x, y)

    def transient_for(self, window_id: str) -> str | None:
        """Hyprland does not say which window a dialog belongs to. A floating window of an app
        with another window on the same workspace is taken to be that window's dialog."""
        if not self._hyprland():
            return None
        clients = self._hypr_clients()
        me = next((c for c in clients if c.get("address") == window_id), None)
        if not me or not me.get("floating"):
            return None
        same = [c for c in clients if c.get("pid") == me.get("pid") and c.get("address") != window_id
                and c.get("workspace") == me.get("workspace")]
        same.sort(key=lambda c: c.get("focusHistoryID", 0))
        return same[0].get("address") if same else None

    def current_desktop(self) -> int | None:
        if not self._hyprland():
            return None
        return json.loads(self.try_run(["hyprctl", "-j", "activeworkspace"]) or "{}").get("id")

    def move_window(self, window: Window, x: int, y: int) -> None:
        if self._hyprland():
            target = self._hypr_float(window)
            self._dispatch(f'hl.dsp.window.move({{ window = "{target}", x = {x}, y = {y} }})')
        else:
            self._sway_window("moving", window, f"floating enable, move absolute position {x} {y}")

    def resize_window(self, window: Window, width: int, height: int) -> None:
        if self._hyprland():
            target = self._hypr_float(window)
            self._dispatch(f'hl.dsp.window.resize({{ window = "{target}", x = {width}, y = {height} }})')
        else:
            self._sway_window("resizing", window, f"floating enable, resize set {width} {height}")

    def minimize_window(self, window: Window) -> None:
        if self._hyprland():
            target = self._hypr_address(window)
            self._dispatch(f'hl.dsp.window.move({{ window = "{target}", workspace = "{HYPR_MINIMIZED}", '
                           f'follow = false }})')
        else:
            self._sway_window("minimizing", window, "move scratchpad")

    def maximize_window(self, window: Window) -> None:
        if self._hyprland():
            self._hypr_fullscreen(window, "set", 1)
        else:
            self._sway_window("maximizing", window, "fullscreen enable")

    def restore_window(self, window: Window) -> None:
        if self._hyprland():
            self._hypr_restore(window)
            return
        # Only a window in the scratchpad needs bringing back: others stay on their own workspace.
        tree = self.try_run(["swaymsg", "-t", "get_tree", "-r"]) if self._sway() else None
        place = sway_window_place(json.loads(tree or "{}"), window.id)
        back = "scratchpad show, floating disable, " if place and place[1] else ""
        self._sway_window("restoring", window, f"{back}fullscreen disable, focus")

    def _hyprland(self) -> bool:
        return bool(self.which("hyprctl") and self.env.get("HYPRLAND_INSTANCE_SIGNATURE"))

    def window_on_show(self, window_id: str) -> bool | None:
        if self._sway():
            place = sway_window_place(json.loads(self.try_run(["swaymsg", "-t", "get_tree", "-r"]) or "{}"), window_id)
            return place[0] if place else None
        if self._hyprland():
            clients = json.loads(self.try_run(["hyprctl", "clients", "-j"]) or "[]")
            monitors = json.loads(self.try_run(["hyprctl", "-j", "monitors"]) or "[]")
            client = next((c for c in clients if c.get("address") == window_id), None)
            if client is None or not monitors:
                return None
            # A monitor shows its workspace and, on top when toggled on, a special one.
            on_show = {(m.get(k) or {}).get("id") for m in monitors for k in ("activeWorkspace", "specialWorkspace")}
            return (client.get("workspace") or {}).get("id") in on_show
        return None

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
