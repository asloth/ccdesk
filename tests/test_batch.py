"""Desktop.batch: several input steps in one call, one seat entry, one observation."""

import pytest

from ccdesk.errors import PolicyError, ValidationError
from tests.test_seat import RecordingSeat as _Seat


class RecordingSeat(_Seat):
    """Reentrant like the real lock: `entries` counts only the outermost entries."""

    def __init__(self):
        super().__init__()
        self.depth = 0

    def __enter__(self):
        self.depth += 1
        self.held = True
        if self.depth == 1:
            self.entries += 1

    def __exit__(self, *exc):
        self.depth -= 1
        self.held = self.depth > 0


def with_tree(d):
    d.ui_tree()
    return d


def test_form_fill_in_one_batch(make_desk, helper):
    d = with_tree(make_desk())
    steps = [{"tool": "set_value", "ref": r, "text": f"v{i}"} for i, r in enumerate(("e1", "e2", "e1"))]
    steps.append({"tool": "click_element", "ref": "e1"})
    r = d.batch(steps)
    assert r["done"] == 4 and "failed" not in r
    assert [s["tool"] for s in r["steps"]] == ["set_value"] * 3 + ["click_element"]
    assert all("changes" not in s for s in r["steps"])


def test_one_seat_entry_and_every_step_inside_it(make_desk, fake, helper):
    d = with_tree(make_desk())
    seat = d.seat = RecordingSeat()
    seen = []
    real = fake.click
    fake.click = lambda *a: (seen.append(seat.held), real(*a))[1]
    d.batch([{"tool": "click", "x": 1, "y": 2}, {"tool": "click", "x": 3, "y": 4}, {"tool": "click", "x": 5, "y": 6}])
    assert seen == [True, True, True]
    assert seat.entries == 1


def test_yield_waits_once_before_the_seat(make_desk, fake):
    d = make_desk(yield_ms=500)
    seat = d.seat = RecordingSeat()
    held = []
    real = d._user_idle_ms
    d._user_idle_ms = lambda: (held.append(seat.held), real())[1]
    d.batch([{"tool": "click", "x": 1, "y": 2}, {"tool": "click", "x": 3, "y": 4}])
    assert held[0] is False and held.count(False) == 1  # the one wait is before the seat
    assert d._yield_done is None


def test_observed_once_after_the_seat_is_free_without_a_tree(make_desk, fake):
    d = make_desk(screen_diff=True)
    seat = d.seat = RecordingSeat()
    sleeps = []
    real_sleep = fake.sleep
    fake.sleep = lambda s: (sleeps.append((s, seat.held)), real_sleep(s))[1]
    r = d.batch([{"tool": "click", "x": 1, "y": 2}, {"tool": "click", "x": 3, "y": 4}])
    assert "screen_changed" in r
    settles = [h for s, h in sleeps if s == 0.3]
    assert settles == [False]


def test_observed_once_with_a_tree(make_desk, fake, helper):
    d = with_tree(make_desk())
    seat = d.seat = RecordingSeat()
    seen = []
    real = d.a11y.changes
    d.a11y.changes = lambda **k: (seen.append(seat.held), real(**k))[1]
    d.batch([{"tool": "click", "x": 1, "y": 2}, {"tool": "press_key", "key": "Tab"}, {"tool": "wait", "seconds": 0}])
    assert seen == [False]


def test_validation_error_runs_nothing(make_desk, fake, read_audit):
    d = with_tree(make_desk())
    bad = [
        [{"tool": "click", "x": 1, "y": 2}, {"tool": "launch", "command": "x"}],
        [{"tool": "click", "x": 1, "y": 2}, {"tool": "click", "x": 1, "bogus": 3}],
        [{"tool": "click", "x": 1, "y": 2}, {"tool": "wait", "seconds": 6}],
        [{"tool": "click", "x": 1, "y": 2, "show_change": True}],
        [{"tool": "click", "x": 1}],
        [], [{"tool": "move", "x": 1, "y": 1}] * 21, "nope", [{"x": 1}],
    ]
    for steps in bad:
        with pytest.raises(ValidationError):
            d.batch(steps)
    assert fake.calls == []
    assert not [e for e in read_audit() if e["tool"] in ("click", "move", "batch")]


