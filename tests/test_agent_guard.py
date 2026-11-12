"""What an agent (--agent NAME) may touch: only its own window. Rules A-E of the agent guard:
no window-manager activation behind an ordinary action, a covered window says so, no target no
input, a layout check that looks at the agent's own window, and a layout per picture."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ccdesk.errors import CcdeskError, PolicyError, StaleFrameError
from ccdesk.parsing import Region, Window
from tests.test_screen_changes import WHITE

FIREFOX = "0x01e00003"
TERMINAL = "0x02000001"
MOVES = ("click", "drag", "scroll", "type_text", "press_key", "move_window")
INPUT_CALLS = ("click", "drag", "scroll", "type_text", "press_key")


@pytest.fixture(autouse=True)
def fast_settle(monkeypatch):
    monkeypatch.setattr("ccdesk.desktop.SETTLE", 0)


@pytest.fixture
def own(fake):
    fake.own_pointer = True  # the agent has its own pointer and keyboard
    return fake


@pytest.fixture
def agent(own, make_desk):
    return make_desk(agent="desk-1")


def sent(fake):
    return [c for c in fake.calls if c[0] in INPUT_CALLS]


def activations(fake):
    return [c for c in fake.calls if c[0] == "activate"]


# -- A: no window-manager activation behind an ordinary action ----------------------------------


def test_a_minimized_target_sends_nothing_and_never_activates(agent, own):
    agent.activate_window(FIREFOX)
    own.calls.clear()
    own.hidden.add(FIREFOX)
    own.under = own.active = TERMINAL
    with pytest.raises(PolicyError) as exc:
        agent.click(500, 400)
    assert exc.value.code == "gui_target_hidden"
    assert exc.value.input_emitted is False
    assert "activate_window()" in str(exc.value) and "minimized or on another desktop" in str(exc.value)
    assert own.calls == []  # no activation, no click: nothing at all


def test_a_target_on_another_desktop_is_the_same(agent, own):
    agent.activate_window(FIREFOX)
    own.calls.clear()
    own.windows[0] = replace(own.windows[0], desktop=1)
    own.desktop = 0
    own.under = own.active = TERMINAL
    with pytest.raises(PolicyError) as exc:
        agent.click(500, 400)
    assert exc.value.code == "gui_target_hidden"
    own.active = TERMINAL
    with pytest.raises(PolicyError) as exc:
        agent.type_text("x")
    assert exc.value.code == "gui_target_hidden"
    assert own.calls == []


def test_a_pointer_action_on_a_hidden_target_is_refused_even_when_the_keyboard_is_still_in_it(agent, own):
    """The agent's keyboard stays on its window, so the focus fallback of the receiver check
    names the target and the click would go to whatever is on that spot of the screen."""
    agent.activate_window(FIREFOX)
    own.calls.clear()
    own.hidden.add(FIREFOX)
    own.active, own.under = FIREFOX, None
    for call in (lambda: agent.click(500, 400), lambda: agent.scroll(500, 400, dy=2),
                 lambda: agent.drag(5, 5, 9, 9)):
        with pytest.raises(PolicyError) as exc:
            call()
        assert exc.value.code == "gui_target_hidden"
    assert own.calls == []


def test_force_sends_it_to_a_hidden_target_as_before(agent, own):
    agent.activate_window(FIREFOX)
    own.hidden.add(FIREFOX)
    own.under = own.active = TERMINAL
    own.calls.clear()
    assert "clicked" in agent.click(500, 400, force=True)
    assert sent(own) and not activations(own)


def test_a_target_on_show_gets_its_focus_back_as_before(agent, own):
    agent.activate_window(FIREFOX)
    own.calls.clear()
    own.active = TERMINAL  # another window took the agent's keyboard
    r = agent.type_text("hola")
    assert activations(own) == [("activate", FIREFOX)]
    assert r["typed_chars"] == 4 and ("type_text", "hola") in own.calls


def test_without_an_own_pointer_the_shared_focus_is_retaken_as_before(make_desk, fake):
    d = make_desk(agent="desk-1")  # fake.own_pointer is False
    d.activate_window(FIREFOX)
    fake.hidden.add(FIREFOX)
    fake.calls.clear()
    fake.active = TERMINAL
    assert d.type_text("hola")["typed_chars"] == 4
    assert activations(fake) == [("activate", FIREFOX)]


def test_a_note_the_backend_leaves_while_retaking_the_window_is_in_that_actions_result(make_desk, fake):
    notes: list[str] = []
    fake.take_note = lambda: notes.pop() if notes else None  # type: ignore[assignment]
    d = make_desk(agent="desk-1")
    d.activate_window(FIREFOX)
    notes.append("the window was shown with the normal activation")
    fake.hidden.add(FIREFOX)
    fake.active = TERMINAL
    r = d.type_text("hola")
    assert "normal activation" in r["note"]
    assert "note" not in d.type_text("again")  # said once


def test_the_normal_mode_never_retakes_the_window_and_still_refuses(make_desk, fake):
    d = make_desk()
    d.activate_window(FIREFOX)
    fake.hidden.add(FIREFOX)
    fake.calls.clear()
    fake.active = TERMINAL
    with pytest.raises(PolicyError, match="the user may have switched windows") as exc:
        d.type_text("hola")
    assert exc.value.code is None and not activations(fake)


# -- B: a covered window is not a window the user switched to ---------------------------------------


def test_a_covered_target_says_what_covers_it(agent, own):
    agent.activate_window(FIREFOX)
    own.calls.clear()
    own.under = TERMINAL
    with pytest.raises(PolicyError) as exc:
        agent.click(20, 20)
    err = exc.value
    assert err.code == "gui_target_covered" and err.input_emitted is False
    text = str(err)
    assert "20,20" in text and "'Terminal'" in text and TERMINAL in text and "'Mozilla Firefox'" in text
    assert "click_element" in text and "wait()" in text and "not raise your window" in text
    assert "switched windows" not in text
    assert not sent(own)


def test_force_sends_it_into_the_covering_window(agent, own):
    agent.activate_window(FIREFOX)
    own.under = TERMINAL
    own.calls.clear()
    assert agent.click(20, 20, force=True)["window"] == {"id": TERMINAL, "title": "Terminal"}
    assert sent(own)


def test_the_normal_mode_keeps_the_switched_windows_message(make_desk, fake):
    d = make_desk()
    d.activate_window(FIREFOX)
    fake.under = TERMINAL
    with pytest.raises(PolicyError, match="the user may have switched windows") as exc:
        d.click(20, 20)
    assert exc.value.code is None


# -- C: an agent needs a target -----------------------------------------------------------------------

NEEDS_TARGET = [
    lambda d, **kw: d.click(5, 5, **kw),
    lambda d, **kw: d.drag(5, 5, 9, 9, **kw),
    lambda d, **kw: d.scroll(5, 5, dy=2, **kw),
    lambda d, **kw: d.type_text("x", **kw),
    lambda d, **kw: d.press_key("Return", **kw),
    lambda d, **kw: d.click_element("e4", **kw),
    lambda d, **kw: d.set_value("e2", "x", **kw),
]


@pytest.mark.parametrize("call", NEEDS_TARGET)
def test_an_agent_without_a_target_is_refused_and_force_does_not_help(agent, own, helper, call, read_audit):
    agent.ui_tree(window="Firefox")  # reading a named window is allowed
    helper.requests.clear()
    own.calls.clear()
    for kw in ({}, {"force": True}):
        with pytest.raises(PolicyError) as exc:
            call(agent, **kw)
        assert exc.value.code == "gui_target_required" and exc.value.input_emitted is False
        assert "activate_window()" in str(exc.value)
    assert own.calls == []
    assert not [q for q in helper.requests if q["op"] in ("action", "set")]
    assert read_audit()[-1]["status"] == "denied"


def test_ui_tree_without_a_window_is_refused_for_an_agent_without_a_target(agent, helper):
    with pytest.raises(PolicyError) as exc:
        agent.ui_tree()
    assert exc.value.code == "gui_target_required"
    assert helper.requests == []
    assert agent.ui_tree(window="Firefox")["window"]  # a named window is fine


def test_the_tools_that_do_not_need_a_target_still_work(agent, own):
    own.calls.clear()
    assert agent.list_windows()["windows"]
    assert agent.screenshot()["path"]
    assert agent.move(5, 5)["moved"]
    assert agent.wait(0)["waited"] == 0
    assert agent.manage_window(TERMINAL, "move", x=50, y=50)["action"] == "move"
    assert agent.clipboard("get") == {"text": ""}


def test_a_batch_that_needs_the_target_is_refused_before_step_one(agent, own):
    with pytest.raises(PolicyError) as exc:
        agent.batch([{"tool": "move", "x": 5, "y": 5}, {"tool": "click", "x": 5, "y": 5}])
    assert exc.value.code == "gui_target_required"
    assert own.calls == []  # not even the move of step 1


def test_a_batch_that_activates_first_is_fine(agent, own):
    r = agent.batch([{"tool": "activate_window", "title_or_id": FIREFOX}, {"tool": "click", "x": 5, "y": 5}])
    assert r["done"] == 2 and "failed" not in r


def test_with_a_target_every_one_of_them_goes_through(agent, own, helper):
    agent.ui_tree(window="Firefox")
    agent.activate_window(FIREFOX)
    own.under = FIREFOX
    assert "clicked" in agent.click(5, 5)
    assert "typed_chars" in agent.type_text("x")
    assert agent.ui_tree()["window"]


def test_the_normal_mode_needs_no_target(make_desk, fake):
    d = make_desk()
    assert "clicked" in d.click(5, 5)
    assert "typed_chars" in d.type_text("x")
    assert d.ui_tree()["window"]
    assert d.batch([{"tool": "click", "x": 5, "y": 5}])["done"] == 1


def test_a_tree_of_another_window_does_not_skip_the_guard_for_an_agent(agent, own, helper):
    """The tree is of the Firefox form, the agent's window is the Terminal: set_value would write
    into a window that is not its own, and a forced click_element would use the native action."""
    agent.ui_tree(window="Firefox")
    agent.activate_window(TERMINAL)
    helper.requests.clear()
    with pytest.raises(PolicyError, match="not 'Terminal'"):
        agent.set_value("e2", "x", force=True)
    assert not [q for q in helper.requests if q["op"] == "set"]
    own.under = TERMINAL
    own.calls.clear()
    helper.actions["3.0.29.5"] = "click"
    r = agent.click_element("e4", force=True)
    assert "activated" not in r and sent(own)  # by the pointer, through _guard, not natively
    assert not [q for q in helper.requests if q["op"] == "action"]


def test_an_agents_forced_element_click_never_lands_in_another_window(agent, own, helper):
    """The same tree of Firefox, and now the element's spot really is over Firefox: force would
    carry the click into the user's window."""
    agent.ui_tree(window="Firefox")
    agent.activate_window(TERMINAL)
    own.under = FIREFOX
    own.calls.clear()
    with pytest.raises(PolicyError):
        agent.click_element("e4", force=True)
    assert not sent(own)


