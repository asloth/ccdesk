"""Wayland backend against a real compositor: a headless sway it starts itself, so nothing touches
the user's screen. Needs sway, grim, wtype and wl-clipboard on PATH, plus a system Python with
PyGObject + GTK 3 for the app and the accessibility tree.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from ccdesk.a11y import A11y
from ccdesk.backends import WaylandBackend
from ccdesk.desktop import Desktop
from ccdesk.errors import CcdeskError
from ccdesk.parsing import png_size
from ccdesk.safety import AuditLog, Policy

APP = Path(__file__).with_name("gtk_app.py")
PY = os.environ.get("CCDESK_A11Y_PYTHON", "/usr/bin/python3")
TOOLS = ("sway", "swaymsg", "grim", "wtype", "wl-copy", "wl-paste", "cc", "wayland-scanner", "pkg-config")
HERE = Path(__file__).parent / "wayland"
CONFIG = """output HEADLESS-1 resolution 1600x900
xwayland disable
for_window [title="ccdesk-gtk"] floating enable, move position 300 200
"""


def _skip_reason() -> str | None:
    missing = [t for t in TOOLS if not shutil.which(t)]
    if missing:
        return f"missing {', '.join(missing)}"
    probe = "import gi; gi.require_version('Gtk', '3.0'); gi.require_version('Atspi', '2.0')"
    if not os.path.exists(PY) or subprocess.run([PY, "-c", probe], capture_output=True).returncode:
        return f"{PY} has no PyGObject with GTK 3 and Atspi"
    try:
        A11y().apps()
    except CcdeskError as exc:
        return f"no AT-SPI bus: {exc}"
    return None


pytestmark = pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")


def _wait(pred, what, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(0.05)
    pytest.fail(f"timed out waiting for {what}")


@pytest.fixture(scope="session")
def vpointer(tmp_path_factory):
    """Compile tests/wayland/vpointer.c, which keeps a virtual pointer plugged in."""
    out = tmp_path_factory.mktemp("vpointer")
    xml = HERE / "wlr-virtual-pointer-unstable-v1.xml"
    header = out / "wlr-virtual-pointer-unstable-v1-client-protocol.h"
    code = out / "protocol.c"
    subprocess.run(["wayland-scanner", "client-header", str(xml), str(header)], check=True)
    subprocess.run(["wayland-scanner", "private-code", str(xml), str(code)], check=True)
    flags = subprocess.run(["pkg-config", "--cflags", "--libs", "wayland-client"], capture_output=True,
                           text=True, check=True).stdout.split()
    exe = out / "vpointer"
    subprocess.run(["cc", "-O1", "-I", str(out), str(HERE / "vpointer.c"), str(code), "-o", str(exe), *flags],
                   check=True)
    return exe


@pytest.fixture
def sway(tmp_path, vpointer):
    # A short runtime dir: sway's IPC socket path must fit in sun_path (108 bytes).
    run = tempfile.mkdtemp(prefix="ccwl-")
    (tmp_path / "config").write_text(CONFIG)
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY", "SWAYSOCK")}
    env.update(XDG_RUNTIME_DIR=run, WLR_BACKENDS="headless", WLR_RENDERER="pixman",
               LIBSEAT_BACKEND="noop", WLR_LIBINPUT_NO_DEVICES="1")
    proc = subprocess.Popen(["sway", "-c", str(tmp_path / "config")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sock = _wait(lambda: next(iter(Path(run).glob("sway-ipc.*.sock")), None), "sway")
    display = _wait(lambda: next((p.name for p in Path(run).glob("wayland-*") if not p.name.endswith(".lock")),
                                 None), "the Wayland socket")
    env = dict(env, WAYLAND_DISPLAY=display, SWAYSOCK=str(sock))
    pointer = subprocess.Popen([str(vpointer)], env=env, stdout=subprocess.PIPE, text=True)
    assert pointer.stdout.readline().strip() == "ready"
    yield env
    pointer.terminate()
    proc.terminate()
    proc.wait(5)
    shutil.rmtree(run, ignore_errors=True)


@pytest.fixture
def gtk(sway, tmp_path):
    state = tmp_path / "state.json"
    proc = subprocess.Popen([PY, str(APP), str(state)], env=dict(sway, GDK_BACKEND="wayland", GNOME_ACCESSIBILITY="1"))

    def read():
        try:
            return json.loads(state.read_text())
        except (OSError, ValueError):
            return {}

    def wait_for(pred):
        return _wait(lambda: (lambda s: s if s and pred(s) else None)(read()), f"app state; last {read()}")

    wait_for(lambda s: s.get("ready"))
    yield wait_for
    proc.terminate()
    proc.wait(5)


@pytest.fixture
def desk(sway, tmp_path):
    return Desktop(backend=WaylandBackend(env=sway), policy=Policy(), audit=AuditLog(tmp_path / "audit.log"),
                   shots_dir=tmp_path / "shots")


def ref_of(tree: str, pattern: str) -> str:
    m = re.search(rf"^\s*(e\d+) {pattern}", tree, re.M)
    assert m, f"{pattern!r} not in tree:\n{tree}"
    return m.group(1)


def test_screen_windows_and_clipboard(gtk, desk):
    assert desk.doctor()["ready"]
    assert desk.screen_size() == (1600, 900)
    shot = desk.screenshot(region="300,200,200,100")
    assert png_size(shot["path"]) == (200, 100)
    win = next(w for w in desk.list_windows()["windows"] if w["title"] == "ccdesk-gtk")
    assert win["geometry"]["x"] == 300 and 200 <= win["geometry"]["y"] <= 240  # below the title bar
    desk.clipboard(action="set", text="from ccdesk")
    assert desk.clipboard()["text"] == "from ccdesk"


def test_tree_coordinates_are_on_screen(gtk, desk):
    r = desk.ui_tree(window="ccdesk-gtk")
    assert r["origin"]["x"] >= 300 and r["origin"]["y"] >= 200, r  # window-relative boxes were shifted
    assert "note" not in r
    tree = r["tree"]

    desk.click_element(ref_of(tree, r'(?:push )?button "Apply settings"'))
    gtk(lambda s: s["clicks"] == 1)

    desk.click_element(ref_of(tree, r'text "Display name"'))
    typed = desk.type_text("Sara")
    gtk(lambda s: s["entry"] == "Sara")
    assert '="Sara"' in typed["changes"], typed

    toggled = desk.click_element(ref_of(tree, r'check box "I agree"'))
    gtk(lambda s: s["agree"])
    assert "checked" in toggled["changes"], toggled


def test_coordinate_click(gtk, desk):
    tree = desk.ui_tree(window="ccdesk-gtk")["tree"]
    box = re.search(r'button "Apply settings" @(\d+),(\d+) (\d+)x(\d+)', tree)
    x, y, w, h = map(int, box.groups())
    desk.click(x + w // 2, y + h // 2)
    gtk(lambda s: s["clicks"] == 1)


def test_virtual_pointer_clicks_scrolls_and_drags(gtk, sway, desk, tmp_path):
    """Without SWAYSOCK the backend can't use sway's seat, so it speaks the protocol itself."""
    tree = desk.ui_tree(window="ccdesk-gtk")["tree"]
    env = {k: v for k, v in sway.items() if k != "SWAYSOCK"}
    other = Desktop(backend=WaylandBackend(env=env), policy=Policy(), audit=AuditLog(tmp_path / "a.log"),
                    shots_dir=tmp_path / "s2")

    def centre(pattern):
        x, y, w, h = map(int, re.search(rf'{pattern}.*?@(\d+),(\d+) (\d+)x(\d+)', tree).groups())
        return x, y, w, h

    x, y, w, h = centre(r'button "Apply settings"')
    other.click(x + w // 2, y + h // 2)
    gtk(lambda s: s["clicks"] == 1)
    other.click(x + w // 2, y + h // 2, count=2)
    gtk(lambda s: s["clicks"] == 3)

    x, y, w, h = centre(r'"Level"')
    other.scroll(x + w // 2, y + h // 2, dy=-3)
    level = gtk(lambda s: s["level"] != 50)["level"]
    other.drag(x + w // 2, y + h // 2, x + w - 2, y + h // 2, duration_ms=200)
    gtk(lambda s: s["level"] > level)


@pytest.mark.xfail(reason="on a headless sway with only virtual devices, a named key sent by wtype on "
                          "its own (Left, BackSpace) never reaches GTK 3; text does. Unverified on a real "
                          "sway session", strict=False)
def test_named_key(gtk, desk):
    tree = desk.ui_tree(window="ccdesk-gtk")["tree"]
    desk.click_element(ref_of(tree, r'text "Display name"'))
    desk.type_text("Sara")
    desk.press_key("Left")
    desk.type_text("X")
    gtk(lambda s: s["entry"] == "SarXa")
