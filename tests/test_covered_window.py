"""An agent whose window is under another one sees its own window: the change report, the picture
of the change, screenshot(window=...), wait_for_change() and the gui_frame_stale picture read the
window's own pixels. A crop of the screen would show the user's window on top."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ccdesk.desktop import DIFF_CELL
from ccdesk.errors import StaleFrameError
from ccdesk.parsing import Region
from tests.pngutil import read_png, write_png

FIREFOX = "0x01e00003"
TERMINAL = "0x02000001"
OWN = (10, 200, 30)  # the colour of the covered window's own content
INPUTS = ("click", "type_text", "press_key", "scroll", "drag")


@pytest.fixture(autouse=True)
def fast_settle(monkeypatch):
    monkeypatch.setattr("ccdesk.desktop.SETTLE", 0)


class Own:
    """The covered window's own pixels, apart from what the fake screen shows. A run of cells
    lights up once an input call went out, or always with `moving`."""

    def __init__(self, fake):
        self.fake, self.grids, self.shots = fake, [], []
        self.covered = {FIREFOX}
        self.broken: str | None = None  # the reason the pixels cannot be read
        self.broken_after_input = False
        self.shot_size = None  # (w, h) to write instead of the region's: a clipped capture
        fake.window_covered = lambda wid: wid in self.covered
        fake.window_gray_grid = self.gray
        fake.window_screenshot = self.shot

    def _acted(self):
        return any(c[0] in INPUTS for c in self.fake.calls)

    def _fails(self):
        if self.broken or (self.broken_after_input and self._acted()):
            self.fake.window_note = self.broken or "the window is gone"
            return True
        return False

    def gray(self, wid, region, cell):
        assert cell == DIFF_CELL
        self.grids.append((wid, region))
        if self._fails():
            return None
        cols, rows = region.width // cell, region.height // cell
        gray = bytearray([40] * (cols * rows))
        if self._acted():
            for i in (cols + 3, cols + 4, cols + 5):
                gray[i] = 200
        return cols, rows, bytes(gray), (region.x, region.y)

    def shot(self, path, wid, region=None):
        self.shots.append((wid, region))
        if self._fails():
            return False
        w, h = self.shot_size or (region.width, region.height)
        write_png(path, w, h, rects=[(0, 0, w, h, OWN)])
        return True


@pytest.fixture
def own(fake):
    fake.own_pointer = True
    fake.active = FIREFOX  # the agent's own keyboard is in its window
    return Own(fake)


@pytest.fixture
def agent(own, make_desk):
    desk = make_desk(screen_diff=True, agent="desk-1")
    desk.activate_window(FIREFOX)
    own.fake.calls.clear()
    return desk


def screen_shots(fake):
    return [c for c in fake.calls if c[0] == "screenshot"]


def test_typing_into_a_covered_window_reports_that_windows_own_change(agent, own, fake):
    r = agent.type_text("hola", show_change=True)
    assert r["covered"] is True
    assert r["screen_changed"] is True
    assert {wid for wid, _ in own.grids} == {FIREFOX}
    assert screen_shots(fake) == []  # the screen was never read: it shows the window on top
    shot = r["change_shot"]
    assert read_png(shot["path"]).pixel(0, 0)[:3] == OWN
    wid, region = own.shots[-1]
    assert wid == FIREFOX
    assert (shot["origin"]["x"], shot["origin"]["y"]) == (region.x, region.y)


def test_the_picture_of_the_change_stays_inside_the_window(make_desk, fake, own):
    fake.windows[0] = replace(fake.windows[0], x=100, y=50, width=400, height=300)
    desk = make_desk(screen_diff=True, agent="desk-1")
    desk.activate_window(FIREFOX)
    r = desk.type_text("hola", show_change=True)
    _, region = own.shots[-1]
    assert region.x >= 100 and region.y >= 50  # the padding around the change is cut at the window's edge
    assert region.x + region.width <= 500 and region.y + region.height <= 350
    assert r["change_shot"]["width"] == region.width


def test_a_window_nothing_covers_is_read_from_the_screen_as_before(agent, own, fake):
    own.covered.clear()
    fake.effects = [(40, 40, 60, 30, (255, 255, 255))]
    r = agent.type_text("hola")
    assert "covered" not in r
    assert r["screen_changed"] is True
    assert own.grids == [] and own.shots == []
    assert screen_shots(fake)


def test_without_agent_mode_the_screen_is_read_as_before(make_desk, fake):
    own = Own(fake)
    fake.active = FIREFOX
    desk = make_desk(screen_diff=True)
    desk.activate_window(FIREFOX)
    r = desk.type_text("hola")
    assert "covered" not in r
    assert own.grids == [] and own.shots == []


def test_own_pixels_that_cannot_be_read_before_the_action_fall_back_to_the_screen(agent, own, fake):
    own.broken = "the Composite extension is missing"
    fake.effects = [(40, 40, 60, 30, (255, 255, 255))]
    r = agent.type_text("hola")
    assert "covered" not in r
    assert r["screen_changed"] is True
    assert screen_shots(fake)


def test_own_pixels_lost_after_the_action_say_so_and_never_compare_with_the_screen(agent, own, fake):
    own.broken_after_input = True
    r = agent.type_text("hola")
    assert r["covered"] is True
    assert "screen_changed" not in r and "change_shot" not in r
    assert "the window is gone" in r["note"] and "screenshot(window=" in r["note"]
    assert screen_shots(fake) == []


def test_a_clipped_picture_of_the_change_is_an_error_and_keeps_the_report(agent, own):
    own.shot_size = (5, 5)
    r = agent.type_text("hola", show_change=True)
    assert r["screen_changed"] is True
    assert "do not fill the area" in r["change_shot"]["error"]


def test_screenshot_of_a_covered_window_is_its_own_pixels(agent, own, fake):
    r = agent.screenshot(window=FIREFOX)
    assert r["covered"] is True and "warning" not in r
    assert own.shots == [(FIREFOX, Region(0, 0, 800, 600))]
    assert screen_shots(fake) == []
    assert read_png(r["path"]).pixel(10, 10)[:3] == OWN
    assert (r["width"], r["height"]) == (800, 600) and r["origin"] == {"x": 0, "y": 0}


def test_screenshot_falls_back_to_the_screen_with_a_warning(agent, own, fake):
    own.broken = "the Composite extension is missing"
    r = agent.screenshot(window=FIREFOX)
    assert "covered" not in r
    assert "the Composite extension is missing" in r["warning"] and "what is on top" in r["warning"]
    assert screen_shots(fake) == [("screenshot", Region(0, 0, 800, 600))]


def test_screenshot_without_agent_mode_crops_the_screen(make_desk, fake):
    own = Own(fake)
    r = make_desk().screenshot(window=FIREFOX)
    assert "covered" not in r and own.shots == []


def test_a_region_or_whole_screen_screenshot_is_the_screen(agent, own, fake):
    agent.screenshot()
    agent.screenshot(region="0,0,100,100")
    assert own.shots == [] and len(screen_shots(fake)) == 2


def test_wait_for_change_watches_the_covered_window(agent, own, fake):
    def sleep(seconds, real=fake.sleep):
        real(seconds)
        if len(fake.slept) == 2:
            fake.calls.append(("press_key", "the page finished loading"))  # the window's content changes
    fake.sleep = sleep
    r = agent.wait_for_change(timeout=5, settle_ms=0)
    assert r["changed"] is True and r["covered"] is True
    assert own.grids and {wid for wid, _ in own.grids} == {FIREFOX}
    assert screen_shots(fake) == []


def test_wait_for_change_after_an_action_keeps_reading_the_same_window(agent, own, fake):
    agent.type_text("hola")
    own.grids.clear()
    r = agent.wait_for_change(timeout=1, settle_ms=0)
    assert r["changed"] is False and r["covered"] is True
    assert own.grids and screen_shots(fake) == []


def test_wait_for_change_with_a_region_is_the_screen(agent, own, fake):
    agent.wait_for_change(region="0,0,100,100", timeout=1, settle_ms=0)
    assert own.grids == []


def test_the_stale_picture_of_a_covered_window_is_its_own_pixels(agent, own, fake):
    fake.under = FIREFOX
    shot = agent.screenshot(window=FIREFOX)
    own.shots.clear()
    fake.windows[0] = replace(fake.windows[0], x=20, y=20, width=700, height=500)
    with pytest.raises(StaleFrameError) as exc:
        agent.click(5, 5, shot=shot["shot"])
    assert own.shots == [(FIREFOX, Region(20, 20, 700, 500))]
    assert "fresh picture of your window" in str(exc.value)
    assert screen_shots(fake) == []


def test_a_covered_window_without_a_tree_gets_its_own_picture_from_ui_tree(agent, own):
    shot = agent._tree_fallback_shot(FIREFOX)
    assert shot["covered"] is True
    assert read_png(shot["path"]).pixel(0, 0)[:3] == OWN


def test_the_picture_of_a_change_never_shows_the_window_next_to_it(make_desk, fake):
    """Nothing covers the Terminal (10,10 300x200), and the change is at its edge: the padding
    stops at the window, so the user's window beside it is not in the picture."""
    desk = make_desk(screen_diff=True)
    desk.activate_window(TERMINAL)
    fake.active = TERMINAL
    fake.effects = [(250, 150, 60, 60, (255, 255, 255))]
    shot = desk.type_text("x", show_change=True)["change_shot"]
    assert shot["origin"]["x"] + shot["width"] <= 310 and shot["origin"]["y"] + shot["height"] <= 210
