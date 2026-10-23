"""Accessibility tree tools against a fake helper (no AT-SPI bus needed)."""

import json

import pytest

from ccdesk.a11y import A11y, format_node
from ccdesk.errors import CcdeskError, PolicyError, UnsupportedError


def test_ui_tree_text(make_desk, helper, read_audit):
    r = make_desk().ui_tree(page=True)
    assert r["window"] == "Form — Mozilla Firefox" and r["count"] == 4 and not r["truncated"]
    assert r["tree"].splitlines() == [
        'e1 document web "Form" [focused] @4,90 1280x886',
        '  e2 entry "Feedback email" ="me@x.org" @135,179 148x21',
        '      e3 check box "Peru" [checked] @32,232 14x14',
        '  e4 push button "Save" @80,263 38x22',
    ]
    assert r["approx_tokens"] == len(r["tree"]) // 4
    assert helper.requests[-1] == {"op": "tree", "window": None, "query": None, "page": True, "max_nodes": 300}
    logged = read_audit()[-1]
    assert logged["status"] == "ok" and "tree" not in logged["result"]


def test_ui_tree_query_is_flat_and_truncation_noted(make_desk):
    d = make_desk()
    assert d.ui_tree(query="save")["tree"] == 'e1 push button "Save" @80,263 38x22'
    r = d.ui_tree(max_nodes=2)
    assert r["truncated"] and "query" in r["note"]


def test_click_element_clicks_centre(make_desk, fake, read_audit):
    d = make_desk()
    d.ui_tree()
    r = d.click_element("e4")
    assert fake.calls[-1] == ("click", 99, 274, 1, 1)
    assert r["element"] == "push button 'Save'"
    assert read_audit()[-1]["tool"] == "click_element"


def test_click_element_refuses_stale_or_hidden(make_desk, fake, helper):
    d = make_desk()
    with pytest.raises(CcdeskError, match="call ui_tree"):
        d.click_element("e1")  # no tree yet
    d.ui_tree()
    helper.nodes["3.0.29.5"]["name"] = "Saving…"
    with pytest.raises(CcdeskError, match="changed"):
        d.click_element("e4")
    helper.nodes["3.0.29.3.1.0"]["showing"] = False
    with pytest.raises(CcdeskError, match="not on screen"):
        d.click_element("e3")
    del helper.nodes["3.0.29.2"]
    with pytest.raises(CcdeskError, match="gone"):
        d.click_element("e2")
    assert not [c for c in fake.calls if c[0] == "click"]


def test_click_element_respects_read_only(make_desk, fake):
    d = make_desk(read_only=True)
    d.ui_tree()  # reading stays allowed
    with pytest.raises(PolicyError):
        d.click_element("e4")
    assert fake.calls == []


def test_helper_python_fallback(tmp_path):
    calls = []

    def runner(argv, timeout):
        calls.append(argv[0])
        if argv[0] == "/bin/false":
            return json.dumps({"error": "PyGObject with Atspi is not available: no gi"})
        return json.dumps({"apps": []})

    a = A11y(runner=runner, pythons=("/bin/false", "/bin/true"))
    assert a.apps() == []
    assert calls == ["/bin/false", "/bin/true"]
    a.apps()
    assert calls[-1] == "/bin/true"  # remembered

    bad = A11y(runner=lambda argv, timeout: json.dumps({"error": "PyGObject missing"}), pythons=("/bin/true",))
    with pytest.raises(UnsupportedError, match="python-gobject"):
        bad.apps()


def test_format_node_escapes():
    line = format_node("e9", {"role": "text", "name": 'say "hi"', "value": "a\nb"}, 1)
    assert line == '  e9 text "say \\"hi\\"" ="a\\nb"'


def test_ui_tree_warns_when_several_windows_match():
    def runner(argv, timeout):
        return json.dumps({"window": "Signup", "app": "a", "nodes": [], "truncated": False, "matches": 2})

    r = A11y(runner=runner, pythons=("/bin/sh",)).tree("Signup", None, False, 300)
    assert "2 windows match 'Signup'" in r["warning"]
    one = A11y(runner=lambda a, t: json.dumps({"window": "x", "app": "a", "nodes": [], "truncated": False}),
               pythons=("/bin/sh",)).tree(None, None, False, 300)
    assert "warning" not in one


@pytest.fixture
def no_settle(monkeypatch):
    monkeypatch.setattr("ccdesk.desktop.SETTLE", 0)


def test_refs_stay_stable_across_trees(make_desk, helper):
    d = make_desk()
    first = d.ui_tree()["tree"]
    helper.nodes["9.9"] = {"path": "9.9", "role": "push button", "name": "New", "depth": 6, "showing": True,
                           "box": [1, 1, 10, 10]}
    second = d.ui_tree()["tree"]
    assert second.startswith(first)  # old elements keep e1..e4, the new one gets e5
    assert 'e5 push button "New"' in second


