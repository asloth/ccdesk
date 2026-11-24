"""Reveal and fill below-fold content on a private Xvfb screen without activation."""
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from ccdesk.backends import X11Backend
from ccdesk.desktop import Desktop
from ccdesk.safety import AuditLog
from tests.test_integration_a11y import ref_of
from tests.test_integration_beside_user import root_prop
from tests.test_integration_multiagent import PY, _skip_reason, screen, wait_for  # noqa: F401
from tests.test_integration_own_pointer import bspc, core_focus

pytestmark = pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")


@pytest.mark.parametrize("action", ["set_value", "click_element"])
def test_offscreen_field_is_listed_revealed_set_and_read_without_input(screen, tmp_path, action):
    hl, env, procs = screen
    if hl.info().get("window_manager") != "bspwm" or not shutil.which("bspc"):
        pytest.skip("needs bspwm to keep the user's window active")
    backend = X11Backend(env=env)
    app_env = dict(env, GDK_BACKEND="x11", GNOME_ACCESSIBILITY="1")
    if not shutil.which("firefox"):
        pytest.skip("needs Firefox for Component scrolling")
    page = tmp_path / "form.html"
    page.write_text('<title>ccdesk-scroll-page</title><div style="height:2500px">Top</div>'
                    '<label>Below fold<input type="range" min="0" max="100" value="10" aria-label="Below fold"></label>'
                    '<button onclick="document.getElementById(\'result\').textContent=\'clicked\'">Below fold button</button>'
                    '<p id="result">idle</p>')
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "user.js").write_text('user_pref("accessibility.force_disabled", -1);\n'
                                   'user_pref("browser.shell.checkDefaultBrowser", false);\n'
                                   'user_pref("browser.startup.homepage_override.mstone", "ignore");\n')
    procs.append(subprocess.Popen(["firefox", "--no-remote", "-profile", str(profile), page.as_uri()],
                                 env=app_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    agent_id = wait_for(lambda: next((w.id for w in backend.list_windows()
                                     if "ccdesk-scroll-page" in w.title), None), timeout=30)
    user_state = tmp_path / "user.txt"
    procs.append(subprocess.Popen([PY, str(Path(__file__).with_name("gtk_text_app.py")), str(user_state)], env=app_env))
    user_id = wait_for(lambda: next((w.id for w in backend.list_windows() if w.title == "ccdesk-text"), None))
    wait_for(user_state.exists)
    for wid in (agent_id, user_id):
        bspc(env, "node", wid, "-t", "floating")
    agent_win = next(w for w in backend.list_windows() if w.id == agent_id)
    backend.resize_window(agent_win, 380, 240)
    bspc(env, "node", user_id, "-f")
    time.sleep(0.3)
    desk = Desktop(backend=backend, audit=AuditLog(tmp_path / "audit.log"),
                   shots_dir=tmp_path / "shots", screen_diff=False)
    desk.a11y.env = env
    try:
        def field():
            tree = desk.ui_tree(window="ccdesk-scroll-page", page=True, shot_fallback=False)["tree"]
            return tree if '"Below fold"' in tree else None

        tree = wait_for(field)
        pattern = r'slider "Below fold"' if action == "set_value" else r'button "Below fold button"'
        ref = ref_of(tree, pattern)
        line = next(line for line in tree.splitlines() if line.lstrip().startswith(ref + " "))
        assert "offscreen" in line, tree
        active = root_prop(env, "_NET_ACTIVE_WINDOW")
        focus = core_focus(env)
        pointer = backend.run(["xdotool", "getmouselocation", "--shell"])
        assert int(active, 16) == int(user_id, 16)
        if action == "set_value":
            result = desk.set_value(ref, "75")
            assert result["verified"] and not result.get("typed")
            assert float(desk.ui_tree(ref=ref)["value"]) == 75
        else:
            assert desk.click_element(ref)["activated"]
            assert "clicked" in desk.ui_tree(window="ccdesk-scroll-page", page=True)["tree"]
        assert desk.a11y.resolve(ref)["showing"]
        assert root_prop(env, "_NET_ACTIVE_WINDOW") == active
        assert core_focus(env) == focus
        assert backend.run(["xdotool", "getmouselocation", "--shell"]) == pointer
    finally:
        desk.a11y.close()
