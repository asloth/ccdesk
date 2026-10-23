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
