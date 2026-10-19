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
