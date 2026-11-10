"""What an action shows of its own effect: tree or pixels, decided per action, and the picture."""

from __future__ import annotations

import pytest

from ccdesk.errors import ValidationError  # noqa: F401  (used by the shot= tests below)
from tests.test_screen_changes import FIREFOX, WHITE  # noqa: F401

TERMINAL = "0x02000001"
BAD_BUS = "no app has a window on the accessibility bus"


@pytest.fixture(autouse=True)
def fast_settle(monkeypatch):
    monkeypatch.setattr("ccdesk.desktop.SETTLE", 0)


# -- a stale tree must not hide the pixel result ----------------------------------------


def test_an_action_in_another_window_than_the_tree_reports_pixels(make_desk, fake, helper):
    d = make_desk(screen_diff=True)
    d.ui_tree()  # the tree is of "Form — Mozilla Firefox"
    fake.under = TERMINAL  # the Terminal is 10,10 300x200
    fake.effects = [(50, 50, 120, 40, WHITE)]
    r = d.click(60, 60)
    assert r["screen_changed"] is True and r["changed_area"]["width"] >= 120
    assert "changes" not in r


def test_an_action_in_the_trees_window_still_reports_the_tree(make_desk, fake, helper):
    d = make_desk(screen_diff=True)
    d.ui_tree()
    fake.under = FIREFOX
    helper.nodes["3.0.29.2"]["value"] = "ana@example.org"
    r = d.click(250, 320)
    assert "changes" in r and "screen_changed" not in r


def test_a_tree_that_cannot_be_read_falls_back_to_the_pixels(make_desk, fake, helper):
    """Real log: a click in Firefox answered `could not re-read the window: no app has a window
    on the accessibility bus` and nothing about the screen."""
    d = make_desk(screen_diff=True)
    d.ui_tree()
    fake.under = FIREFOX
    helper.tree_error = BAD_BUS
    fake.effects = [(200, 300, 120, 40, WHITE)]
    r = d.click(250, 320)
    assert r["screen_changed"] is True and "changed_area" in r
    assert "changes" not in r and BAD_BUS not in str(r)


def test_after_a_failed_re_read_the_next_actions_use_the_pixels_at_once(make_desk, fake, helper):
    d = make_desk(screen_diff=True)
    d.ui_tree()
    fake.under = FIREFOX
    helper.tree_error = BAD_BUS
    d.click(250, 320)
    helper.requests.clear()
    fake.effects = [(10, 10, 50, 20, WHITE)]
    r = d.click(250, 320)
    assert r["screen_changed"] is True and "changes" not in r
    assert not [q for q in helper.requests if q["op"] == "tree"]  # no second 1-second re-read


def test_reading_the_tree_again_brings_the_tree_back(make_desk, fake, helper):
    d = make_desk(screen_diff=True)
    d.ui_tree()
    fake.under = FIREFOX
    helper.tree_error = BAD_BUS
    d.click(250, 320)
    helper.tree_error = None
    d.ui_tree()
    r = d.click(250, 320)
    assert "changes" in r and "screen_changed" not in r


def test_without_a_baseline_a_failed_re_read_says_so_in_a_note_not_in_changes(make_desk, fake, helper):
    d = make_desk()  # screen_diff off: no pixels to fall back to
    d.ui_tree()
    helper.tree_error = BAD_BUS
    r = d.click(5, 5)
    assert "changes" not in r and BAD_BUS in r["note"]


def test_a_failed_re_read_keeps_the_password_guard(make_desk, fake, helper):
    """The tree is kept, not forgotten, so the focused password field is still known."""
    d = make_desk(screen_diff=True)
    helper.nodes["3.0.29.2"].update(role="password text", states=["focused"])
    d.ui_tree()
    fake.under = FIREFOX
    helper.tree_error = BAD_BUS
    d.click(250, 320)
    from ccdesk.errors import PolicyError
    with pytest.raises(PolicyError, match="password"):
        d.type_text("hunter2")
