"""An own-pointer agent works in a window behind the user's without activating it: a private X
screen with a window manager, two GTK windows, the user's in front. While the agent clicks a button,
sets a field and types into it, the active window (_NET_ACTIVE_WINDOW), the stacking order and the
user's keyboard focus stay as they were. Safe on a real desktop: it never uses $DISPLAY.

CuaDriverBench saw the opposite on xfwm4: activate_window sent XISetFocus, and the window manager
made the agent's window active and raised it over the user's.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time

import pytest

from ccdesk.backends import X11Backend
from ccdesk.backends.mpx import Mpx
from ccdesk.headless import find_libexec
from tests.test_integration_a11y import APP as FORM_APP
from tests.test_integration_a11y import ref_of
from tests.test_integration_multiagent import APP as TEXT_APP
from tests.test_integration_multiagent import PY, _skip_reason, screen, wait_for  # noqa: F401
from tests.test_integration_own_pointer import bspc, core_focus


def _reason() -> str | None:
    if _skip_reason():
        return _skip_reason()
    for tool in ("xinput", "xprop"):
        if not shutil.which(tool):
            return f"{tool} not on PATH"
    if not find_libexec("at-spi2-registryd"):
        return "at-spi2-core not installed"
    probe = "import gi; gi.require_version('Atspi', '2.0')"
    if subprocess.run([PY, "-c", probe], capture_output=True).returncode:
        return f"{PY} has no Atspi"
    return None


pytestmark = pytest.mark.skipif(_reason() is not None, reason=_reason() or "")


def root_prop(env, name: str) -> str:
    out = subprocess.run(["xprop", "-root", name], env=env, capture_output=True, text=True).stdout
    return out.split("#", 1)[-1].strip() if "#" in out else out.strip()


def test_element_actions_leave_the_active_window_and_the_stacking_alone(screen, tmp_path):
    from ccdesk.backends.x11_own import OwnPointerBackend
    from ccdesk.desktop import Desktop
    from ccdesk.safety import AuditLog

    hl, env, procs = screen
    if hl.info().get("window_manager") != "bspwm" or not shutil.which("bspc"):
        pytest.skip("needs bspwm as the headless window manager (floating windows that overlap)")
    backend = X11Backend(env=env)
    app_env = dict(env, GDK_BACKEND="x11", GNOME_ACCESSIBILITY="1")

    form_state, typed = tmp_path / "form.json", tmp_path / "user.txt"
    procs.append(subprocess.Popen([PY, str(FORM_APP), str(form_state)], env=app_env))
    agent_id = wait_for(lambda: next((w.id for w in backend.list_windows() if w.title == "ccdesk-gtk"), None))
    procs.append(subprocess.Popen([PY, str(TEXT_APP), str(typed)], env=app_env))
    user_id = wait_for(lambda: next((w.id for w in backend.list_windows() if w.title == "ccdesk-text"), None))
    wait_for(typed.exists)

    def form():
        try:
            return json.loads(form_state.read_text())
        except (OSError, ValueError):
            return {}

    wait_for(lambda: form().get("ready"))
    for wid in (agent_id, user_id):
        bspc(env, "node", wid, "-t", "floating")
    time.sleep(0.3)
    for wid in (agent_id, user_id):  # overlapping: the user's window hides most of the agent's
        backend.move_window(next(w for w in backend.list_windows() if w.id == wid), 100, 100)
    time.sleep(0.3)
    bspc(env, "node", user_id, "-f")  # the user works in front
    time.sleep(0.4)

    active = root_prop(env, "_NET_ACTIVE_WINDOW")
    stacking = root_prop(env, "_NET_CLIENT_LIST_STACKING")
    user_focus = core_focus(env)
    assert user_focus == int(user_id, 16)
    assert int(active, 16) == int(user_id, 16), active

    mpx = Mpx("bes", env)
    mpx.create()
    try:
        desk = Desktop(backend=OwnPointerBackend(mpx, env=env), agent="bes",
                       audit=AuditLog(tmp_path / "audit.log"), shots_dir=tmp_path / "shots", screen_diff=False)
        desk.a11y.env = env

        def unchanged(step: str) -> None:
            time.sleep(0.3)  # xfwm4 raised 30 to 130 ms after the focus call
            assert root_prop(env, "_NET_ACTIVE_WINDOW") == active, step
            assert root_prop(env, "_NET_CLIENT_LIST_STACKING") == stacking, step
            assert core_focus(env) == user_focus, step
            assert mpx.query_focus() is None, step  # the agent's keyboard was never pointed anywhere

        tree = wait_for(lambda: desk.ui_tree(window="ccdesk-gtk"))["tree"]
        assert desk._target is None
        desk.click_element(ref_of(tree, r'(?:push )?button "Apply settings"'))
        wait_for(lambda: form().get("clicks") == 1)
        assert desk._target is not None and desk._target.id == agent_id
        unchanged("click_element")

        combo = ref_of(tree, r'combo box "Priority"')
        assert desk.set_value(combo, "low")["verified"] is True  # an option by name, no popup
        wait_for(lambda: form().get("priority") == "Low")
        unchanged("set_value on a combo box")

        entry = ref_of(tree, r'(?:text|entry) "Display name"')
        assert desk.set_value(entry, "Ana")["verified"] is True
        wait_for(lambda: form().get("entry") == "Ana")
        unchanged("set_value")

        r = desk.type_text(" Ñandú")
        assert r["via"] == "accessibility", r
        time.sleep(0.5)
        assert form().get("entry") == "Ana Ñandú", form()
        unchanged("type_text")

        # The user's own typing still reaches the user's window.
        subprocess.run(["xdotool", "type", "--delay", "20", "mine"], env=env, check=True)
        wait_for(lambda: "mine" in typed.read_text())
        assert form()["entry"] == "Ana Ñandú"
    finally:
        mpx.close()


def test_a_ref_of_a_second_window_acts_there_with_nothing_sent(screen, tmp_path):
    """Bench pilot round 2: after working in window A, an element of window B's tree used to take
    the pointer path and be refused as covered. Now it acts in B through accessibility, B becomes
    the agent's window, and the active window, stacking and focus stay the user's."""
    from pathlib import Path

    from ccdesk.backends.x11_own import OwnPointerBackend
    from ccdesk.desktop import Desktop
    from ccdesk.safety import AuditLog

    hl, env, procs = screen
    if hl.info().get("window_manager") != "bspwm" or not shutil.which("bspc"):
        pytest.skip("needs bspwm as the headless window manager (floating windows that overlap)")
    backend = X11Backend(env=env)
    app_env = dict(env, GDK_BACKEND="x11", GNOME_ACCESSIBILITY="1")
    form_state, other_state, typed = tmp_path / "form.json", tmp_path / "b.json", tmp_path / "user.txt"
    procs.append(subprocess.Popen([PY, str(FORM_APP), str(form_state)], env=app_env))
    a_id = wait_for(lambda: next((w.id for w in backend.list_windows() if w.title == "ccdesk-gtk"), None))
    procs.append(subprocess.Popen([PY, str(Path(__file__).with_name("cua_gtk_app.py")), str(other_state), "b"],
                                  env=app_env))
    b_id = wait_for(lambda: next((w.id for w in backend.list_windows() if w.title == "ccdesk-cua-b"), None))
    procs.append(subprocess.Popen([PY, str(TEXT_APP), str(typed)], env=app_env))
    user_id = wait_for(lambda: next((w.id for w in backend.list_windows() if w.title == "ccdesk-text"), None))
    wait_for(typed.exists)

    def read(path):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    wait_for(lambda: read(form_state).get("ready") and read(other_state).get("ready"))
    for wid in (a_id, b_id, user_id):
        bspc(env, "node", wid, "-t", "floating")
    time.sleep(0.3)
    for wid, x in ((a_id, 100), (b_id, 160), (user_id, 120)):  # all overlapping, the user's on top
        backend.move_window(next(w for w in backend.list_windows() if w.id == wid), x, 100)
    time.sleep(0.3)
    bspc(env, "node", user_id, "-f")
    time.sleep(0.4)
    active = root_prop(env, "_NET_ACTIVE_WINDOW")
    stacking = root_prop(env, "_NET_CLIENT_LIST_STACKING")
    user_focus = core_focus(env)
    assert int(active, 16) == int(user_id, 16)

    mpx = Mpx("bes2", env)
    mpx.create()
    try:
        desk = Desktop(backend=OwnPointerBackend(mpx, env=env), agent="bes2",
                       audit=AuditLog(tmp_path / "audit.log"), shots_dir=tmp_path / "shots", screen_diff=False)
        desk.a11y.env = env
        clicks = []
        real_click = desk.backend.click
        desk.backend.click = lambda *a: (clicks.append(a), real_click(*a))

        def unchanged(step: str) -> None:
            time.sleep(0.3)
            assert root_prop(env, "_NET_ACTIVE_WINDOW") == active, step
            assert root_prop(env, "_NET_CLIENT_LIST_STACKING") == stacking, step
            assert core_focus(env) == user_focus, step
            assert mpx.query_focus() is None, step
            assert clicks == [], step

        tree_a = wait_for(lambda: desk.ui_tree(window="ccdesk-gtk"))["tree"]
        desk.click_element(ref_of(tree_a, r'(?:push )?button "Apply settings"'))
        wait_for(lambda: read(form_state).get("clicks") == 1)
        assert desk._target.id == a_id
        unchanged("click_element in A")

        tree_b = wait_for(lambda: desk.ui_tree(window="ccdesk-cua-b"))["tree"]
        r = desk.click_element(ref_of(tree_b, r'(?:push )?button "Count"'))
        wait_for(lambda: read(other_state).get("clicks") == 1)
        assert desk._target.id == b_id and "your window is now 'ccdesk-cua-b'" in r["note"]
        unchanged("click_element in B")

        # back to a ref of A's older tree: it acts in A again
        entry = ref_of(tree_a, r'(?:text|entry) "Display name"')
        assert desk.set_value(entry, "Ana")["verified"] is True
        wait_for(lambda: read(form_state).get("entry") == "Ana")
        assert desk._target.id == a_id
        unchanged("set_value in A from its older tree")
    finally:
        mpx.close()
