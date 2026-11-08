"""The drawn cursor, with a fake helper that records what it is sent (no GTK, no display)."""

import os
import sys
import time
from pathlib import Path

from ccdesk.backends import X11Backend
from ccdesk.cursor import OFFSET, PALETTE, AgentCursor, claim_color, pick_color, valid_color
from ccdesk.desktop import Desktop
from ccdesk.safety import AuditLog
from tests.test_backends import X11_ALL, make


def fake_helper(tmp_path: Path, body: str = "") -> Path:
    path = tmp_path / "helper.py"
    log = tmp_path / "received.txt"
    path.write_text(
        "import sys\n"
        f"{body}\n"
        f"log = open({str(log)!r}, 'a')\n"
        "for line in sys.stdin:\n"
        "    log.write(sys.argv[1] + ' ' + sys.argv[2] + ' ' + line); log.flush()\n")
    return path


def received(tmp_path: Path, count: int) -> list[str]:
    log = tmp_path / "received.txt"
    for _ in range(100):
        lines = log.read_text().splitlines() if log.exists() else []
        if len(lines) >= count:
            return lines
        time.sleep(0.05)
    return lines


def test_move_sends_the_point_offset_from_the_click(tmp_path):
    cur = AgentCursor("bot-1", "#112233", python=sys.executable, helper=fake_helper(tmp_path))
    cur.move(100, 50)
    cur.hide()
    cur.close()
    assert received(tmp_path, 2) == [f"bot-1 #112233 move {100 + OFFSET} {50 + OFFSET}", "bot-1 #112233 hide"]
    assert OFFSET > 0  # the overlay never sits on the pixel that is clicked


def test_palette_pick_is_stable_and_given_color_wins():
    assert pick_color("bot-1") == pick_color("bot-1")
    assert pick_color("bot-1") in PALETTE
    assert len({pick_color(f"bot-{i}") for i in range(30)}) > 3
    assert pick_color("bot-1", "#abcdef") == "#abcdef"
    assert valid_color("#abcdef") and not valid_color("red") and not valid_color("#abc")


def test_a_helper_that_cannot_start_turns_the_cursor_off_once(tmp_path):
    cur = AgentCursor("bot-1", python=sys.executable,
                      helper=fake_helper(tmp_path, "sys.stderr.write('no PyGObject\\n'); sys.exit(1)"))
    cur.move(1, 1)
    time.sleep(0.5)
    cur.move(2, 2)  # never raises
    cur.move(3, 3)
    assert not cur.enabled
    assert "no PyGObject" in cur.status() and "bot-1" in cur.status()


def test_missing_python_turns_the_cursor_off():
    cur = AgentCursor("bot-1", python="/nonexistent/python")
    cur.move(1, 1)
    assert not cur.enabled and cur.status()


class Spy:
    def __init__(self):
        self.moves = []
        self.failure = None

    def move(self, x, y):
        self.moves.append((x, y))

    def status(self):
        return None


def desk(tmp_path, cursor, agent="bot-1"):
    backend, rec = make(X11Backend, X11_ALL, env={"DISPLAY": ":9"})
    return Desktop(backend=backend, audit=AuditLog(tmp_path / "audit.log"), shots_dir=tmp_path / "s",
                   screen_diff=False, agent=agent, cursor=cursor), rec


def test_desktop_moves_the_cursor_after_pointer_actions(tmp_path):
    spy = Spy()
    d, _ = desk(tmp_path, spy)
    d._show_cursor(10, 20)
    d._cursor_to_node({"box": (100, 200, 40, 20)})
    d._cursor_to_node({"box": None})
    assert spy.moves == []  # nothing moves until the action is over
    d._flush_cursor()
    assert spy.moves == [(120, 210)]  # the latest point wins
    d.click(30, 40)
    assert spy.moves[-1] == (30, 40)


def test_cursor_moves_after_the_change_check(tmp_path):
    """The agent's own arrow must not be in the window when _changes() compares it."""
    spy = Spy()
    d, _ = desk(tmp_path, spy)
    seen = []
    real = d._changes
    d._changes = lambda *a, **k: (seen.append(list(spy.moves)), real(*a, **k))[1]
    d.click(30, 40)
    assert seen == [[]] and spy.moves == [(30, 40)]


def test_no_cursor_without_an_agent_name(tmp_path):
    spy = Spy()
    d, _ = desk(tmp_path, spy, agent=None)
    assert d.cursor is None
    d._show_cursor(1, 1)  # a no-op
    assert spy.moves == []


def test_list_windows_hides_the_overlays_in_both_paths():
    wmctrl = ("0x0060002f  0 0 0 640 800 tk.Tk host real\n"
              "0x00600030  0 0 0 20 20 tk.Tk host ccdesk-cursor:bot-1\n")
    b, _ = make(X11Backend, X11_ALL, responses={"wmctrl -lGx": wmctrl})
    assert [w.title for w in b.list_windows()] == ["real"]

    names = iter(["real\n", "ccdesk-cursor:bot-1\n"])
    geo = "X=1\nY=2\nWIDTH=3\nHEIGHT=4\n"

    def runner(argv, input=None, timeout=None, detach=False):
        if argv[:2] == ["xdotool", "search"]:
            return "4194306\n4194307\n"
        if argv[:2] == ["xdotool", "getwindowname"]:
            return next(names)
        if argv[:2] == ["xdotool", "getwindowgeometry"]:
            return geo
        return ""

    b = X11Backend(env={"DISPLAY": ":0"}, runner=runner, which=lambda n: f"/usr/bin/{n}" if n == "xdotool" else None)
    assert [w.title for w in b.list_windows()] == ["real"]


def test_palette_colors_are_distinct():
    assert len(set(PALETTE)) == len(PALETTE) == 8


def test_live_agents_on_one_screen_get_different_colors(tmp_path):
    reg = tmp_path / "cursors.json"
    me = os.getpid()
    colors = [claim_color(f"bot-{i}", reg, pid=me) for i in range(1, 9)]
    assert len(set(colors)) == 8  # every palette color once, even when the hashes collide
    assert claim_color("bot-1", reg, pid=me) == colors[0]  # a name keeps its color
    assert claim_color("bot-9", reg, pid=me) in PALETTE  # all taken: falls back to its hash pick


def test_dead_agents_free_their_color(tmp_path):
    reg = tmp_path / "cursors.json"
    dead = 2 ** 22 + 12345  # above pid_max on most systems
    reg.write_text('{"bot-a": {"color": "%s", "pid": %d}}' % (pick_color("bot-b"), dead))
    assert claim_color("bot-b", reg) == pick_color("bot-b")  # the dead holder no longer blocks it


def test_registry_survives_garbage(tmp_path):
    reg = tmp_path / "cursors.json"
    reg.write_text("not json")
    assert claim_color("bot-1", reg) == pick_color("bot-1")


def test_a_helper_that_never_reads_cannot_block_the_caller(tmp_path):
    helper = tmp_path / "stuck.py"
    helper.write_text("import time\ntime.sleep(30)\n")
    cur = AgentCursor("bot", python=sys.executable, helper=helper)
    try:
        start = time.monotonic()
        for i in range(10000):
            cur.move(i, i)
        assert time.monotonic() - start < 2
        assert cur.enabled
    finally:
        cur._proc and cur._proc.kill()
        cur.close()