def test_failure_stops_and_reports(make_desk, fake, helper):
    d = with_tree(make_desk())
    r = d.batch([{"tool": "click", "x": 1, "y": 2}, {"tool": "click_element", "ref": "e999"},
                 {"tool": "click", "x": 9, "y": 9}])
    assert r["done"] == 1
    assert r["failed"]["index"] == 1 and r["failed"]["tool"] == "click_element" and r["failed"]["error"]
    assert len(r["steps"]) == 1
    assert "changes" in r  # the first step was sent, so its effect is still observed


def test_state_is_clean_after_a_failed_batch(make_desk, fake, helper):
    d = with_tree(make_desk(screen_diff=True))
    d.batch([{"tool": "click", "x": 1, "y": 2}, {"tool": "click_element", "ref": "e999"}])
    assert d._deferring is False and d._deferred is None and d._yield_done is None
    r = d.click(5, 5)
    assert "changes" in r


def test_destructive_key_needs_confirm_in_a_step(make_desk, fake):
    d = make_desk()
    r = d.batch([{"tool": "press_key", "key": "q", "modifiers": ["ctrl"]}])
    assert r["done"] == 0 and "confirm=true" in r["failed"]["error"]
    r = d.batch([{"tool": "press_key", "key": "q", "modifiers": ["ctrl"], "confirm": True}])
    assert r["done"] == 1


def test_read_only_refuses_batch(make_desk, fake):
    d = make_desk(read_only=True)
    with pytest.raises(PolicyError, match="read-only"):
        d.batch([{"tool": "wait", "seconds": 0}])


def test_audit_has_the_batch_and_its_steps(make_desk, fake, read_audit):
    d = make_desk()
    d.batch([{"tool": "type_text", "text": "hello"}, {"tool": "click", "x": 1, "y": 2}])
    tools = [e["tool"] for e in read_audit()]
    assert tools == ["type_text", "click", "batch"]
    batch = read_audit()[-1]
    assert batch["status"] == "ok" and len(batch["args"]["steps"]) == 2
    assert batch["result"] == {"done": 2}


def test_batch_caps_the_text_of_all_steps(make_desk, fake):
    from ccdesk.desktop import MAX_TEXT
    from ccdesk.errors import ValidationError
    d = make_desk()
    half = "x" * (MAX_TEXT // 2 + 1)
    with pytest.raises(ValidationError, match="at most"):
        d.batch([{"tool": "type_text", "text": half}, {"tool": "type_text", "text": half}])


def test_batch_will_not_type_into_a_password_field_a_step_focused(make_desk, helper, fake):
    """Single calls learn about the focused password field from the click's re-read; a batch has
    no re-read between steps, so it checks again before typing (blast-radius finding, 2026-11-07)."""
    d = make_desk()
    d.ui_tree()
    real_click = fake.click

    def click_focuses_password(*a):
        helper.nodes["3.0.29.2"].update(role="password text", states=["focused"])
        return real_click(*a)

    fake.click = click_focuses_password
    r = d.batch([{"tool": "click", "x": 50, "y": 60}, {"tool": "type_text", "text": "hunter2"}])
    assert r["done"] == 1 and "password field" in r["failed"]["error"]
    assert not [c for c in fake.calls if c[0] == "type_text"]


def test_batch_pixel_diff_covers_every_step(make_desk, fake):
    """screen_changed compares with the screen before the first step, not the last one."""
    from tests.test_screen_changes import WHITE
    d = make_desk(screen_diff=True)
    real_click = fake.click

    def click_opens_dialog(*a):
        fake.effects = [(200, 300, 120, 40, WHITE)]
        return real_click(*a)

    fake.click = click_opens_dialog
    r = d.batch([{"tool": "click", "x": 250, "y": 320}, {"tool": "press_key", "key": "Tab"}])
    assert r["done"] == 2 and r["screen_changed"] is True
