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
    assert set(entry["result"]["change_shot"]) == {"path", "width", "height", "approx_tokens", "origin", "shot"}
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


# -- coordinates in the picture's own pixels (shot=) -----------------------------------------------


def clicks(fake):
    return [c[1:3] for c in fake.calls if c[0] == "click"]


def test_every_picture_gets_an_id(make_desk, fake, helper):
    d = make_desk(screen_diff=True)
    assert d.screenshot()["shot"] == "s1"
    assert d.screenshot(region="0,0,100,100")["shot"] == "s2"
    fake.effects = [(200, 300, 120, 40, WHITE)]
    assert d.click(250, 320)["change_shot"]["shot"] == "s3"


def test_the_tree_fallback_picture_and_the_stale_refusal_have_ids(make_desk, fake, helper):
    from dataclasses import replace

    from ccdesk.errors import StaleFrameError

    fake.windows[0] = replace(fake.windows[0], title="Form — Mozilla Firefox")
    helper.nodes = {"0": {"path": "0", "role": "frame", "name": "Slack", "depth": 0, "showing": True,
                          "box": [0, 0, 800, 600]}}
    d = make_desk()
    assert d.ui_tree()["fallback_shot"]["shot"] == "s1"
    d.screenshot()
    fake.windows[1].x += 40
    with pytest.raises(StaleFrameError, match=r"\(shot s3\)"):
        d.click(100, 100)
    d.click(10, 10, shot="s3")  # the picture of the refusal is the latest one, and usable


def test_a_scaled_region_shot_maps_back_to_the_screen(make_desk, fake):
    d = make_desk()
    shot = d.screenshot(region="100,100,400,300", scale=0.5)
    assert (shot["width"], shot["height"]) == (200, 150)
    assert 'shot="s1"' in shot["note"] and "origin" in shot and "pixel (px,py)" not in shot["note"]
    r = d.click(50, 40, shot=shot["shot"])
    assert r["clicked"] == {"x": 200, "y": 180} and r["from_shot"] == "s1"
    assert clicks(fake) == [(200, 180)]
    assert d.click(199, 149, shot="s1")["clicked"] == {"x": 498, "y": 398}  # the last pixel: still inside


def test_a_window_shot_maps_back_to_the_screen(make_desk, fake):
    d = make_desk()
    shot = d.screenshot(window="Terminal", scale=0.5)  # 10,10 300x200
    assert shot["origin"] == {"x": 10, "y": 10}
    assert d.click(20, 30, shot=shot["shot"])["clicked"] == {"x": 50, "y": 70}


def test_a_scaled_full_screen_shot_maps_back_to_the_screen(make_desk, fake):
    d = make_desk()
    shot = d.screenshot(scale=0.5)
    assert "origin" not in shot
    assert d.click(100, 100, shot=shot["shot"])["clicked"] == {"x": 200, "y": 200}


def test_an_unscaled_full_screen_shot_needs_no_conversion(make_desk, fake):
    d = make_desk()
    shot = d.screenshot()
    assert "note" not in shot
    assert d.click(100, 100, shot=shot["shot"])["clicked"] == {"x": 100, "y": 100}


def test_the_change_shot_has_its_own_origin_and_scale(make_desk, fake):
    d = make_desk(screen_diff=True)
    fake.effects = [(200, 300, 120, 40, WHITE)]
    shot = d.click(250, 320)["change_shot"]
    assert shot["origin"] == {"x": 176, "y": 280}
    assert d.click(10, 10, shot=shot["shot"])["clicked"] == {"x": 186, "y": 290}
    big = d.screenshot(scale=0.5)["shot"]
    fake.effects = [(0, 0, 800, 600, (9, 9, 9))]
    scaled = d.click(5, 5, shot=big)["change_shot"]  # a whole-screen change comes back scaled down
    assert scaled["scale"] < 1
    x = scaled["width"] // 2
    assert d.click(x, 5, shot=scaled["shot"])["clicked"]["x"] == round(x / scaled["width"] * 800)


def test_without_shot_coordinates_are_screen_pixels(make_desk, fake, read_audit):
    d = make_desk()
    d.screenshot(region="100,100,400,300", scale=0.5)
    assert d.click(50, 40)["clicked"] == {"x": 50, "y": 40}
    assert "from_shot" not in d.click(50, 40) and "shot" not in read_audit()[-1]["args"]


def test_a_point_outside_the_picture_is_refused_before_any_input(make_desk, fake, read_audit):
    d = make_desk()
    d.screenshot(region="100,100,400,300", scale=0.5)  # 200x150
    for x, y in ((200, 10), (10, 150), (-1, 5)):
        with pytest.raises(ValidationError, match="outside shot s1.*200x150.*new screenshot"):
            d.click(x, y, shot="s1")
    assert not [c for c in fake.calls if c[0] in ("click", "move")]
    assert read_audit()[-1]["status"] == "error"