# -- D: the layout check looks at the agent's own window ------------------------------------------------


@pytest.fixture
def framed(agent, own):
    """The agent's window is Firefox, made sticky so a desktop switch does not hide it, and it has
    taken a screenshot of it."""
    own.windows[0] = replace(own.windows[0], desktop=-1)
    agent.activate_window(FIREFOX)
    own.under = FIREFOX
    agent.screenshot(window="Mozilla Firefox")
    own.calls.clear()
    return agent


def test_other_windows_opening_moving_or_closing_do_not_stale_the_agents_picture(framed, own):
    own.windows[1] = replace(own.windows[1], x=400)  # moved
    assert "clicked" in framed.click(500, 400)
    own.windows.append(Window(id="0x03000001", title="Notes", wm_class="n.N", x=300, y=300,
                              width=200, height=100))  # opened
    assert "clicked" in framed.click(500, 400)
    own.windows = [w for w in own.windows if w.id != TERMINAL]  # closed
    assert "clicked" in framed.click(500, 400)
    assert len(sent(own)) == 3


def test_the_user_switching_desktop_or_window_does_not_stale_it_either(framed, own):
    own.desktop = 3
    own.active = TERMINAL  # the user's focus (here the fake's single focus)
    assert "clicked" in framed.click(500, 400)
    assert "at" in framed.scroll(500, 400, dy=1)


