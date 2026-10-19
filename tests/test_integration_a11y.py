"""Accessibility-tree tools against a real GTK app on an X server: run with `xvfb-run -a pytest -q`.

Needs a system Python with PyGObject + GTK 3 and a running AT-SPI bus (at-spi2-core).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from ccdesk.a11y import A11y
from ccdesk.backends import X11Backend
from ccdesk.desktop import Desktop
from ccdesk.errors import CcdeskError
from ccdesk.safety import AuditLog, Policy

APP = Path(__file__).with_name("gtk_app.py")
PY = os.environ.get("CCDESK_A11Y_PYTHON", "/usr/bin/python3")


def _skip_reason() -> str | None:
    if not os.environ.get("DISPLAY"):
        return "no DISPLAY (run under xvfb-run)"
    probe = "import gi; gi.require_version('Gtk', '3.0'); gi.require_version('Atspi', '2.0')"
    if not os.path.exists(PY) or subprocess.run([PY, "-c", probe], capture_output=True).returncode:
        return f"{PY} has no PyGObject with GTK 3 and Atspi"
    try:
        A11y().apps()
    except CcdeskError as exc:
        return f"no AT-SPI bus: {exc}"
    return None


pytestmark = [pytest.mark.display, pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")]


@pytest.fixture
def gtk(tmp_path):
    state = tmp_path / "state.json"
    env = dict(os.environ, GNOME_ACCESSIBILITY="1", GDK_BACKEND="x11")
    proc = subprocess.Popen([PY, str(APP), str(state)], env=env)

    def read():
        try:
            return json.loads(state.read_text())
        except (OSError, ValueError):
            return {}

    def wait_for(pred, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = read()
            if st and pred(st):
                return st
            time.sleep(0.05)
        pytest.fail(f"timed out; last state {read()}")

    wait_for(lambda s: s.get("ready"))
    yield wait_for
    proc.terminate()
    proc.wait(5)


@pytest.fixture
def desk(tmp_path):
    env = dict(os.environ)
    env.pop("WAYLAND_DISPLAY", None)
    return Desktop(backend=X11Backend(env=env), policy=Policy(), audit=AuditLog(tmp_path / "audit.log"),
                   shots_dir=tmp_path / "shots")


def ref_of(tree: str, pattern: str) -> str:
    m = re.search(rf"^\s*(e\d+) {pattern}", tree, re.M)
    assert m, f"{pattern!r} not in tree:\n{tree}"
    return m.group(1)


def test_tree_click_type_and_toggle(gtk, desk):
    r = desk.ui_tree(window="ccdesk-gtk")
    tree = r["tree"]
    assert r["window"] == "ccdesk-gtk" and r["approx_tokens"] < 300, tree
    desk.click_element(ref_of(tree, r'(?:push )?button "Apply settings"'))
    gtk(lambda s: s["clicks"] == 1)
    assert '"clicked 1"' in desk.ui_tree(window="ccdesk-gtk")["tree"]

    tree = desk.ui_tree(window="ccdesk-gtk")["tree"]
    desk.click_element(ref_of(tree, r'text "Display name"'))
    desk.type_text("Sara")
    gtk(lambda s: s["entry"] == "Sara")
    tree = desk.ui_tree(window="ccdesk-gtk")["tree"]
    assert re.search(r'text "Display name" ="Sara"', tree), tree

    desk.click_element(ref_of(tree, r'check box "I agree"'))
    gtk(lambda s: s["agree"])
    assert re.search(r'check box "I agree" \[[^]]*checked', desk.ui_tree(window="ccdesk-gtk", query="agree")["tree"])


def test_stale_ref_after_new_tree(gtk, desk):
    desk.ui_tree(window="ccdesk-gtk", query="agree")
    first = desk.a11y.refs["e1"]
    desk.ui_tree(window="ccdesk-gtk", query="Apply")
    assert desk.a11y.refs["e1"] != first  # refs belong to the latest tree only