def test_an_unknown_or_expired_id_says_to_take_a_new_screenshot(make_desk, fake):
    d = make_desk()
    d.screenshot()
    for bad in ("s9", "", "1", "change_shot"):
        with pytest.raises(ValidationError, match="unknown or expired shot.*new screenshot"):
            d.click(5, 5, shot=bad)
    from ccdesk.desktop import MAX_SHOTS

    for _ in range(MAX_SHOTS):
        d.screenshot(region="0,0,100,100")
    with pytest.raises(ValidationError, match="expired"):
        d.click(5, 5, shot="s1")
    assert d.click(5, 5, shot=f"s{MAX_SHOTS + 1}")["from_shot"] == f"s{MAX_SHOTS + 1}"  # the latest 32 stay
    assert not clicks(fake)[:-1]


def test_move_scroll_and_drag_take_shot_too(make_desk, fake):
    d = make_desk()
    d.screenshot(region="100,100,400,300", scale=0.5)
    assert d.move(10, 20, shot="s1")["moved"] == {"x": 120, "y": 140}
    r = d.scroll(10, 20, dy=3, shot="s1")
    assert r["at"] == {"x": 120, "y": 140} and r["from_shot"] == "s1"
    r = d.drag(10, 20, 60, 70, shot="s1", duration_ms=0)
    assert r["from"] == {"x": 120, "y": 140} and r["to"] == {"x": 220, "y": 240} and r["from_shot"] == "s1"
    assert ("drag", 120, 140, 220, 240, 0) in fake.calls
    fake.calls.clear()
    with pytest.raises(ValidationError, match="outside"):
        d.drag(10, 20, 260, 70, shot="s1")  # the far end is outside the picture
    assert not fake.calls


def test_the_converted_point_still_gets_the_normal_bounds_check(make_desk, fake):
    d = make_desk()
    fake.size = (100, 100)
    d.screenshot(region="0,0,100,100", scale=0.5)
    d._shots["s1"] = (500, 500, 1.0, 1.0, 50, 50)  # a picture of another, bigger screen
    with pytest.raises(ValidationError, match="<="):
        d.click(10, 10, shot="s1")


def test_the_layout_guard_still_applies_with_shot(make_desk, fake):
    from ccdesk.errors import StaleFrameError

    d = make_desk()
    d.screenshot(region="100,100,400,300", scale=0.5)
    fake.windows[1].x += 40
    with pytest.raises(StaleFrameError):
        d.click(10, 20, shot="s1")
    assert not clicks(fake)


def test_batch_steps_take_shot(make_desk, fake):
    d = make_desk()
    d.screenshot(region="100,100,400,300", scale=0.5)
    r = d.batch([{"tool": "click", "x": 10, "y": 20, "shot": "s1"},
                 {"tool": "move", "x": 50, "y": 40, "shot": "s1"},
                 {"tool": "scroll", "x": 10, "y": 20, "dy": 2, "shot": "s1"},
                 {"tool": "drag", "from_x": 0, "from_y": 0, "to_x": 20, "to_y": 20, "shot": "s1", "duration_ms": 0},
                 {"tool": "click", "x": 7, "y": 7}])
    assert r["done"] == 5 and "failed" not in r
    assert [s.get("from_shot") for s in r["steps"]] == ["s1", "s1", "s1", "s1", None]
    assert clicks(fake) == [(120, 140), (7, 7)]
    assert ("move", 200, 180) in fake.calls and ("scroll", 120, 140, 0, 2) in fake.calls
    assert ("drag", 100, 100, 140, 140, 0) in fake.calls


def test_a_bad_shot_in_a_batch_step_stops_it_before_the_first_step(make_desk, fake):
    d = make_desk()
    d.screenshot(region="100,100,400,300", scale=0.5)
    for bad in ({"tool": "click", "x": 10, "y": 20, "shot": "s7"},
                {"tool": "click", "x": 300, "y": 20, "shot": "s1"},
                {"tool": "drag", "from_x": 1, "from_y": 1, "to_x": 1, "to_y": 999, "shot": "s1"}):
        with pytest.raises(ValidationError, match="step 1"):
            d.batch([{"tool": "click", "x": 1, "y": 1}, bad])
    assert not [c for c in fake.calls if c[0] in ("click", "drag")]


def test_the_audit_log_keeps_the_shot_pixels_and_the_screen_point(make_desk, fake, read_audit):
    d = make_desk()
    d.screenshot(region="100,100,400,300", scale=0.5)
    d.click(50, 40, shot="s1")
    entry = read_audit()[-1]
    assert entry["args"]["x"] == 50 and entry["args"]["shot"] == "s1"
    assert entry["result"]["clicked"] == {"x": 200, "y": 180}
