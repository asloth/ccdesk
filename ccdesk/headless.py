"""A private screen for the agent, so it can work while you use your own.

`ccdesk mcp --headless` starts an invisible X display (Xvfb), a window manager on it, and a
private D-Bus session with its own accessibility bus. Everything ccdesk does goes there: your
mouse, keyboard, windows and clipboard are never touched, and ui_tree only sees the apps the
agent launched. When the server exits, all of it is stopped.

Apps can't reach your session bus, so they don't hand their window to a copy already running on
your screen (Firefox, GNOME apps), and they don't see your keyring or notifications either.
"""

from __future__ import annotations

import ctypes
import glob
import json
import os
import re
import select
import shutil
import signal
import subprocess
import time
from pathlib import Path

from ccdesk.errors import CcdeskError
from ccdesk.safety import cache_dir

DEFAULT_SIZE = "1920x1080"
# Floating window managers that run without a config. bspwm is a fallback with a stub config.
WMS = ("openbox", "fluxbox", "icewm", "matchbox-window-manager", "xfwm4")
BSPWMRC = Path(__file__).with_name("headless_bspwmrc")
LIBEXEC = ("/usr/lib", "/usr/libexec", "/usr/lib/at-spi2-core", "/usr/lib/*/at-spi2-core")
# Set on the private screen: these point at your own session and must not leak into it.
DROP = ("WAYLAND_DISPLAY", "WAYLAND_SOCKET", "SWAYSOCK", "HYPRLAND_INSTANCE_SIGNATURE", "I3SOCK",
        "BSPWM_SOCKET", "AT_SPI_BUS_ADDRESS", "DESKTOP_STARTUP_ID", "XDG_ACTIVATION_TOKEN")
FORCE = {
    "XDG_SESSION_TYPE": "x11",
    "GDK_BACKEND": "x11",
    "QT_QPA_PLATFORM": "xcb",
    "MOZ_ENABLE_WAYLAND": "0",
    "ELECTRON_OZONE_PLATFORM_HINT": "x11",
    "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
    "GNOME_ACCESSIBILITY": "1",
    "GIO_USE_VFS": "local",  # no gvfs daemons on the private bus
    "GVFS_DISABLE_FUSE": "1",
}
START_TIMEOUT = 10.0


def parse_size(spec: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{3,5})x(\d{3,5})\s*", spec or "")
    if not m:
        raise CcdeskError(f"screen size must look like 1920x1080, not {spec!r}")
    return int(m.group(1)), int(m.group(2))