def test_the_target_moving_is_stale_and_the_picture_is_of_its_region(framed, own):
    own.windows[0] = replace(own.windows[0], x=10)
    with pytest.raises(StaleFrameError) as exc:
        framed.click(500, 400)
    assert exc.value.code == "gui_frame_stale" and not sent(own)
    shots = [c for c in own.calls if c[0] == "screenshot"]
    assert shots[-1] == ("screenshot", Region(10, 0, 790, 600))  # the window, not the whole screen
    assert "Mozilla Firefox" in str(exc.value) and "(shot s" in str(exc.value)
    assert "Terminal" not in str(exc.value)


def test_the_target_resized_or_the_screen_changing_size_is_stale(framed, own):
    own.windows[0] = replace(own.windows[0], width=700)
    with pytest.raises(StaleFrameError):
        framed.click(500, 400)
    framed.screenshot(window="Mozilla Firefox")
    own.size = (1024, 768)
    framed._screen = None
    with pytest.raises(StaleFrameError, match="screen"):
        framed.click(500, 400)


def test_force_skips_the_agents_layout_check_too(framed, own):
    own.windows[0] = replace(own.windows[0], x=10)
    assert "clicked" in framed.click(500, 400, force=True)


def test_in_the_normal_mode_all_of_those_still_refuse(make_desk, fake):
    for change in (lambda f: f.windows.__setitem__(1, replace(f.windows[1], x=400)),
                   lambda f: f.windows.append(Window(id="0x03000001", title="Notes", wm_class="n.N", x=1, y=1,
                                                     width=5, height=5)),
                   lambda f: f.windows.pop(1),
                   lambda f: setattr(f, "desktop", 3)):
        fake.__init__()
        d = make_desk()
        d.screenshot()
        change(fake)
        with pytest.raises(StaleFrameError):
            d.click(500, 400)