def test_actions_report_changes_after_ui_tree(make_desk, helper, no_settle):
    d = make_desk()
    assert "changes" not in d.type_text("x")  # no ui_tree yet: nothing to compare with
    d.ui_tree()
    helper.nodes["3.0.29.2"]["value"] = "ana@example.org"
    r = d.click_element("e2")
    assert r["changes"] == 'e2 entry "Feedback email" ="ana@example.org" @135,179 148x21'
    assert d.press_key("Tab")["changes"].startswith("no change")
    del helper.nodes["3.0.29.2"]
    assert d.type_text("y")["changes"] == "gone: e2"


def test_many_changes_ask_for_a_new_tree(make_desk, helper, no_settle):
    d = make_desk()
    d.ui_tree()
    for i in range(30):
        helper.nodes[f"8.{i}"] = {"path": f"8.{i}", "role": "link", "name": f"l{i}", "depth": 1, "showing": True}
    assert "call ui_tree()" in d.click(5, 5)["changes"]


IS_ENABLED = "busctl --user get-property org.a11y.Bus /org/a11y/bus org.a11y.Status IsEnabled"
ENABLE = "busctl --user set-property org.a11y.Bus /org/a11y/bus org.a11y.Status IsEnabled b true"
OFF_BUS = [{"name": "lxpolkit", "pid": 7, "windows": []}, {"name": "Telegram", "pid": 8, "windows": ["Telegram"]}]


def test_ui_tree_names_the_missing_window_and_the_fix(make_desk, helper, fake):
    """Real session: Firefox on bspwm, gsettings on but the bus's IsEnabled false."""
    helper.apps, helper.tree_error = OFF_BUS, "no active accessible window; pass window=... (windows: 'Telegram')"
    fake.active = "0x01e00003"
    fake.responses[IS_ENABLED] = "b false\n"
    with pytest.raises(CcdeskError) as err:
        make_desk().ui_tree(page=True)
    msg = str(err.value)
    assert "'Mozilla Firefox' (Navigator.firefox) is not on the accessibility bus" in msg
    assert "on the bus: Telegram" in msg
    assert "IsEnabled is false" in msg and ENABLE in msg and "restart" in msg
    assert "screenshot" in msg


def test_ui_tree_diagnosis_by_title_and_toolkit(make_desk, helper, fake):
    helper.apps, helper.tree_error = OFF_BUS, "no accessible window title contains 'termi'; windows: 'Telegram'"
    fake.responses[IS_ENABLED] = "b true\n"
    with pytest.raises(CcdeskError) as err:
        make_desk().ui_tree(window="termi")
    msg = str(err.value)
    assert "'Terminal' (xterm.XTerm) is not on the accessibility bus" in msg and ENABLE not in msg
    assert "GNOME_ACCESSIBILITY=1" in msg

    fake.windows[0].wm_class = "chromium.Chromium"
    fake.active = "0x01e00003"
    helper.tree_error = "no active accessible window; pass window=..."
    with pytest.raises(CcdeskError, match="--force-renderer-accessibility"):
        make_desk().ui_tree()


def test_ui_tree_diagnosis_without_a_known_window(make_desk, helper, fake):
    helper.apps, helper.tree_error = [], "no app has a window on the accessibility bus"
    with pytest.raises(CcdeskError) as err:
        make_desk().ui_tree()
    assert str(err.value).startswith("no app has a window on the accessibility bus")
    assert "is not on the accessibility bus" not in str(err.value)


def test_doctor_checks_the_bus_switch(make_desk, helper, fake):
    helper.apps = OFF_BUS
    fake.responses[IS_ENABLED] = "b false\n"
    report = make_desk().doctor()
    assert report["a11y"]["bus_enabled"] is False
    warning = next(w for w in report["warnings"] if "IsEnabled" in w)
    assert ENABLE in warning and "restart" in warning
    fake.responses[IS_ENABLED] = "b true\n"
    report = make_desk().doctor()
    assert report["a11y"]["bus_enabled"] is True and not any("IsEnabled" in w for w in report["warnings"])
    del fake.responses[IS_ENABLED]
    assert make_desk().doctor()["a11y"]["bus_enabled"] is None


def test_doctor_text_shows_the_bus_switch(make_desk, helper, fake):
    from ccdesk.doctor import format_report

    fake.responses[IS_ENABLED] = "b false\n"
    text = format_report(make_desk().doctor())
    assert "a11y bus:     off (org.a11y.Status IsEnabled=false)" in text
    assert ENABLE in text


def test_window_relative_boxes_are_shifted(make_desk, helper, fake):
    """Wayland toolkits put their own frame at 0,0; the compositor says where it really is."""
    real = helper.__call__

    def wayland(argv, timeout):
        out = json.loads(real(argv, timeout))
        if "nodes" in out:
            out["frame_box"] = [0, 0, 1280, 960]
        return json.dumps(out)

    d = make_desk()
    d.a11y._runner = wayland
    d.a11y.locate = lambda pid, title: (100, 50)
    r = d.ui_tree(query="save")
    assert r["tree"] == 'e1 push button "Save" @180,313 38x22' and r["origin"] == {"x": 100, "y": 50}
    d.click_element("e1")
    assert fake.calls[-1] == ("click", 199, 324, 1, 1)
