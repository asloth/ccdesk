"""click_element on an item without an AT-SPI action selects it through its parent's Selection."""
from __future__ import annotations

import pytest

from ccdesk.errors import CcdeskError

SAVE = "3.0.29.5"  # e4, the push button of the fake form


@pytest.fixture
def desk(make_desk, fake, helper):
    d = make_desk()
    d.ui_tree(window="Firefox")
    fake.calls.clear()
    return d


def test_no_action_but_a_selection_selects_it_without_the_pointer(desk, fake, helper):
    helper.selections[SAVE] = {"selected": True}
    r = desk.click_element("e4")
    assert r["selected"] == "push button 'Save'" and r["via"] == "accessibility"
    assert [q["op"] for q in helper.requests if q["op"] in ("action", "select")] == ["action", "select"]
    assert not [c for c in fake.calls if c[0] == "click"]


def test_an_action_still_wins(desk, fake, helper):
    helper.actions[SAVE] = "click"
    helper.selections[SAVE] = {"selected": True}
    assert desk.click_element("e4")["activated"] == "click"
    assert not [q for q in helper.requests if q["op"] == "select"]


def test_without_a_selection_it_falls_back_to_the_pointer(desk, fake, helper):
    r = desk.click_element("e4")  # the fake answers {"selected": None}
    assert "clicked" in r and [c for c in fake.calls if c[0] == "click"]


def test_a_selection_that_does_not_take_is_reported_and_not_clicked(desk, fake, helper):
    helper.selections[SAVE] = {"selected": False}
    with pytest.raises(CcdeskError, match="did not become selected"):
        desk.click_element("e4")
    assert not [c for c in fake.calls if c[0] == "click"]
