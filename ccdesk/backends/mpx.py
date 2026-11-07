"""An agent's own pointer and keyboard: an XInput2 master pair with its own XTEST slaves.

`xinput create-master ccdesk-NAME` adds a "ccdesk-NAME pointer" and a "ccdesk-NAME keyboard"
master, with "ccdesk-NAME XTEST pointer" / "... XTEST keyboard" slaves that fake input. Events
sent through the slaves move only that master pair: the user's pointer does not jump and the
user's keyboard focus does not change. Firefox and GTK 3 accept this input.

libX11, libXi and libXtst are used through ctypes (stdlib only). Every call declares argtypes
and restype: a default int return would truncate the 64-bit Display pointer. One Display per
process, and one lock around every call, because the server runs each tool call on a worker thread.
"""

from __future__ import annotations

import atexit
import ctypes as C
import ctypes.util
import fcntl
import os
import re
import shutil
import subprocess
import threading
import time

from ccdesk.errors import CcdeskError, UnsupportedError, ValidationError
from ccdesk.parsing import KeyCombo
from ccdesk.seat import _screen_key, lock_dir

PREFIX = "ccdesk-"
CORE_POINTER, CORE_KEYBOARD = 2, 3  # the user's master pair: where removed devices' slaves go back to
KEY_DELAY = 0.006  # between typed characters
SPARE_SETTLE = 0.03  # the toolkit reads a keymap change before the key that uses it
MODIFIER_KEYSYMS = {"ctrl": 0xFFE3, "alt": 0xFFE9, "shift": 0xFFE1, "super": 0xFFEB}
RETURN, TAB = 0xFF0D, 0xFF09
SHIFT = MODIFIER_KEYSYMS["shift"]

_LINE = re.compile(r"^[^\w]*(?P<name>.+?)\s+id=(?P<id>\d+)\s+\[(?P<kind>master|slave|floating slave)\s+"
                   r"(?P<type>pointer|keyboard)")


def device_name(agent: str) -> str:
    return f"{PREFIX}{agent}"


def parse_xinput_list(text: str) -> list[dict]:
    """Rows of `xinput list`: name, id, kind (master/slave/floating slave), type (pointer/keyboard)."""
    rows = []
    for line in text.splitlines():
        m = _LINE.match(line.strip())
        if m:
            rows.append({"name": m["name"].strip(), "id": int(m["id"]), "kind": m["kind"], "type": m["type"]})
    return rows


def find_devices(rows: list[dict], name: str) -> dict[str, int]:
    """The four devices of master pair `name`, by role; roles not present are left out."""
    wanted = {
        "master_pointer": (f"{name} pointer", "master", "pointer"),
        "master_keyboard": (f"{name} keyboard", "master", "keyboard"),
        "slave_pointer": (f"{name} XTEST pointer", "slave", "pointer"),
        "slave_keyboard": (f"{name} XTEST keyboard", "slave", "keyboard"),
    }
    found = {}
    for role, (dev_name, kind, typ) in wanted.items():
        for row in rows:
            if row["name"] == dev_name and row["kind"] == kind and row["type"] == typ:
                found[role] = row["id"]
    return found


