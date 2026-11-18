"""Real 0.38.0 input on a private Xvfb and accessibility bus, never the real desktop."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from ccdesk.backends import X11Backend
from ccdesk.backends.cua_driver import CuaDriverBackend
from ccdesk.headless import Headless, find_libexec


def binary():
    candidates = [os.environ.get("CCDESK_TEST_CUA_DRIVER"),
                  str(Path.home() / ".cache/ccdesk-dev/cua-driver"), shutil.which("cua-driver")]
    for path in candidates:
        if path and os.path.isfile(path):
            version = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
            if version.stdout.strip().split()[-1:] == ["0.38.0"]:
                return path
    return None


def skip_reason():
    if not binary():
        return "cua-driver 0.38.0 absent (set CCDESK_TEST_CUA_DRIVER or use ~/.cache/ccdesk-dev/cua-driver)"
    for tool in ("Xvfb", "xdotool", "dbus-daemon", "bspwm", "xprop"):
        if not shutil.which(tool):
            return f"{tool} absent"
    if not find_libexec("at-spi2-registryd"):
        return "AT-SPI registry absent"
    probe = subprocess.run(["/usr/bin/python3", "-c", "import gi; gi.require_version('Gtk', '3.0')"],
                           capture_output=True)
    if probe.returncode:
        return "system PyGObject/GTK3 absent"
    return None


pytestmark = [pytest.mark.display, pytest.mark.skipif(skip_reason() is not None, reason=skip_reason() or "")]


def wait_for(read, predicate=lambda value: bool(value), timeout=15):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = read()
        if predicate(value):
            return value
        time.sleep(0.05)
    pytest.fail(f"Timed out: {value!r}")


@pytest.mark.parametrize("overlay", [False, True])
def test_unfocused_click_and_type_preserve_user_input_focus_and_pointer(tmp_path, monkeypatch, overlay):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    screen = Headless(size="1200x800", wm="auto")
    def start_private_x(xvfb):
        # Headless normally asks Xvfb for the first free number, often :1.
        # Keep this integration on a high, explicitly owned display instead.
        for number in range(200, 300):
            if Path(f"/tmp/.X11-unix/X{number}").exists() or Path(f"/tmp/.X{number}-lock").exists():
                continue
            screen.display = f":{number}"
            screen.env["DISPLAY"] = screen.display
            proc = screen._spawn([xvfb, screen.display, "-screen", "0", "1200x800x24", "-nolisten", "tcp"])
            def ready():
                assert proc.poll() is None, "private Xvfb exited"
                return subprocess.run(["xdotool", "getdisplaygeometry"], env=screen.env,
                                      capture_output=True).returncode == 0
            wait_for(ready)
            return
        pytest.fail("No free private display from :200 to :299")
    monkeypatch.setattr(screen, "_start_x", start_private_x)
    apps = []
    backend = None
    writer = None
    try:
        env = screen.start()
        assert screen.private_bus and env["DISPLAY"] not in (":0", ":1")
        session = X11Backend(env=env)

        def read(role):
            try:
                return json.loads((tmp_path / f"{role}.json").read_text())
            except (OSError, ValueError):
                return {}

        for role in ("target", "user"):
            apps.append(subprocess.Popen(["/usr/bin/python3", str(Path(__file__).with_name("cua_gtk_app.py")),
                                          str(tmp_path / f"{role}.json"), role], env=env))
            wait_for(lambda: read(role))

        def window(role):
            return next((w for w in session.list_windows() if w.title == "ccdesk-cua-" + role), None)

        target = wait_for(lambda: window("target"))
        user = wait_for(lambda: window("user"))
        # Test setup is the only foreground input. It runs entirely on this private screen.
        session.run(["xdotool", "windowactivate", "--sync", user.id])
        session.run(["xdotool", "mousemove", "980", "650"])
        time.sleep(0.3)
        focus = session.run(["xdotool", "getwindowfocus"]).strip()
        active = session.active_window()
        assert active == user.id and active != target.id
        active_property = session.run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
        pointer = session.run(["xdotool", "getmouselocation", "--shell"])
        focus_events = read("user")["focus_events"]
        backend = CuaDriverBackend(session, binary(), overlay=overlay)
        sent = []
        driver_call = backend.driver.call
        def record_call(name, args, **kwargs):
            sent.append((name, args.copy()))
            return driver_call(name, args, **kwargs)
        monkeypatch.setattr(backend.driver, "call", record_call)
        def undisturbed():
            assert session.run(["xdotool", "getwindowfocus"]).strip() == focus
            assert session.run(["xprop", "-root", "_NET_ACTIVE_WINDOW"]) == active_property
            assert session.active_window() == active
            assert session.run(["xdotool", "getmouselocation", "--shell"]) == pointer
        backend.activate_window(target)
        undisturbed()
        backend.move(*read("target")["button"])
        undisturbed()
        if overlay:
            def overlay_window():
                xq = session._xq()
                return any((xq.window_info(wid) or {}).get("title", "").startswith("Cua.AgentCursorOverlay.")
                           for wid in xq.visible_named())
            wait_for(overlay_window)
        user_text = "the user keeps typing without interruption"
        writer = subprocess.Popen(["xdotool", "type", "--clearmodifiers", "--delay", "80", user_text], env=env)
        backend.click(*read("target")["button"], 1, 1)
        wait_for(lambda: read("target"), lambda state: state.get("clicks") == 1)
        undisturbed()
        backend.type_text("background text")
        wait_for(lambda: read("target"), lambda state: state.get("text") == "background text")
        undisturbed()
        writer.wait(timeout=15)
        assert writer.returncode == 0
        wait_for(lambda: read("user"), lambda state: state.get("text") == user_text)
        undisturbed()
        assert read("user")["focus_events"] == focus_events
        click = next(args for name, args in sent if name == "click")
        assert click == {"pid": apps[0].pid, "window_id": int(target.id, 16),
                         "x": read("target")["button"][0], "y": read("target")["button"][1],
                         "coordinate_frame": "desktop", "delivery_mode": "background", "button": "left", "count": 1}
        assert all(args.get("scope") != "desktop" for _, args in sent)
    finally:
        if writer and writer.poll() is None:
            writer.terminate()
            writer.wait(timeout=5)
        if backend:
            backend.close()
        for app in apps:
            app.terminate()
            app.wait(timeout=5)
        screen.stop()
