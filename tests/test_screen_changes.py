"""Without an accessibility tree, actions say whether the screen changed; wait_for_change waits for it."""

import shutil
import subprocess

import pytest

from ccdesk.errors import UnsupportedError, ValidationError
from ccdesk.parsing import Region
from ccdesk.screendiff import Grid, changed_box, read_gray
from tests.pngutil import read_png, write_png

WHITE = (255, 255, 255)
FIREFOX = "0x01e00003"


def grid(tmp_path, rects=(), name="a.png", size=(80, 60), origin=(0, 0), cell=8):
    path = tmp_path / name
    write_png(path, *size, rects=rects)
    return Grid.from_png(path, origin, cell)


def test_changed_box(tmp_path):
    before = grid(tmp_path, origin=(100, 50))
    after = grid(tmp_path, [(20, 10, 30, 20, WHITE)], "b.png", origin=(100, 50))
    # 8-pixel cells: the box covers the rect, rounded out to whole cells, in screen coordinates.
    assert changed_box(before, after) == (116, 58, 40, 24)
    assert changed_box(before, before) is None


def test_a_blinking_caret_is_not_a_change(tmp_path):
    before = grid(tmp_path)
    caret = grid(tmp_path, [(33, 10, 2, 16, WHITE)], "b.png")
    assert changed_box(before, caret) is None
    # A checkbox the same height but two cells wide is.
    check = grid(tmp_path, [(33, 10, 14, 14, WHITE)], "c.png")
    assert changed_box(before, check) is not None


def test_a_resized_capture_is_all_changed(tmp_path):
    assert changed_box(grid(tmp_path), grid(tmp_path, name="b.png", size=(40, 60))) == (0, 0, 80, 56)


@pytest.mark.skipif(not (shutil.which("magick") or shutil.which("convert")), reason="ImageMagick not installed")
def test_read_gray_decodes_what_imagemagick_writes(tmp_path):
    """ImageMagick picks PNG filters per row; the decoder must undo every one of them."""
    src = tmp_path / "src.png"
    write_png(src, 97, 41, color=(30, 60, 90), rects=[(5, 5, 40, 20, (250, 10, 120)), (60, 2, 30, 30, WHITE)])
    out = tmp_path / "out.png"
    tool = shutil.which("magick") or shutil.which("convert")
    subprocess.run([tool, str(src), "-resize", "50x20!", f"PNG24:{out}"], check=True)
    width, height, gray = read_gray(out)
    ref = read_png(out)
    assert (width, height) == (ref.width, ref.height)
    for y in range(height):
        for x in range(width):
            r, g, b = ref.pixel(x, y)
            assert gray[y * width + x] == (r * 299 + g * 587 + b * 114) // 1000


def test_actions_report_screen_changes(make_desk, fake):
    d = make_desk(screen_diff=True)
    r = d.click(5, 5)
    assert r["screen_changed"] is False and "changed_area" not in r
    fake.effects = [(200, 300, 120, 40, WHITE)]  # Save closes a dialog, say
    r = d.click(250, 320)
    assert r["screen_changed"] is True
    assert r["changed_area"] == {"x": 200, "y": 304, "width": 120, "height": 40}
    fake.effects = [(10, 10, 50, 20, WHITE)]
    assert d.type_text("x")["screen_changed"] is True
    assert d.press_key("Tab")["screen_changed"] is False
    fake.effects = [(0, 500, 800, 100, WHITE)]
    assert d.scroll(5, 5, dy=3)["screen_changed"] is True


def test_screen_changes_cover_only_the_receiving_window(make_desk, fake):
    """On the real screen a clock or a chat popup elsewhere is not the action's doing."""
    d = make_desk(screen_diff=True)
    d.activate_window("Terminal")  # 10,10 300x200
    fake.under = "0x02000001"
    fake.effects = [(500, 400, 100, 100, WHITE)]
    r = d.click(50, 50)
    assert r["screen_changed"] is False
    assert Region(10, 10, 300, 200) in [c[1] for c in fake.calls if c[0] == "screenshot"]


def test_no_screen_changes_with_a_tree_or_without_imagemagick(make_desk, fake, helper, monkeypatch):
    monkeypatch.setattr("ccdesk.desktop.SETTLE", 0)
    d = make_desk(screen_diff=True)
    d.ui_tree()
    r = d.click(5, 5)
    assert "screen_changed" not in r and "changes" in r
    fake.magick = False
    assert "screen_changed" not in make_desk(screen_diff=True).click(5, 5)


def test_wait_for_change_sees_what_changed_since_the_last_action(make_desk, fake):
    """The page loaded while the model was thinking: no need to wait at all."""
    d = make_desk(screen_diff=True)
    d.click(5, 5)
    fake.rects.append((100, 100, 200, 200, WHITE))
    r = d.wait_for_change(timeout=10, settle_ms=500)
    assert r["changed"] is True and r["settled"] is True
    assert r["changed_area"] == {"x": 104, "y": 104, "width": 200, "height": 200}
    assert r["waited_ms"] < 1500