def _die_with_parent() -> None:
    """Children get SIGTERM if the server is killed without a chance to clean up."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except OSError:
        pass


def find_libexec(name: str) -> str | None:
    for base in LIBEXEC:
        for path in glob.glob(os.path.join(base, name)):
            if os.access(path, os.X_OK):
                return path
    return None


class Headless:
    def __init__(self, size: str = DEFAULT_SIZE, wm: str = "auto", which=shutil.which):
        self.size = parse_size(size)
        self.wm = wm
        self._which = which
        self.display: str | None = None
        self.wm_name: str | None = None
        self.private_bus = False
        self.env: dict[str, str] = {}
        self._procs: list[subprocess.Popen] = []
        self._state: Path | None = None
        self.notes: list[str] = []

    def _spawn(self, argv: list[str], **kw) -> subprocess.Popen:
        kw.setdefault("stdout", subprocess.DEVNULL)
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.env,
                                start_new_session=True, preexec_fn=_die_with_parent, **kw)
        self._procs.append(proc)
        return proc

    def start(self) -> dict[str, str]:
        xvfb = self._which("Xvfb")
        if not xvfb:
            raise CcdeskError("--headless needs Xvfb (Arch: xorg-server-xvfb, Debian/Ubuntu: xvfb, Fedora: "
                              "xorg-x11-server-Xvfb)")
        self.env = {k: v for k, v in os.environ.items() if k not in DROP}
        self.env.update(FORCE)
        try:
            self._start_x(xvfb)
            self._start_bus()
            self._start_wm()
        except BaseException:
            self.stop()
            raise
        self._write_state()
        return self.env

    def _start_x(self, xvfb: str) -> None:
        # -displayfd: Xvfb picks a free display number itself and writes it to the pipe.
        r, w = os.pipe()
        w_, h_ = self.size
        try:
            self._spawn([xvfb, "-displayfd", str(w), "-screen", "0", f"{w_}x{h_}x24", "-nolisten", "tcp"],
                        pass_fds=(w,))
        finally:
            os.close(w)
        try:
            ready, _, _ = select.select([r], [], [], START_TIMEOUT)
            number = os.read(r, 32).decode().strip() if ready else ""
        finally:
            os.close(r)
        if not number.isdigit():
            raise CcdeskError("Xvfb did not start (no display number within 10s)")
        self.display = f":{number}"
        self.env["DISPLAY"] = self.display

    def _start_bus(self) -> None:
        """Own session bus plus accessibility bus and registry, started by hand: the bus launcher
        would ask systemd to start the registry, and systemd only knows your real session."""
        dbus = self._which("dbus-daemon")
        launcher = find_libexec("at-spi-bus-launcher")
        registry = find_libexec("at-spi2-registryd")
        if not (dbus and launcher and registry):
            self.notes.append("no private bus (needs dbus-daemon and at-spi2-core): the agent's apps share "
                              "your session bus, so ui_tree may also list your own windows")
            return
        proc = self._spawn([dbus, "--session", "--nofork", "--print-address=1"], stdout=subprocess.PIPE)
        ready, _, _ = select.select([proc.stdout], [], [], START_TIMEOUT)
        address = proc.stdout.readline().decode().strip() if ready else ""
        if not address:
            raise CcdeskError("dbus-daemon did not print an address")
        self.env["DBUS_SESSION_BUS_ADDRESS"] = address
        self._spawn([launcher, "--launch-immediately", "--a11y=1"])
        time.sleep(0.5)
        self._spawn([registry])
        self.private_bus = True

    def _start_wm(self) -> None:
        if self.wm == "none":
            self.notes.append("no window manager: activate_window and the active window are approximate")
            return
        if self.wm != "auto":
            argv = self.wm.split()
        else:
            found = next((name for name in WMS if self._which(name)), None)
            if found:
                argv = [found]
            elif self._which("bspwm"):
                argv = ["bspwm", "-c", str(BSPWMRC)]
            else:
                self.notes.append(f"no window manager found (install one of: {', '.join(WMS)}); "
                                  f"activate_window and the active window are approximate")
                return
        if not self._which(argv[0]):
            raise CcdeskError(f"window manager {argv[0]!r} not found on PATH")
        self._spawn(argv)
        self.wm_name = os.path.basename(argv[0])
        time.sleep(0.3)

    def _write_state(self) -> None:
        """Lets `ccdesk peek` find this screen."""
        folder = cache_dir() / "headless"
        folder.mkdir(parents=True, exist_ok=True)
        self._state = folder / f"{os.getpid()}.json"
        self._state.write_text(json.dumps(self.info()))

    def info(self) -> dict:
        w, h = self.size
        return {"display": self.display, "size": f"{w}x{h}", "window_manager": self.wm_name,
                "private_bus": self.private_bus, "pid": os.getpid(), "notes": self.notes}

    def stop(self) -> None:
        for proc in reversed(self._procs):
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        for proc in reversed(self._procs):
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self._procs.clear()
        if self._state:
            self._state.unlink(missing_ok=True)
            self._state = None


def running() -> list[dict]:
    """Headless screens of ccdesk servers that are still alive."""
    found = []
    for path in sorted((cache_dir() / "headless").glob("*.json")):
        try:
            info = json.loads(path.read_text())
            os.kill(int(info["pid"]), 0)
        except (OSError, ValueError, KeyError):
            path.unlink(missing_ok=True)  # the server died without cleaning up
            continue
        found.append(info)
    return found
