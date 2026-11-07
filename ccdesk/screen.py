"""Named private screens: one Xvfb + window manager + private bus that several servers join.

`ccdesk screen start NAME` runs a Headless in the foreground and writes a state file with what a
joiner needs. `ccdesk mcp --screen NAME` reads it and drives that screen without owning it:
leaving the server does not stop the screen, `ccdesk screen stop NAME` (or Ctrl-C) does.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path

from ccdesk import headless as hl
from ccdesk.errors import CcdeskError
from ccdesk.safety import cache_dir

NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}")
# The only variables a joiner takes from the screen: never the whole environment of whoever started it.
ENV_KEYS = ("DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "AT_SPI_BUS_ADDRESS", *hl.FORCE)


def state_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return (Path(runtime) / "ccdesk" if runtime else cache_dir()) / "screens"


def check_name(name: str) -> str:
    if not NAME_RE.fullmatch(name or ""):
        raise CcdeskError(f"screen name must be letters, digits, '_', '.' or '-' (up to 63), not {name!r}")
    return name


def state_path(name: str) -> Path:
    return state_dir() / f"{check_name(name)}.json"


def _proc_stat(pid) -> list[str] | None:
    """Fields 3.. of /proc/<pid>/stat (state first, start time at index 19), or None if unreadable."""
    try:
        text = Path(f"/proc/{int(pid)}/stat").read_text()
    except (OSError, ValueError, TypeError):
        return None
    return text.rpartition(")")[2].split()


def start_time(pid) -> str | None:
    fields = _proc_stat(pid)
    return fields[19] if fields and len(fields) > 19 else None


def _alive(pid, started: str | None = None) -> bool:
    """Is `pid` a live process (not a zombie) and, when `started` is given, still the same process."""
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        pass
    except (OSError, ValueError, TypeError):
        return False
    if Path("/proc/self/stat").exists():
        fields = _proc_stat(pid)
        if not fields or fields[0] in ("Z", "X"):
            return False
        if started is not None and (len(fields) < 20 or fields[19] != str(started)):
            return False
    return True


def read_state(name: str) -> dict | None:
    """The state of a running screen, or None. A file of a dead pid is removed."""
    path = state_path(name)
    try:
        info = json.loads(path.read_text())
        if _alive(info["pid"], info.get("started")):
            return info
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError):
        pass
    path.unlink(missing_ok=True)
    return None


def running() -> list[dict]:
    found = []
    folder = state_dir()
    if not folder.is_dir():
        return found
    for path in sorted(folder.glob("*.json")):
        info = read_state(path.stem) if NAME_RE.fullmatch(path.stem) else None
        if info:
            found.append(info)
    return found


def screen_env(env: dict) -> dict:
    return {k: env[k] for k in ENV_KEYS if env.get(k)}


def write_state(name: str, screen: hl.Headless) -> Path:
    folder = state_dir()
    for d in (folder.parent, folder):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    w, h = screen.size
    info = {"name": name, "pid": os.getpid(), "started": start_time(os.getpid()),
            "display": screen.display, "size": f"{w}x{h}",
            "wm": screen.wm_name, "private_bus": screen.private_bus, "notes": screen.notes,
            "env": screen_env(screen.env)}
    path = state_path(name)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(info, f)
    os.replace(tmp, path)
    return path


class JoinedScreen:
    """What Desktop needs from a screen it does not own (the part of Headless it reads)."""

    def __init__(self, info: dict, base_env: dict | None = None):
        self.name = info["name"]
        self.display = info["display"]
        self.size = info["size"]
        self.wm_name = info.get("wm")
        self.private_bus = bool(info.get("private_bus"))
        self.notes = list(info.get("notes") or [])
        self.pid = info["pid"]
        base = os.environ if base_env is None else base_env
        self.env = {k: v for k, v in base.items() if k not in hl.DROP}
        self.env.update({k: v for k, v in info.get("env", {}).items() if k in ENV_KEYS})

    def info(self) -> dict:
        return {"display": self.display, "size": self.size, "window_manager": self.wm_name,
                "private_bus": self.private_bus, "pid": self.pid, "notes": self.notes, "screen": self.name}

    def stop(self) -> None:  # joining never owns the screen
        pass


def join(name: str, base_env: dict | None = None) -> JoinedScreen:
    info = read_state(name)
    if not info:
        raise CcdeskError(f"no screen named {name!r} is running: start it with `ccdesk screen start {name}`")
    return JoinedScreen(info, base_env)


def start(name: str, size: str = hl.DEFAULT_SIZE, wm: str = "auto") -> int:
    check_name(name)
    folder = state_dir()
    for d in (folder.parent, folder):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    # Held for the screen's whole life; the kernel drops it if this process dies.
    lock = open(folder / f"{name}.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise CcdeskError(f"screen {name!r} is already running (stop it with `ccdesk screen stop {name}`)")
    try:
        if read_state(name):
            raise CcdeskError(f"screen {name!r} is already running (stop it with `ccdesk screen stop {name}`)")
        return _run(name, size, wm)
    finally:
        lock.close()


def _run(name: str, size: str, wm: str) -> int:
    screen = hl.Headless(size=size, wm=wm)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    screen.start()
    path = None
    try:
        path = write_state(name, screen)
        print(f"screen {name} on {screen.display} ({size}): join with `ccdesk mcp --screen {name}`, "
              f"watch with `x11vnc -display {screen.display} -localhost` or `ccdesk peek`", flush=True)
        for note in screen.notes:
            print(f"note: {note}", file=sys.stderr)
        xvfb = screen._procs[0]
        while not stop.wait(0.5):
            if xvfb.poll() is not None:
                print(f"screen {name}: Xvfb exited", file=sys.stderr)
                break
    finally:
        if path:
            try:
                if json.loads(path.read_text()).get("pid") == os.getpid():
                    path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
        screen.stop()
    return 0


# Headless.stop() gives each of its ~5 children up to 3 s to exit after SIGTERM.
STOP_TIMEOUT = 20.0


def stop_screen(name: str, timeout: float = STOP_TIMEOUT) -> None:
    info = read_state(name)  # also checks the pid is still the screen, not a reused one
    if not info:
        raise CcdeskError(f"no screen named {name!r} is running")
    pid = int(info["pid"])
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        raise CcdeskError(f"no screen named {name!r} is running")
    except PermissionError:
        raise CcdeskError(f"cannot stop screen {name!r} (pid {pid}): it belongs to another user")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid, info.get("started")):
            return
        time.sleep(0.05)
    raise CcdeskError(f"screen {name!r} (pid {pid}) did not exit within {timeout:g}s after SIGTERM")