def test_wait_for_change_waits_for_a_late_change_to_settle(make_desk, fake):
    d = make_desk(screen_diff=True)
    fake.later = [[5, (0, 0, 64, 64, WHITE)], [8, (100, 0, 64, 64, WHITE)]]  # a spinner, then the page
    r = d.wait_for_change(timeout=30, settle_ms=1000)
    assert r["changed"] is True and r["settled"] is True
    assert r["changed_area"] == {"x": 0, "y": 0, "width": 168, "height": 64}
    assert 1000 <= r["waited_ms"] < 5000


def test_wait_for_change_times_out(make_desk, fake, read_audit):
    d = make_desk(screen_diff=True)
    r = d.wait_for_change(region="0,0,400,300", timeout=2)
    assert r == {"changed": False, "waited_ms": 2000, "note": r["note"]}
    assert "screenshot" in r["note"]
    assert read_audit()[-1]["tool"] == "wait_for_change"


def test_wait_for_change_validation(make_desk, fake):
    d = make_desk(screen_diff=True)
    with pytest.raises(ValidationError, match="timeout"):
        d.wait_for_change(timeout=301)
    with pytest.raises(ValidationError, match="settle_ms"):
        d.wait_for_change(settle_ms=-1)
    fake.magick = False
    with pytest.raises(UnsupportedError, match="ImageMagick"):
        d.wait_for_change(timeout=1)


def test_wait_for_change_is_allowed_read_only(make_desk, fake):
    fake.later = [[2, (0, 0, 64, 64, WHITE)]]
    assert make_desk(screen_diff=True, read_only=True).wait_for_change(timeout=5, settle_ms=0)["changed"]


def test_show_change_attaches_a_capture_of_the_changed_area(make_desk, fake):
    """So checking an action needs no screenshot of its own."""
    d = make_desk(screen_diff=True)
    fake.effects = [(200, 300, 120, 40, WHITE)]
    r = d.click(250, 320, show_change=True)
    shot = r["change_shot"]
    # changed_area 200,304 120x40, plus SHOT_PAD on each side.
    assert shot["origin"] == {"x": 176, "y": 280}
    assert (shot["width"], shot["height"]) == (168, 88)
    assert shot["approx_tokens"] == round(168 * 88 / 750) and "scale" not in shot
    assert read_png(shot["path"]).pixel(24, 24) == WHITE  # the dialog's corner, inside the pad


def test_a_big_change_shot_is_scaled_down(make_desk, fake):
    d = make_desk(screen_diff=True)
    fake.effects = [(0, 0, 800, 600, WHITE)]  # a new page
    shot = d.click(5, 5, show_change=True)["change_shot"]
    assert shot["scale"] < 1
    assert shot["approx_tokens"] <= 610


def test_show_change_adds_nothing_when_nothing_changed(make_desk, fake):
    d = make_desk(screen_diff=True)
    r = d.press_key("Tab", show_change=True)
    assert r["screen_changed"] is False and "change_shot" not in r and "note" not in r
    fake.magick = False
    r = make_desk(screen_diff=True).click(5, 5, show_change=True)
    assert "change_shot" not in r and "ImageMagick" in r["note"]


def test_a_wide_screenshot_after_a_small_change_says_what_the_crop_costs(make_desk, fake):
    d = make_desk(screen_diff=True)
    fake.effects = [(200, 300, 120, 40, WHITE)]
    d.click(250, 320)
    hint = d.screenshot()["hint"]
    assert 'region="176,280,168,88"' in hint and "show_change=true" in hint
    assert "hint" not in d.screenshot()  # once per action
    fake.effects = [(200, 300, 120, 40, (0, 0, 0))]
    d.click(250, 320)
    assert "hint" not in d.screenshot(region="150,250,300,200")  # already a crop


def test_a_fixed_wait_after_an_action_points_to_wait_for_change(make_desk, fake, monkeypatch):
    monkeypatch.setattr("ccdesk.desktop.time.sleep", lambda s: None)
    d = make_desk(screen_diff=True)
    assert "hint" not in d.wait(2)  # nothing happened before it
    d.click(5, 5)
    assert "wait_for_change" in d.wait(2)["hint"]
    d.click(5, 5)
    assert "hint" not in d.wait(0.5)  # a short pause is fine
    d.activate_window("Terminal")
    assert "hint" not in d.wait(2)  # after a window change there is nothing to compare


def test_no_crop_hint_when_the_crop_saves_little(make_desk, fake):
    """A half-scale shot of a monitor costs about what a full-scale crop of a big change does."""
    fake.size = (1920, 1080)
    d = make_desk(screen_diff=True)
    fake.effects = [(100, 50, 600, 500, WHITE)]  # ~470 tokens to crop; the half-scale shot is ~690
    d.click(250, 320)
    assert "hint" not in d.screenshot(scale=0.5)