# -- E: every picture carries the layout it was taken in ----------------------------------------------------


@pytest.mark.parametrize("mode", ["normal", "agent"])
def test_an_older_pictures_coordinates_after_the_window_moved_are_refused(make_desk, fake, mode):
    fake.own_pointer = True
    d = make_desk(agent="desk-1" if mode == "agent" else None)
    if mode == "agent":
        d.activate_window(FIREFOX)
        fake.under = FIREFOX
    d.screenshot(window="Mozilla Firefox")  # s1
    moved = 1 if mode == "normal" else 0
    fake.windows[moved] = replace(fake.windows[moved], x=fake.windows[moved].x + 20)
    d.screenshot(window="Mozilla Firefox") if mode == "agent" else d.screenshot()  # s2 shows the new layout
    fake.calls.clear()
    with pytest.raises(StaleFrameError):
        d.click(50, 50, shot="s1")  # measured before the window moved
    assert not sent(fake)
    # the refusal handed out a fresh picture; the newer one's coordinates pass
    fake.calls.clear()
    assert "clicked" in d.click(50, 50, shot="s2")


def test_without_shot_the_latest_screenshot_is_still_the_baseline(make_desk, fake):
    d = make_desk()
    d.screenshot()
    d.screenshot()
    fake.windows[1] = replace(fake.windows[1], x=400)
    with pytest.raises(StaleFrameError):
        d.click(50, 50)


def test_a_change_shot_is_a_picture_with_its_own_layout(make_desk, fake):
    d = make_desk(screen_diff=True)
    d.screenshot()  # latest delivered screenshot, before the window moved
    fake.windows[1] = replace(fake.windows[1], x=400)
    fake.under = TERMINAL
    fake.effects = [(450, 50, 120, 40, WHITE)]
    r = d.click(450, 50, force=True, show_change=True)  # forced: the baseline is stale on purpose
    shot = r["change_shot"]["shot"]
    fake.effects = []
    fake.calls.clear()
    assert "clicked" in d.click(5, 5, shot=shot)  # it was taken after the move: this layout holds
    with pytest.raises(StaleFrameError):
        d.click(5, 5)  # the latest screenshot still shows the old one
    fake.windows[1] = replace(fake.windows[1], x=100)
    with pytest.raises(StaleFrameError):
        d.click(5, 5, shot=shot)


def test_a_shot_from_before_activate_window_is_unchecked_as_before(make_desk, fake):
    d = make_desk()
    d.screenshot()
    d.activate_window("Mozilla Firefox")
    fake.windows[1] = replace(fake.windows[1], x=400)
    assert "clicked" in d.click(100, 100, shot="s1")
