"""The fast before/after comparison: XGrab's averaging, the shared X error handler, and how
Desktop settles on a direct capture."""

import ctypes as C
import random

import pytest

from ccdesk.backends import xerror
from ccdesk.backends.x11 import X11Backend
from ccdesk.desktop import DIFF_CELL, SETTLE, SETTLE_POLL
from ccdesk.backends.xgrab import average_cells

# -- average_cells ------------------------------------------------------------


def make_buf(width, height, bpl=None, pad=None, seed=1, pixels=None):
    rnd, padrnd = random.Random(seed), random.Random(seed + 1000)
    bpl = bpl or width * 4
    buf = bytearray(bpl * height)
    for y in range(height):
        for x in range(width):
            b, g, r = pixels(x, y) if pixels else (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
            p = padrnd.randrange(256) if pad is None else pad
            buf[y * bpl + x * 4:y * bpl + x * 4 + 4] = bytes((b, g, r, p))
    return bytes(buf)


def reference(buf, bpl, width, height, cell):
    cols, rows = width // cell, height // cell
    n = len(range(0, cell, 2)) * cell
    out = bytearray()
    for cy in range(rows):
        for cx in range(cols):
            total = 0
            for y in range(cy * cell, cy * cell + cell, 2):
                for x in range(cx * cell, cx * cell + cell):
                    b, g, r = buf[y * bpl + x * 4:y * bpl + x * 4 + 3]
                    total += 2 * r + 4 * g + b
            out.append(min(255, total // (7 * n)))
    return cols, rows, bytes(out)


def test_average_cells_matches_reference():
    w, h = 40, 24
    buf = make_buf(w, h, pad=0)
    assert average_cells(buf, w * 4, w, h, 8) == reference(buf, w * 4, w, h, 8)


def test_average_cells_ignores_the_pad_byte():
    w, h = 40, 24
    a, b = make_buf(w, h, pad=0, seed=5), make_buf(w, h, pad=None, seed=5)
    assert a != b
    assert average_cells(a, w * 4, w, h, 8) == average_cells(b, w * 4, w, h, 8)
    assert average_cells(make_buf(w, h, pad=255, seed=5), w * 4, w, h, 8) == average_cells(a, w * 4, w, h, 8)


def test_average_cells_with_row_padding():
    w, h, bpl = 24, 16, 24 * 4 + 20
    buf = make_buf(w, h, bpl=bpl, pad=0, seed=9)
    assert average_cells(buf, bpl, w, h, 8) == reference(buf, bpl, w, h, 8)


def test_average_cells_drops_partial_cells():
    w, h = 21, 19  # 2 whole cells across, 2 down; 5 columns and 3 rows left over
    buf = make_buf(w, h, pad=0, seed=3)
    cols, rows, gray = average_cells(buf, w * 4, w, h, 8)
    assert (cols, rows, len(gray)) == (2, 2, 4)
    assert gray == reference(buf, w * 4, w, h, 8)[2]


def test_red_green_blue_give_three_different_grays():
    """A plain RGB mean made red and green equal and hid a real change (test_integration_tk)."""
    w, h = 24, 8
    colours = [(0, 0, 255), (0, 255, 0), (255, 0, 0)]  # B, G, R order: red, green, blue
    buf = make_buf(w, h, pad=0, pixels=lambda x, y: colours[x // 8])
    _, _, gray = average_cells(buf, w * 4, w, h, 8)
    red, green, blue = gray
    assert len({red, green, blue}) == 3
    assert green > red > blue
    assert (red, green, blue) == (255 * 2 // 7, 255 * 4 // 7, 255 // 7)


# -- the shared X error handler -------------------------------------------------


class FakeXlib:
    def __init__(self):
        self.set_calls = 0

    def XSetErrorHandler(self, handler):
        self.set_calls += 1


@pytest.fixture
def clean_xerror():
    saved = xerror._installed, dict(xerror._sinks)
    xerror._installed = None
    xerror._sinks.clear()
    yield
    xerror._installed = saved[0]
    xerror._sinks.clear()
    xerror._sinks.update(saved[1])


def event(code, request):
    return xerror._XErrorEvent(error_code=code, request_code=request)


def test_xerror_routes_by_display(clean_xerror):
    xlib, a, b = FakeXlib(), [], []
    xerror.register(xlib, 111, a.append)
    xerror.register(xlib, 222, b.append)
    assert xerror._on_error(111, C.pointer(event(10, 73))) == 0
    assert a == ["X error 10 in request 73"] and b == []
    xerror._on_error(222, C.pointer(event(3, 5)))
    assert b == ["X error 3 in request 5"] and len(a) == 1


def test_xerror_unknown_or_unregistered_display_does_nothing(clean_xerror):
    xlib, a = FakeXlib(), []
    xerror.register(xlib, 111, a.append)
    assert xerror._on_error(999, C.pointer(event(1, 1))) == 0
    assert xerror._on_error(None, C.pointer(event(1, 1))) == 0
    xerror.unregister(111)
    assert xerror._on_error(111, C.pointer(event(1, 1))) == 0
    assert a == []
    xerror.unregister(111)  # twice is fine


def test_xerror_installs_the_handler_once(clean_xerror):
    xlib = FakeXlib()
    xerror.register(xlib, 1, print)
    xerror.register(xlib, 2, print)
    xerror.register(FakeXlib(), 3, print)
    assert xlib.set_calls == 1


# -- the backends -----------------------------------------------------------------


def test_x11_backend_with_a_runner_has_no_gray_grid():
    backend = X11Backend(env={"DISPLAY": ":99"}, which=lambda n: f"/usr/bin/{n}", runner=lambda *a, **k: "")
    assert backend.gray_grid(None, 8) is None
    assert getattr(backend, "_xgrab", None) is None  # never built a grabber, so never opened a display


def test_base_backend_has_no_gray_grid(fake):
    assert fake.gray_grid(None, 8) is None


# -- Desktop on the fast path -------------------------------------------------------

COLS, ROWS = 10, 4
BASE = bytes([40] * (COLS * ROWS))


def with_cells(cells, value=200):
    g = bytearray(BASE)
    for i in cells:
        g[i] = value
    return bytes(g)


BIG = with_cells([11, 12, 13])  # a run of cells: a real change
CARET = with_cells([11])  # one cell: changed_box ignores it


def script(fake, grids):
    """gray_grid answers grids[i] on call i (the last one from then on)."""
    calls = []

    def gray_grid(region, cell):
        assert cell == DIFF_CELL
        gray = grids[min(len(calls), len(grids) - 1)]
        calls.append(region)
        return COLS, ROWS, gray, (0, 0)

    fake.gray_grid = gray_grid
    return calls


def settle(desk, fake):
    before = desk._snapshot(None)
    fake.slept.clear()
    return before, desk._settled_snapshot(None, before)


def test_change_that_stays_returns_before_settle(make_desk, fake):
    d = make_desk(screen_diff=True)
    calls = script(fake, [BASE, BASE, BIG, BIG, BIG])  # before, poll 1, poll 2 (change), poll 3 (still)
    _, after = settle(d, fake)
    assert after.gray == BIG
    assert len(calls) == 4
    assert fake.slept == [SETTLE_POLL] * 3
    assert sum(fake.slept) < SETTLE


def test_no_change_waits_the_whole_settle(make_desk, fake):
    d = make_desk(screen_diff=True)
    script(fake, [BASE])
    before, after = settle(d, fake)
    assert after.gray == before.gray
    assert SETTLE <= sum(fake.slept) < SETTLE + 2 * SETTLE_POLL
    assert set(fake.slept) == {SETTLE_POLL}


def test_an_animation_never_settles_early(make_desk, fake):
    d = make_desk(screen_diff=True)
    frames = [with_cells([11, 12, 13], 50 + 3 * i) for i in range(1, 60)]
    script(fake, [BASE] + frames)
    _, after = settle(d, fake)
    assert SETTLE <= sum(fake.slept) < SETTLE + 2 * SETTLE_POLL
    assert after.gray in frames


def test_a_caret_blink_does_not_return_early(make_desk, fake):
    d = make_desk(screen_diff=True)
    script(fake, [BASE, BASE, CARET, CARET, CARET])
    _, after = settle(d, fake)
    assert after.gray == CARET
    assert SETTLE <= sum(fake.slept) < SETTLE + 2 * SETTLE_POLL


def test_without_a_direct_grid_it_sleeps_once_and_captures_once(make_desk, fake):
    d = make_desk(screen_diff=True)  # FakeBackend.gray_grid is the base one: None
    before = d._snapshot(None)
    assert d._grid_fast is False
    fake.slept.clear()
    fake.calls.clear()
    after = d._settled_snapshot(None, before)
    assert after is not None
    assert fake.slept == [SETTLE]
    assert [c[0] for c in fake.calls].count("screenshot") == 1


def test_click_reports_screen_changed_on_the_fast_path(make_desk, fake):
    d = make_desk(screen_diff=True)
    calls = []

    def gray_grid(region, cell):
        calls.append(region)
        return COLS, ROWS, (BIG if fake.rects else BASE), (0, 0)

    fake.gray_grid = gray_grid
    r = d.click(5, 5)
    assert r["screen_changed"] is False and "changed_area" not in r
    fake.effects = [(0, 0, 10, 10, (255, 255, 255))]  # painted by the next click
    r = d.click(5, 5)
    assert r["screen_changed"] is True and "changed_area" in r
    assert d._grid_fast is True
    assert not any(c[0] == "screenshot" for c in fake.calls)  # the PNG path was never used


def test_empty_web_page_counts_as_no_tree():
    """Firefox started before the a11y bus was on: toolbars and tabs, an empty document."""
    from ccdesk.desktop import empty_page

    chrome_only = ('e1 frame "WhatsApp"\n  e2 tool bar "Navigation"\n    e3 button "Back"\n'
                   '    e4 combo box "Search" ="web.whatsapp.com"\n        e5 document web "WhatsApp" @0,0 9x9')
    assert empty_page(chrome_only)
    full = chrome_only + '\n          e6 button "Send"'
    assert not empty_page(full)
    sibling_after = ('e1 frame "x"\n  e2 document web "p"\n  e3 button "ok"')
    assert empty_page(sibling_after)
    assert not empty_page('e1 frame "x"\n  e2 button "documents"')
