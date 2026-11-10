"""Window questions answered over one persistent Xlib connection, instead of one subprocess each.

Every guarded input action asks which windows exist, which one is focused, which one is under the
point, whether a window is a dialog of another: xdotool, xprop, xwininfo, wmctrl and xprintidle
each cost a spawn (4 to 7 ms), and the window list costs 3 more per window. Here they are a few
round trips on a socket that stays open. The pointer is never moved to ask what is under it.

libX11 (and libXss for the idle time) through ctypes, stdlib only. Every call declares argtypes
and restype: a default int return would truncate 64-bit pointers. One Display per process and one
lock around it, because tool calls run on worker threads.

Every method either answers or raises XQueryUnavailable; the caller then runs the subprocess
path it had before. After the first unexpected failure the connection is marked broken and stays
that way.
"""

from __future__ import annotations

import ctypes as C
import ctypes.util
import threading

from ccdesk.backends import xerror
from ccdesk.cursor import TITLE_PREFIX

ANY_PROPERTY_TYPE = 0
IS_VIEWABLE = 2
INPUT_OUTPUT = 1
SUCCESS = 0
UNSET = 0xFFFFFFFF  # _NET_WM_DESKTOP of a window shown on every desktop


class XQueryUnavailable(Exception):
    """This question cannot be answered over the connection; use the subprocess path."""


class _Attrs(C.Structure):
    _fields_ = [("x", C.c_int), ("y", C.c_int), ("width", C.c_int), ("height", C.c_int),
                ("border_width", C.c_int), ("depth", C.c_int), ("visual", C.c_void_p),
                ("root", C.c_ulong), ("cls", C.c_int), ("bit_gravity", C.c_int),
                ("win_gravity", C.c_int), ("backing_store", C.c_int), ("backing_planes", C.c_ulong),
                ("backing_pixel", C.c_ulong), ("save_under", C.c_int), ("colormap", C.c_ulong),
                ("map_installed", C.c_int), ("map_state", C.c_int), ("all_event_masks", C.c_long),
                ("your_event_mask", C.c_long), ("do_not_propagate_mask", C.c_long),
                ("override_redirect", C.c_int), ("screen", C.c_void_p)]


class _SaverInfo(C.Structure):
    _fields_ = [("window", C.c_ulong), ("state", C.c_int), ("kind", C.c_int),
                ("til_or_since", C.c_ulong), ("idle", C.c_ulong), ("event_mask", C.c_ulong)]


def _text(kind: str, raw: bytes) -> str:
    """A window name as the property's own type says: UTF8_STRING is UTF-8, STRING is Latin-1."""
    if kind == "UTF8_STRING":
        return raw.decode("utf-8", "replace")
    return raw.decode("latin-1")