class NameLock:
    """One live process per agent name on a screen: an flock held for the process lifetime on
    lock_dir()/own-DISPLAY-NAME.lock. The kernel drops it when the process dies, however it dies, so
    a free lock proves that a ccdesk-NAME master pair on that screen is a leftover."""

    def __init__(self, env: dict, agent: str):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", agent)
        self.path = lock_dir() / f"own-{_screen_key('x11', env)}-{safe}.lock"
        self.agent = agent
        self._fd: int | None = None
        self.unavailable: str | None = None  # why the lock file cannot be made

    def acquire(self) -> bool:
        """True when held. False when the lock file cannot be made (then nothing is proven).
        Raises CcdeskError when another live process holds it."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            self.unavailable = str(exc)
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise CcdeskError(f"another ccdesk agent named {self.agent} is running on this screen; "
                              f"use another --agent name") from None
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def clean_leftovers(run, env: dict) -> list[str]:
    """Remove every ccdesk-* master pair whose agent-name lock is free (its process is gone):
    what a kill -9 or a crash leaves behind. Returns the device names removed."""
    removed = []
    for row in parse_xinput_list(run("list")):
        name = row["name"]
        if not (row["kind"] == "master" and row["type"] == "pointer" and name.startswith(PREFIX)
                and name.endswith(" pointer")):
            continue
        lock = NameLock(env, name[len(PREFIX):-len(" pointer")])
        try:
            if not lock.acquire():
                continue
        except CcdeskError:
            continue  # a live agent owns it
        try:
            run("remove-master", str(row["id"]), "AttachToMaster", str(CORE_POINTER), str(CORE_KEYBOARD))
            removed.append(name[:-len(" pointer")])
        except CcdeskError:
            pass
        finally:
            lock.release()
    return removed


def unavailable_reason(backend_name: str, which=shutil.which) -> str | None:
    """Why an own pointer cannot be used here, or None."""
    if backend_name != "x11":
        return f"an own pointer needs the x11 backend (this one is {backend_name})"
    if not which("xinput"):
        return "xinput is not installed (it creates the agent's pointer and keyboard)"
    for lib in ("X11", "Xi", "Xtst"):
        if not ctypes.util.find_library(lib):
            return f"lib{lib} is not installed"
    return None


class _XErrorEvent(C.Structure):
    _fields_ = [("type", C.c_int), ("display", C.c_void_p), ("resourceid", C.c_ulong), ("serial", C.c_ulong),
                ("error_code", C.c_ubyte), ("request_code", C.c_ubyte), ("minor_code", C.c_ubyte)]


class Libs:
    """The three libraries with every signature declared."""

    def __init__(self):
        self.x = C.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        self.xi = C.CDLL(ctypes.util.find_library("Xi") or "libXi.so.6")
        self.xt = C.CDLL(ctypes.util.find_library("Xtst") or "libXtst.so.6")
        v, u, i, d, ul, ub, ci = C.c_void_p, C.c_uint, C.c_int, C.c_double, C.c_ulong, C.c_ubyte, C.c_char_p
        x, xi, xt = self.x, self.xi, self.xt

        def sig(fn, restype, *argtypes):
            fn.restype, fn.argtypes = restype, list(argtypes)

        sig(x.XOpenDisplay, v, ci)
        sig(x.XCloseDisplay, i, v)
        sig(x.XDefaultRootWindow, ul, v)
        sig(x.XSync, i, v, i)
        sig(x.XFlush, i, v)
        sig(x.XDisplayKeycodes, i, v, C.POINTER(i), C.POINTER(i))
        sig(x.XGetKeyboardMapping, C.POINTER(ul), v, ub, i, C.POINTER(i))
        sig(x.XChangeKeyboardMapping, i, v, i, i, C.POINTER(ul), i)
        sig(x.XFree, i, v)
        sig(x.XStringToKeysym, ul, ci)
        sig(x.XSetErrorHandler, v, v)
        sig(xi.XOpenDevice, v, v, ul)
        sig(xi.XCloseDevice, i, v, v)
        sig(xi.XIWarpPointer, i, v, i, ul, ul, d, d, u, u, d, d)
        sig(xi.XISetFocus, i, v, i, ul, ul)
        sig(xi.XIGetFocus, i, v, i, C.POINTER(ul))
        sig(xi.XIQueryPointer, C.c_int, v, i, ul, C.POINTER(ul), C.POINTER(ul), C.POINTER(d), C.POINTER(d),
            C.POINTER(d), C.POINTER(d), v, v, v)
        sig(xt.XTestFakeDeviceButtonEvent, i, v, v, u, i, v, i, ul)
        sig(xt.XTestFakeDeviceKeyEvent, i, v, v, u, i, v, i, ul)


def keysym_for_char(ch: str) -> int:
    o = ord(ch)
    return o if 0x20 <= o <= 0x7E or 0xA0 <= o <= 0xFF else 0x01000000 + o


class Mpx:
    """One agent's master pointer and keyboard. create() adds them, close() removes them."""

    def __init__(self, agent: str, env: dict, run=None, libs=None):
        self.agent = agent
        self.name = device_name(agent)
        self.env = dict(env)
        self._run = run or self._xinput
        self._libs = libs
        self.lock = threading.RLock()
        self.ids: dict[str, int] = {}
        self._dpy = None
        self._root = 0
        self._slaves: dict[str, int] = {}  # "pointer"/"keyboard" -> XDevice*
        self._error: str | None = None
        self._handler = None
        self._closed = True
        self._spare_lock = threading.Lock()
        self.name_lock = NameLock(self.env, agent)
        self.cleaned: list[str] = []  # leftovers of dead agents that create() removed

    # -- xinput ------------------------------------------------------------

    def _xinput(self, *args: str) -> str:
        try:
            proc = subprocess.run(["xinput", *args], capture_output=True, text=True, timeout=10, env=self.env)
        except FileNotFoundError:
            raise UnsupportedError("xinput is not installed") from None
        except subprocess.TimeoutExpired:
            raise CcdeskError(f"xinput {args[0]} timed out") from None
        if proc.returncode:
            lines = (proc.stderr or proc.stdout).strip().splitlines()
            raise CcdeskError(f"xinput {args[0]} failed: {lines[-1] if lines else 'no output'}")
        return proc.stdout

    def devices(self) -> dict[str, int]:
        return find_devices(parse_xinput_list(self._run("list")), self.name)

    def remove_stale(self) -> bool:
        """Remove a master pair with this exact name, left behind by a crash."""
        found = self.devices()
        if "master_pointer" not in found:
            return False
        self._run("remove-master", str(found["master_pointer"]), "AttachToMaster",
                  str(CORE_POINTER), str(CORE_KEYBOARD))
        return True

    # -- lifecycle -----------------------------------------------------------

    def create(self) -> None:
        with self.lock:
            if not self._closed:
                return
            self.name_lock.acquire()  # raises when a live process already has this name
            try:
                self._create()
            except BaseException:
                self.name_lock.release()
                raise
            atexit.register(self.close)

    def _create(self) -> None:
        # The name lock is ours, so a pair with this name is a leftover; so is any other
        # ccdesk-* pair whose own lock is free.
        self.remove_stale()
        if not self.name_lock.unavailable:
            self.cleaned = clean_leftovers(self._run, self.env)
        self._run("create-master", self.name)
        self.ids = self.devices()
        missing = {"master_pointer", "master_keyboard", "slave_pointer", "slave_keyboard"} - set(self.ids)
        if missing:
            self.ids = {}
            raise CcdeskError(f"xinput create-master {self.name} did not make {', '.join(sorted(missing))}")
        self._closed = False
        try:
            self._open()
        except BaseException:
            self.close()
            raise

    def _open(self) -> None:
        libs = self._libs = self._libs or Libs()
        display = self.env.get("DISPLAY")
        if not display:
            raise CcdeskError("DISPLAY is not set")
        self._dpy = libs.x.XOpenDisplay(display.encode())
        if not self._dpy:
            raise CcdeskError(f"cannot open display {display}")
        # Xlib's default error handler exits the process; a bad window id must be an error instead.
        proto = C.CFUNCTYPE(C.c_int, C.c_void_p, C.POINTER(_XErrorEvent))

        def on_error(_dpy, ev):
            self._error = f"X error {ev.contents.error_code} in request {ev.contents.request_code}"
            return 0

        self._handler = proto(on_error)
        libs.x.XSetErrorHandler(C.cast(self._handler, C.c_void_p))
        self._root = libs.x.XDefaultRootWindow(self._dpy)
        for kind in ("pointer", "keyboard"):
            dev = libs.xi.XOpenDevice(self._dpy, self.ids[f"slave_{kind}"])
            if not dev:
                raise CcdeskError(f"cannot open {self.name} XTEST {kind}")
            self._slaves[kind] = dev
        self._sync()

    def close(self) -> None:
        # From a signal handler while a worker types, wait a moment and then go on without the lock:
        # the process is on its way out and the devices must not outlive it.
        held = self.lock.acquire(timeout=2)
        try:
            self._close()
        finally:
            if held:
                self.lock.release()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        libs = self._libs
        try:
            if libs and self._dpy:
                for dev in self._slaves.values():
                    libs.xi.XCloseDevice(self._dpy, dev)
                libs.x.XSync(self._dpy, 0)
        except Exception:
            pass
        self._slaves = {}
        try:
            # Only our own pair: if it was removed and another agent now has this name, its
            # devices may have taken the same id.
            mine = self.ids.get("master_pointer")
            if mine is not None and any(
                    r["id"] == mine and r["name"] == f"{self.name} pointer" and r["kind"] == "master"
                    for r in parse_xinput_list(self._run("list"))):
                self._run("remove-master", str(mine), "AttachToMaster",
                          str(CORE_POINTER), str(CORE_KEYBOARD))
        except CcdeskError:
            pass  # already gone
        self.name_lock.release()
        try:
            if libs and self._dpy:
                libs.x.XCloseDisplay(self._dpy)
        except Exception:
            pass
        self._dpy = None

    def info(self) -> dict:
        return {"enabled": True, "name": self.name, **self.ids}

    # -- helpers -------------------------------------------------------------

    def _need(self):
        if self._closed or not self._dpy:
            raise CcdeskError("the agent's own pointer is closed")
        return self._libs

    def _sync(self) -> None:
        self._error = None
        self._libs.x.XSync(self._dpy, 0)
        if self._error:
            err, self._error = self._error, None
            raise CcdeskError(err)

    # -- pointer -------------------------------------------------------------

    def warp(self, x: int, y: int) -> None:
        with self.lock:
            libs = self._need()
            libs.xi.XIWarpPointer(self._dpy, self.ids["master_pointer"], 0, self._root, 0, 0, 0, 0,
                                  float(x), float(y))
            self._sync()

    def button(self, button: int, press: bool) -> None:
        with self.lock:
            libs = self._need()
            libs.xt.XTestFakeDeviceButtonEvent(self._dpy, self._slaves["pointer"], button, 1 if press else 0,
                                               None, 0, 0)
            libs.x.XFlush(self._dpy)

    def _query_pointer(self, win: int):
        libs = self._need()
        root, child = C.c_ulong(), C.c_ulong()
        rx, ry, wx, wy = (C.c_double() for _ in range(4))
        buttons = (C.c_ubyte * 16)()  # XIButtonState: int mask_len + pointer
        mods = (C.c_int * 4)()
        group = (C.c_int * 4)()
        libs.xi.XIQueryPointer(self._dpy, self.ids["master_pointer"], win, C.byref(root), C.byref(child),
                               C.byref(rx), C.byref(ry), C.byref(wx), C.byref(wy),
                               C.cast(buttons, C.c_void_p), C.cast(mods, C.c_void_p), C.cast(group, C.c_void_p))
        mask = C.c_void_p.from_buffer(buttons, 8).value  # Xlib allocated the button mask
        if mask:
            libs.x.XFree(mask)
        return round(rx.value), round(ry.value), child.value

    def query_pointer(self) -> tuple[int, int]:
        """The agent pointer's position on the screen."""
        with self.lock:
            x, y, _ = self._query_pointer(self._root)
            return x, y

    def windows_under_pointer(self) -> list[int]:
        """Windows under the agent pointer from the top-level one (a frame, with a window manager)
        down to the deepest child."""
        with self.lock:
            chain, win = [], self._root
            for _ in range(16):
                _, _, child = self._query_pointer(win)
                if not child:
                    break
                chain.append(child)
                win = child
            return chain

    # -- keyboard focus ----------------------------------------------------------

    def focus(self, window: int) -> None:
        with self.lock:
            libs = self._need()
            libs.xi.XISetFocus(self._dpy, self.ids["master_keyboard"], window, 0)
            self._sync()

    def query_focus(self) -> int | None:
        """The agent keyboard's focus window, or None when it has none (or follows the pointer)."""
        with self.lock:
            libs = self._need()
            win = C.c_ulong()
            libs.xi.XIGetFocus(self._dpy, self.ids["master_keyboard"], C.byref(win))
            self._sync()
            return win.value if win.value > 1 else None

    # -- keys -----------------------------------------------------------------------

    def _keymap(self):
        """(first keycode, last keycode, per, rows) where rows[i] are the keysyms of keycode first+i."""
        libs = self._libs
        lo, hi, per = C.c_int(), C.c_int(), C.c_int()
        libs.x.XDisplayKeycodes(self._dpy, C.byref(lo), C.byref(hi))
        count = hi.value - lo.value + 1
        raw = libs.x.XGetKeyboardMapping(self._dpy, lo.value, count, C.byref(per))
        rows = [[raw[i * per.value + j] for j in range(per.value)] for i in range(count)]
        libs.x.XFree(raw)
        return lo.value, hi.value, per.value, rows

    @staticmethod
    def lookup(rows, first: int) -> dict[int, tuple[int, bool]]:
        """keysym -> (keycode, needs shift), from the first two columns of the keymap."""
        table: dict[int, tuple[int, bool]] = {}
        for column, shifted in ((0, False), (1, True)):
            for i, row in enumerate(rows):
                if len(row) > column and row[column] and row[column] not in table:
                    table[row[column]] = (first + i, shifted)
        return table

    def keysym(self, name: str) -> int:
        if len(name) == 1:
            return keysym_for_char(name)
        sym = self._libs.x.XStringToKeysym(name.encode())
        if not sym:
            raise ValidationError(f"no key named {name!r} on this keyboard")
        return sym

    def _fake_key(self, keycode: int, press: bool) -> None:
        self._libs.xt.XTestFakeDeviceKeyEvent(self._dpy, self._slaves["keyboard"], keycode, 1 if press else 0,
                                              None, 0, 0)

    def _tap(self, keycode: int, shift: bool, shift_code: int | None) -> None:
        if shift and shift_code:
            self._fake_key(shift_code, True)
        self._fake_key(keycode, True)
        self._fake_key(keycode, False)
        if shift and shift_code:
            self._fake_key(shift_code, False)
        self._libs.x.XFlush(self._dpy)

    def _spare_keycode(self, lo: int, hi: int, rows) -> int:
        for i in range(len(rows) - 1, -1, -1):
            if not any(rows[i]):
                return lo + i
        return hi  # every key is taken: borrow the last one, as xdotool does

    def _type_unmapped(self, sym: int) -> None:
        """A character with no key in the keymap: put it on a spare keycode, type it, put the
        old mapping back. The keymap is shared by every device, so only one such character at a
        time per screen: a lock file across processes (see Mpx.spare_guard)."""
        libs = self._libs
        with self._spare_lock, self.spare_guard():
            lo, hi, per, rows = self._keymap()
            kc = self._spare_keycode(lo, hi, rows)
            old = rows[kc - lo]
            new = (C.c_ulong * per)(*([sym] * per))
            libs.x.XChangeKeyboardMapping(self._dpy, kc, per, new, 1)
            self._sync()
            time.sleep(SPARE_SETTLE)
            try:
                self._tap(kc, False, None)
                time.sleep(SPARE_SETTLE)
            finally:
                restore = (C.c_ulong * per)(*old)
                libs.x.XChangeKeyboardMapping(self._dpy, kc, per, restore, 1)
                self._sync()

    def spare_guard(self):
        from ccdesk.seat import SeatLock, seat_key

        return SeatLock("keymap-" + seat_key("x11", self.env))

    def type_text(self, text: str) -> None:
        with self.lock:
            self._need()
            text = text.replace("\r\n", "\n")
            lo, _, _, rows = self._keymap()
            table = self.lookup(rows, lo)
            shift_code = table.get(SHIFT, (None, False))[0]
            for ch in text:
                sym = RETURN if ch == "\n" else TAB if ch == "\t" else keysym_for_char(ch)
                hit = table.get(sym)
                if hit:
                    self._tap(hit[0], hit[1], shift_code)
                    time.sleep(KEY_DELAY)
                else:
                    self._type_unmapped(sym)
                    lo, _, _, rows = self._keymap()
                    table = self.lookup(rows, lo)
            self._sync()

    def press_key(self, combo: KeyCombo) -> None:
        """Modifiers down, the key, modifiers up in reverse."""
        with self.lock:
            self._need()
            lo, _, _, rows = self._keymap()
            table = self.lookup(rows, lo)
            mods = []
            for m in combo.modifiers:
                hit = table.get(MODIFIER_KEYSYMS[m])
                if not hit:
                    raise UnsupportedError(f"this keyboard has no {m} key")
                mods.append(hit[0])
            hit = table.get(self.keysym(combo.key))
            if not hit:
                raise ValidationError(f"this keyboard has no key {combo.key!r}")
            code, needs_shift = hit
            shift_code = table.get(SHIFT, (None, False))[0]
            if needs_shift and "shift" not in combo.modifiers and shift_code:
                mods.append(shift_code)
            held = []
            try:
                for kc in mods:
                    self._fake_key(kc, True)
                    held.append(kc)
                self._fake_key(code, True)
                self._fake_key(code, False)
            finally:
                for kc in reversed(held):
                    self._fake_key(kc, False)
                self._libs.x.XFlush(self._dpy)
            self._sync()
