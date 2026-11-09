"""Coordinate input is refused when the window layout moved since the screenshot it came from."""

from __future__ import annotations

import pytest

from ccdesk.errors import StaleFrameError


def test_click_after_the_layout_moved_is_refused(make_desk, fake, read_audit):
    d = make_desk()
    d.screenshot()
    fake.windows[1].x += 40  # another window moved under the model's feet
    with pytest.raises(StaleFrameError) as exc:
        d.click(100, 100)
    assert exc.value.code == "gui_frame_stale"
    assert exc.value.input_emitted is False
    assert not [c for c in fake.calls if c[0] in ("click", "drag", "scroll")]
    assert read_audit()[-1]["status"] == "denied"


def test_force_sends_the_click_anyway(make_desk, fake):
    d = make_desk()
    d.screenshot()
    fake.windows[1].x += 40
    assert "clicked" in d.click(100, 100, force=True)
    assert [c for c in fake.calls if c[0] == "click"]


def test_drag_and_scroll_are_guarded_too(make_desk, fake):
    # Each refusal hands back a fresh screenshot, which becomes the new baseline, so ask again.
    d = make_desk()
    d.screenshot()
    fake.windows[1].x += 40
    with pytest.raises(StaleFrameError):
        d.drag(10, 10, 50, 50)
    fake.windows[1].x += 40
    with pytest.raises(StaleFrameError):
        d.scroll(10, 10, dy=3)
    assert not [c for c in fake.calls if c[0] in ("drag", "scroll")]


def test_a_retitled_target_does_not_count_as_a_moved_layout(make_desk, fake):
    """Pages retitle their window as they load; that alone must not block typing or clicking."""
    d = make_desk()
    d.screenshot()
    target = fake.windows[0]
    target.title = "Form - Mozilla Firefox"
    assert "clicked" in d.click(200, 200)


def test_no_layout_check_without_a_screenshot(make_desk, fake):
    d = make_desk()
    fake.windows[1].x += 40  # nothing was captured, so there is nothing to be stale against
    assert "clicked" in d.click(100, 100)


def test_no_layout_check_when_the_backend_cannot_list_windows(make_desk, fake):
    d = make_desk()
    d.screenshot()

    from ccdesk.errors import CommandError

    def boom():
        raise CommandError("cannot enumerate windows")

    fake.list_windows = boom  # type: ignore[assignment]
    assert "clicked" in d.click(100, 100)


def test_typing_is_not_blocked_by_a_moved_layout(make_desk, fake):
    """Keys go to the focused window, which _guard already checks; the layout fingerprint is
    about coordinates only."""
    d = make_desk()
    d.screenshot()
    fake.windows[1].x += 40
    assert "typed_chars" in d.type_text("hola")


def test_a_new_screenshot_clears_the_refusal(make_desk, fake):
    d = make_desk()
    d.screenshot()
    fake.windows[1].x += 40
    with pytest.raises(StaleFrameError):
        d.click(100, 100)
    d.screenshot()
    assert "clicked" in d.click(100, 100)


def test_activate_window_invalidates_the_old_frame(make_desk, fake):
    d = make_desk()
    d.screenshot()
    d.activate_window("Mozilla Firefox")
    fake.windows[1].x += 40
    # Raising a window restacks by definition: the old screenshot cannot be trusted for it,
    # so the guard is off until the next capture.
    assert "clicked" in d.click(100, 100)


def test_taking_our_window_back_is_not_a_layout_change(make_desk, fake):
    """Another agent on the screen had focus when we took the screenshot; _guard takes our window
    back before the click, and that alone must not make the screenshot stale."""
    d = make_desk(agent="desk-1")
    a, b = fake.windows[0], fake.windows[1]
    d.activate_window(a.title)
    fake.active = b.id
    fake.under = None  # the receiver is the focused window (B), so _guard re-activates A
    d.screenshot()
    assert "clicked" in d.click(500, 400)
    assert [c for c in fake.calls if c[0] == "click"]


def test_focus_moving_to_another_window_still_counts(make_desk, fake):
    d = make_desk()
    a, b = fake.windows[0], fake.windows[1]
    fake.active = a.id
    d.screenshot()
    fake.active = b.id
    with pytest.raises(StaleFrameError, match="focus moved"):
        d.click(500, 400)
    assert not [c for c in fake.calls if c[0] == "click"]