class XQuery:
    def __init__(self, display: str):
        self.display = display
        self.lock = threading.RLock()
        self.broken: str | None = None
        self._x = None
        self._xss = None
        self._xss_tried = False
        self._dpy = None
        self._root = 0
        self._error: str | None = None
        self._atoms: dict[str, int] = {}
        self._names: dict[int, str] = {}  # atom -> name, for the type of a property we read

    # -- connection -----------------------------------------------------------

    def _open(self) -> None:
        x = C.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        v, i, u, ul, ci, l = C.c_void_p, C.c_int, C.c_uint, C.c_ulong, C.c_char_p, C.c_long
        pul, pi, pu = C.POINTER(ul), C.POINTER(i), C.POINTER(u)

        def sig(fn, restype, *argtypes):
            fn.restype, fn.argtypes = restype, list(argtypes)

        sig(x.XOpenDisplay, v, ci)
        sig(x.XDefaultRootWindow, ul, v)
        sig(x.XDefaultScreen, i, v)
        sig(x.XDisplayWidth, i, v, i)
        sig(x.XDisplayHeight, i, v, i)
        sig(x.XInternAtom, ul, v, ci, i)
        sig(x.XGetAtomName, v, v, ul)
        sig(x.XGetWindowProperty, i, v, ul, ul, l, l, i, ul, pul, pi, pul, pul, C.POINTER(v))
        sig(x.XGetWindowAttributes, i, v, ul, C.POINTER(_Attrs))
        sig(x.XGetGeometry, i, v, ul, pul, pi, pi, pu, pu, pu, pu)
        sig(x.XTranslateCoordinates, i, v, ul, ul, i, i, pi, pi, pul)
        sig(x.XQueryTree, i, v, ul, pul, pul, C.POINTER(C.POINTER(ul)), pu)
        sig(x.XFree, i, v)
        sig(x.XSync, i, v, i)
        sig(x.XSetErrorHandler, v, v)
        dpy = x.XOpenDisplay(self.display.encode())
        if not dpy:
            raise OSError(f"cannot open display {self.display}")
        xerror.register(x, dpy, self._on_error)
        self._x, self._dpy = x, dpy
        self._root = x.XDefaultRootWindow(dpy)

    def _on_error(self, message: str) -> None:
        self._error = message

    def _ready(self) -> None:
        if self.broken:
            raise XQueryUnavailable(self.broken)
        if self._dpy is None:
            try:
                self._open()
            except Exception as exc:
                self.broken = f"{type(exc).__name__}: {exc}"
                raise XQueryUnavailable(self.broken) from None

    def _call(self, fn, *args):
        """Run fn(*args) under the lock, with the connection open; anything unexpected breaks it."""
        with self.lock:
            self._ready()
            try:
                self._error = None
                return fn(*args)
            except XQueryUnavailable:
                raise
            except Exception as exc:  # ctypes.ArgumentError, a short buffer: stop trusting it
                self.broken = f"{type(exc).__name__}: {exc}"
                raise XQueryUnavailable(self.broken) from None

    # -- primitives (the lock is held) ------------------------------------------

    def _atom(self, name: str) -> int:
        atom = self._atoms.get(name)
        if atom is None:
            atom = self._atoms[name] = self._x.XInternAtom(self._dpy, name.encode(), 0)
            self._names[atom] = name
        return atom

    def _atom_name(self, atom: int) -> str:
        name = self._names.get(atom)
        if name is None:
            p = self._x.XGetAtomName(self._dpy, atom)
            name = self._names[atom] = C.string_at(p).decode("latin-1") if p else ""
            if p:
                self._x.XFree(p)
        return name

    def _prop(self, win: int, name: str, req: int | str = ANY_PROPERTY_TYPE):
        """(type name, items) of a property, or None when it is not set or the window is gone.
        items is bytes for 8-bit data, a list of ints for 16 and 32-bit data."""
        x = self._x
        typ, fmt, n, after, data = C.c_ulong(), C.c_int(), C.c_ulong(), C.c_ulong(), C.c_void_p()
        req_atom = self._atom(req) if isinstance(req, str) else req
        self._error = None
        status = x.XGetWindowProperty(self._dpy, win, self._atom(name), 0, 1 << 16, 0, req_atom,
                                      C.byref(typ), C.byref(fmt), C.byref(n), C.byref(after), C.byref(data))
        if status != SUCCESS or self._error:
            if data.value:
                x.XFree(data)
            return None
        try:
            if not typ.value or not data.value:
                return None
            count = n.value
            if fmt.value == 8:
                items = C.string_at(data.value, count)
            elif fmt.value == 16:
                items = list(C.cast(data, C.POINTER(C.c_ushort))[:count])
            else:  # 32-bit items arrive as C longs
                items = list(C.cast(data, C.POINTER(C.c_ulong))[:count])
            return self._atom_name(typ.value), items
        finally:
            x.XFree(data)

    def _cardinals(self, win: int, name: str):
        got = self._prop(win, name, "CARDINAL")
        return got[1] if got and got[0] == "CARDINAL" and isinstance(got[1], list) else None

    def _windows_prop(self, win: int, name: str):
        got = self._prop(win, name, "WINDOW")
        return got[1] if got and got[0] == "WINDOW" and isinstance(got[1], list) else None

    def _attrs(self, win: int) -> _Attrs | None:
        a = _Attrs()
        self._error = None
        if not self._x.XGetWindowAttributes(self._dpy, win, C.byref(a)) or self._error:
            return None
        return a

    def _children(self, win: int) -> list[int] | None:
        """Children of win, bottom to top."""
        root, parent, kids, n = C.c_ulong(), C.c_ulong(), C.POINTER(C.c_ulong)(), C.c_uint()
        self._error = None
        if not self._x.XQueryTree(self._dpy, win, C.byref(root), C.byref(parent), C.byref(kids), C.byref(n)) \
                or self._error:
            return None
        try:
            return [kids[i] for i in range(n.value)]
        finally:
            if kids:
                self._x.XFree(kids)

    def _title(self, win: int) -> str:
        got = self._prop(win, "_NET_WM_NAME", "UTF8_STRING")
        if got and got[0] == "UTF8_STRING" and got[1]:
            return _text(got[0], got[1])
        got = self._prop(win, "WM_NAME")
        if got and isinstance(got[1], bytes):
            return _text(got[0], got[1])
        return ""

    def _wm_class(self, win: int) -> str | None:
        """instance.class, the form wmctrl prints; None when the window has no WM_CLASS."""
        got = self._prop(win, "WM_CLASS", "STRING")
        if not got or not isinstance(got[1], bytes):
            return None
        parts = got[1].decode("latin-1").split("\0")
        if parts and parts[-1] == "":
            parts.pop()
        return ".".join(parts[:2])

    def _box(self, win: int) -> tuple[int, int, int, int] | None:
        """x, y, width, height of the window's inside in root coordinates."""
        x = self._x
        root, gx, gy, w, h, bw, depth = C.c_ulong(), C.c_int(), C.c_int(), C.c_uint(), C.c_uint(), C.c_uint(), C.c_uint()
        self._error = None
        if not x.XGetGeometry(self._dpy, win, C.byref(root), C.byref(gx), C.byref(gy), C.byref(w), C.byref(h),
                              C.byref(bw), C.byref(depth)) or self._error:
            return None
        ox, oy, child = C.c_int(), C.c_int(), C.c_ulong()
        if not x.XTranslateCoordinates(self._dpy, win, self._root, 0, 0, C.byref(ox), C.byref(oy), C.byref(child)) \
                or self._error:
            return None
        return ox.value, oy.value, w.value, h.value

    def _has_wm_state(self, win: int) -> bool:
        return self._prop(win, "WM_STATE", "WM_STATE") is not None

    def _client(self, win: int) -> int:
        """The window of the application under a window manager's frame or a toolkit's child window:
        the one carrying WM_STATE, looked for in win and then below it, as xdotool does."""
        pending = [win]
        seen = 0
        while pending and seen < 512:
            w = pending.pop(0)
            seen += 1
            if self._has_wm_state(w):
                return w
            pending.extend(self._children(w) or [])
        return win

    # -- answers ----------------------------------------------------------------

    def screen_size(self) -> tuple[int, int]:
        def go():
            scr = self._x.XDefaultScreen(self._dpy)
            return self._x.XDisplayWidth(self._dpy, scr), self._x.XDisplayHeight(self._dpy, scr)
        return self._call(go)

    def root_id(self) -> int:
        return self._call(lambda: self._root)

    def current_desktop(self) -> int | None:
        def go():
            got = self._cardinals(self._root, "_NET_CURRENT_DESKTOP")
            return got[0] if got else None
        return self._call(go)

    def active_window(self) -> int | None:
        def go():
            got = self._windows_prop(self._root, "_NET_ACTIVE_WINDOW")
            return got[0] if got and got[0] else None
        return self._call(go)

    def transient_for(self, window: int) -> int | None:
        def go():
            got = self._windows_prop(window, "WM_TRANSIENT_FOR")
            return got[0] if got and got[0] else None
        return self._call(go)

    def window_role(self, window: int) -> str | None:
        def go():
            got = self._prop(window, "WM_WINDOW_ROLE", "STRING")
            return got[1].decode("latin-1") if got and isinstance(got[1], bytes) and got[0] == "STRING" else None
        return self._call(go)

    def on_show(self, window: int) -> bool | None:
        def go():
            a = self._attrs(window)
            return None if a is None else a.map_state == IS_VIEWABLE
        return self._call(go)

    def client_list(self) -> list[int] | None:
        """_NET_CLIENT_LIST of the window manager, in its order; None when there is none."""
        return self._call(lambda: self._windows_prop(self._root, "_NET_CLIENT_LIST"))

    def window_info(self, window: int) -> dict | None:
        """title, wm_class (None: not set), desktop (None: not set) and box of one window, or None
        when it is gone."""
        def go():
            box = self._box(window)
            if box is None:
                return None
            desk = self._cardinals(window, "_NET_WM_DESKTOP")
            return {"title": self._title(window), "wm_class": self._wm_class(window),
                    "desktop": desk[0] if desk else None, "box": box}
        return self._call(go)

    def visible_named(self) -> list[int]:
        """Every viewable window below the root that has a name, parents before children and
        siblings bottom to top: what `xdotool search --onlyvisible --name .` returns."""
        def go():
            found, stack = [], list(reversed(self._children(self._root) or []))
            while stack:
                w = stack.pop()
                a = self._attrs(w)
                if a is None:
                    continue
                if a.map_state == IS_VIEWABLE and self._title(w):
                    found.append(w)
                stack.extend(reversed(self._children(w) or []))
            return found
        return self._call(go)

    def window_at(self, px: int, py: int) -> int:
        """The client window at a screen point, without moving any pointer: the topmost viewable
        InputOutput child of the root that holds the point, resolved to the window with WM_STATE.
        The root itself when nothing does. The agents' drawn cursors are skipped. Window shapes
        are not looked at."""
        def go():
            for child in reversed(self._children(self._root) or []):
                a = self._attrs(child)
                if a is None or a.map_state != IS_VIEWABLE or a.cls != INPUT_OUTPUT:
                    continue
                span = 2 * a.border_width
                if not (a.x <= px < a.x + a.width + span and a.y <= py < a.y + a.height + span):
                    continue
                client = self._client(child)
                if self._title(client).startswith(TITLE_PREFIX) or self._title(child).startswith(TITLE_PREFIX):
                    continue
                return client
            return self._root
        return self._call(go)

    def _toplevel(self, win: int) -> int | None:
        """The ancestor of win (or win itself) that is a direct child of the root, None when gone."""
        for _ in range(64):
            kids = self._tree_parent(win)
            if kids is None or kids == 0:
                return None
            if kids == self._root:
                return win
            win = kids
        return None

    def _tree_parent(self, win: int) -> int | None:
        root, parent, kids, n = C.c_ulong(), C.c_ulong(), C.POINTER(C.c_ulong)(), C.c_uint()
        self._error = None
        if not self._x.XQueryTree(self._dpy, win, C.byref(root), C.byref(parent), C.byref(kids), C.byref(n)) \
                or self._error:
            return None
        if kids:
            self._x.XFree(kids)
        return parent.value

    def window_covered(self, window: int) -> bool | None:
        """Whether another viewable window overlaps the window's box (as list_windows reports it)
        from above in the stacking order. None when the window is not on screen or is gone. The
        agents' drawn cursors do not count; neither do windows that take no pixels (InputOnly)."""
        def go():
            top = self._toplevel(window)
            a = self._attrs(window)
            if top is None or a is None or a.map_state != IS_VIEWABLE:
                return None
            box = self._box(window)
            if box is None:
                return None
            bx, by, bw, bh = box
            stack = self._children(self._root) or []
            if top not in stack:
                return None
            for other in stack[stack.index(top) + 1:]:
                o = self._attrs(other)
                if o is None or o.map_state != IS_VIEWABLE or o.cls != INPUT_OUTPUT:
                    continue
                span = 2 * o.border_width
                if o.x >= bx + bw or o.x + o.width + span <= bx or o.y >= by + bh or o.y + o.height + span <= by:
                    continue
                if self._title(other).startswith(TITLE_PREFIX) or self._title(self._client(other)).startswith(TITLE_PREFIX):
                    continue
                return True
            return False
        return self._call(go)

    def idle_ms(self) -> int:
        """Milliseconds since the last input, from the MIT-SCREEN-SAVER extension (libXss)."""
        def go():
            if not self._xss_tried:
                self._xss_tried = True
                name = ctypes.util.find_library("Xss") or "libXss.so.1"
                try:
                    lib = C.CDLL(name)
                except OSError:
                    lib = None
                if lib is not None:
                    lib.XScreenSaverQueryInfo.restype = C.c_int
                    lib.XScreenSaverQueryInfo.argtypes = [C.c_void_p, C.c_ulong, C.POINTER(_SaverInfo)]
                self._xss = lib
            if self._xss is None:
                raise XQueryUnavailable("libXss is not installed")
            info = _SaverInfo()
            if not self._xss.XScreenSaverQueryInfo(self._dpy, self._root, C.byref(info)):
                raise XQueryUnavailable("the MIT-SCREEN-SAVER extension is not available")
            return int(info.idle)
        return self._call(go)
