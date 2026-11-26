"""Public Calc routes must bypass reveal/pointer/type fallbacks, including on failure."""
import pytest

from ccdesk.errors import CcdeskError, ValidationError

PATH = "3.0.29.5"


@pytest.fixture
def calc_desk(make_desk, helper, fake, monkeypatch):
    helper.nodes[PATH].update(role="table", name="Sheet1", showing=False, box=None, states=["offscreen"])
    desk = make_desk()
    desk.ui_tree(window="Firefox")
    fake.calls.clear()
    def fail(*args, **kwargs):
        raise AssertionError("Calc must never reveal, move the cursor, or type")
    monkeypatch.setattr(desk, "_reveal_element", fail)
    monkeypatch.setattr(desk, "_cursor_to_node", fail)
    monkeypatch.setattr(desk, "_type_into", fail)
    return desk


def test_public_write_forwards_address_and_clipboard_note(calc_desk, helper, fake):
    helper.values[PATH] = {"verified": True, "note": "clipboard was used briefly"}
    result = calc_desk.set_value("e4", "text", cell="B7")
    assert result["verified"] and result["note"] == "clipboard was used briefly"
    assert next(q for q in helper.requests if q["op"] == "set")["cell"] == "B7"
    assert not [c for c in fake.calls if c[0] in ("click", "press_key", "type_text")]


def test_single_cell_write_has_no_address_or_fallback(calc_desk, helper):
    helper.nodes[PATH].update(role="table cell", calc_cell=True)
    calc_desk.ui_tree(window="Firefox")
    ref = next(k for k, v in calc_desk.a11y.refs.items() if v.role == "table cell")
    assert calc_desk.set_value(ref, "42")["verified"]
    assert "cell" not in next(q for q in helper.requests if q["op"] == "set")


@pytest.mark.parametrize("answer", [{"verified": False, "refused": True}, {"error": "readback mismatch"}])
def test_failed_write_cannot_type(calc_desk, helper, fake, answer):
    helper.values[PATH] = answer
    with pytest.raises(CcdeskError):
        calc_desk.set_value("e4", "text", cell="B7")
    assert not [c for c in fake.calls if c[0] in ("click", "press_key", "type_text")]


def test_distant_click_uses_exclusive_selection(calc_desk, monkeypatch, fake):
    calls = []
    monkeypatch.setattr(calc_desk.a11y, "calc_select", lambda ref, cell=None: calls.append((ref, cell)) or {"selected": True})
    assert calc_desk.click_element("e4", cell="AA1001")["via"] == "accessibility"
    assert calls == [("e4", "AA1001")]
    assert not [c for c in fake.calls if c[0] == "click"]


@pytest.mark.parametrize("kwargs", [{"button": "right"}, {"count": 2}, {"expect_title": "Firefox"}])
def test_address_never_turns_into_pointer_click(calc_desk, kwargs):
    with pytest.raises(ValidationError):
        calc_desk.click_element("e4", cell="B7", **kwargs)


def test_generic_table_cells_keep_their_pointer_route(make_desk, helper, fake):
    helper.nodes[PATH]["role"] = "table cell"
    desk = make_desk()
    desk.ui_tree(window="Firefox")
    fake.calls.clear()
    desk.click_element("e4")
    assert [c for c in fake.calls if c[0] == "click"]
