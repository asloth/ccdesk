"""What an action shows of its own effect: tree or pixels, decided per action, and the picture."""

from __future__ import annotations

import pytest

from ccdesk.errors import ValidationError  # noqa: F401  (used by the shot= tests below)
from tests.pngutil import read_png
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


# -- a pixel action shows what it changed, once the screen has settled ----------------------------


def test_a_pixel_action_that_changed_the_screen_carries_the_picture(make_desk, fake):
    d = make_desk(screen_diff=True)
    fake.effects = [(200, 300, 120, 40, WHITE)]
    r = d.click(250, 320)
    shot = r["change_shot"]
    assert shot["origin"] == {"x": 176, "y": 280} and shot["approx_tokens"] <= 610
    assert read_png(shot["path"]).pixel(24, 24) == WHITE


def test_no_picture_when_nothing_changed(make_desk, fake):
    d = make_desk(screen_diff=True)
    r = d.click(5, 5)
    assert r["screen_changed"] is False and "change_shot" not in r and "note" not in r


@pytest.mark.parametrize("act", [
    lambda d: d.click(250, 320, show_change=False),
    lambda d: d.drag(210, 310, 250, 330, show_change=False),
    lambda d: d.scroll(250, 320, dy=3, show_change=False),
    lambda d: d.type_text("x", show_change=False),
    lambda d: d.press_key("Tab", show_change=False),
    lambda d: d.batch([{"tool": "click", "x": 250, "y": 320}], show_change=False),
])
def test_show_change_false_never_attaches_it(make_desk, fake, act):
    d = make_desk(screen_diff=True)
    fake.effects = [(200, 300, 120, 40, WHITE)]
    r = act(d)
    assert r["screen_changed"] is True and "change_shot" not in r


@pytest.mark.parametrize("act", [
    lambda d: d.click(250, 320),
    lambda d: d.drag(210, 310, 250, 330),
    lambda d: d.scroll(250, 320, dy=3),
    lambda d: d.type_text("x"),
    lambda d: d.press_key("Tab"),
    lambda d: d.batch([{"tool": "click", "x": 250, "y": 320}, {"tool": "press_key", "key": "Tab"}]),
])
def test_every_pixel_action_attaches_it_by_default(make_desk, fake, act):
    d = make_desk(screen_diff=True)
    fake.effects = [(200, 300, 120, 40, WHITE)]
    assert "change_shot" in act(d)


def test_on_the_tree_path_there_is_no_picture(make_desk, fake, helper):
    d = make_desk(screen_diff=True)
    d.ui_tree()
    fake.under = FIREFOX
    fake.effects = [(200, 300, 120, 40, WHITE)]
    helper.nodes["3.0.29.2"]["value"] = "ana@example.org"
    r = d.click(250, 320)
    assert "changes" in r and "change_shot" not in r and "screen_changed" not in r


def test_the_picture_is_not_stored_in_the_audit_log(make_desk, fake, read_audit):
    d = make_desk(screen_diff=True)
    fake.effects = [(200, 300, 120, 40, WHITE)]
    d.click(250, 320)
    entry = read_audit()[-1]
    assert entry["args"]["show_change"] is None
    assert set(entry["result"]["change_shot"]) == {"path", "width", "height", "approx_tokens", "origin"}
    assert len(str(entry)) < 1500


def moving_screen(fake, moving_for):
    """A direct capture that, once the click has painted, changes on each of the next moving_for calls."""
    state = {"n": 0}

    def gray_grid(region, cell):
        if not fake.rects:
            return 10, 4, bytes(40), (0, 0)
        state["n"] += 1
        value = 100 if state["n"] > moving_for else (160 if state["n"] % 2 else 120)  # never the same twice
        gray = bytearray(40)
        for i in (11, 12, 13):
            gray[i] = value
        return 10, 4, bytes(gray), (0, 0)

    fake.gray_grid = gray_grid


def test_a_screen_still_moving_at_the_cap_says_so(make_desk, fake):
    d = make_desk(screen_diff=True)
    moving_screen(fake, moving_for=10_000)
    d.click(5, 5, show_change=False)  # learns that captures are direct
    fake.effects = [(0, 0, 10, 10, WHITE)]
    r = d.click(5, 5)
    assert r["screen_changed"] is True and r["settled"] is False and "change_shot" in r


def test_a_screen_that_stops_does_not_say_it_is_moving(make_desk, fake):
    d = make_desk(screen_diff=True)
    moving_screen(fake, moving_for=20)
    d.click(5, 5, show_change=False)
    fake.effects = [(0, 0, 10, 10, WHITE)]
    r = d.click(5, 5)
    assert r["screen_changed"] is True and "settled" not in r


def test_settling_runs_after_the_seat_is_released(make_desk, fake):
    from tests.test_seat import RecordingSeat

    d = make_desk(screen_diff=True)
    seat = d.seat = RecordingSeat()
    moving_screen(fake, moving_for=10_000)
    d.click(5, 5, show_change=False)
    fake.effects = [(0, 0, 10, 10, WHITE)]
    held, real = [], fake.sleep
    fake.sleep = lambda s: (held.append(seat.held), real(s))[1]
    d.click(5, 5)
    assert len(held) > 30 and not any(held[-30:])  # the long settling poll holds no seat
