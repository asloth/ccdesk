from __future__ import annotations

import json
import os

import pytest

from ccdesk.a11y import A11y
from ccdesk.backends.base import Backend
from ccdesk.desktop import Desktop
from ccdesk.errors import CommandError, UnsupportedError
from ccdesk.parsing import Window
from ccdesk.safety import AuditLog, Policy
from tests.pngutil import read_png, write_png, write_rows


class FakeBackend(Backend):
    """Records calls instead of touching a display."""

    name = "fake"
    TOOLS = {"input": ("fake-tool",)}

    def __init__(self, size=(800, 600)):
        super().__init__(env={"DISPLAY": ":0"}, which=lambda n: f"/usr/bin/{n}")
        self.size = size
        self.calls: list[tuple] = []
        self.clip = ""
        self.windows = [
            Window(id="0x01e00003", title="Mozilla Firefox", wm_class="Navigator.firefox", x=0, y=0,
                   width=800, height=600, desktop=0),
            Window(id="0x02000001", title="Terminal", wm_class="xterm.XTerm", x=10, y=10, width=300, height=200),
        ]
        self.active: str | None = None  # id of the focused window; None: the backend cannot tell
        self.under: str | None = None  # id of the window under the pointer, for window_at
        self.transients: dict[str, str] = {}  # dialog id -> the window it belongs to
        self.responses: dict[str, str] = {}  # command line -> output for run(); anything else fails
        self.ran: list[list[str]] = []
        self.slept: list[float] = []
        self.now = 0.0  # a clock that only sleep() advances, for Desktop.clock
        # What is on screen: (x, y, w, h, rgb) rects on black. `effects` are painted by the next
        # input action; `later` holds (screenshots to go, rect) for changes that appear on their own.
        self.rects: list[tuple] = []
        self.effects: list[tuple] = []
        self.later: list[list] = []
        self.magick = True
        # Like X's idle counter: any input resets it, ours included. None: the backend cannot tell.
        self.last_event: float | None = -1e9
        self.user_typing = False  # the user never stops
        self.desktop: int | None = None  # the workspace on show

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def clock(self):
        return self.now

    def idle_ms(self):
        if self.last_event is None:
            return None
        return 0 if self.user_typing else round((self.now - self.last_event) * 1000)

    def _input(self):
        self.rects += self.effects
        self.effects = []
        if self.last_event is not None:
            self.last_event = self.now

    def run(self, argv, input=None, timeout=None, detach=False):
        self.ran.append(list(argv))
        line = " ".join(argv)
        if line not in self.responses:
            raise CommandError(f"{argv[0]} is not installed")
        return self.responses[line]

    def available(self):
        return None

    def screen_size(self):
        return self.size

    def screenshot(self, path, region):
        self.calls.append(("screenshot", region))
        for item in self.later:
            item[0] -= 1
            if item[0] <= 0:
                self.rects.append(item[1])
        self.later = [item for item in self.later if item[0] > 0]
        x, y, w, h = (region.x, region.y, region.width, region.height) if region else (0, 0, *self.size)
        write_png(path, w, h, rects=[(rx - x, ry - y, rw, rh, c) for rx, ry, rw, rh, c in self.rects])

    def scale_png(self, path, width, height):
        self.calls.append(("scale_png", width, height))
        if not self.magick:
            raise UnsupportedError("screenshot(scale=...) needs ImageMagick (magick or convert)")
        img = read_png(path)  # nearest-neighbour, like a cheap resize
        rows = [b"".join(bytes(img.pixel(x * img.width // width, y * img.height // height)) for x in range(width))
                for y in range(height)]
        write_rows(path, width, height, rows)

    def move(self, x, y):
        self.calls.append(("move", x, y))
        self._input()

    def click(self, x, y, button, count):
        self.calls.append(("click", x, y, button, count))
        self._input()

    def drag(self, x0, y0, x1, y1, ms):
        self.calls.append(("drag", x0, y0, x1, y1, ms))
        self._input()

    def scroll(self, x, y, dx, dy):
        self.calls.append(("scroll", x, y, dx, dy))
        self._input()

    def type_text(self, text):
        self.calls.append(("type_text", text))
        self._input()

    def press_key(self, combo):
        self.calls.append(("press_key", combo.xdotool()))
        self._input()

    def list_windows(self):
        return list(self.windows)

    def activate_window(self, window):
        self.calls.append(("activate", window.id))
        self.active = window.id

    def active_window(self):
        return self.active

    def current_desktop(self):
        return self.desktop

    def window_at(self, x, y):
        return self.under

    def transient_for(self, wid):
        return self.transients.get(wid)

    def clipboard_get(self):
        return self.clip

    def clipboard_set(self, text):
        self.clip = text


@pytest.fixture
def fake():
    return FakeBackend()


FORM_NODES = [
    {"path": "3.0.29", "role": "document web", "name": "Form", "depth": 5, "showing": True,
     "box": [4, 90, 1280, 886], "states": ["focused"]},
    {"path": "3.0.29.2", "role": "entry", "name": "Feedback email", "depth": 6, "showing": True,
     "box": [135, 179, 148, 21], "value": "me@x.org"},
    {"path": "3.0.29.3.1.0", "role": "check box", "name": "Peru", "depth": 8, "showing": True,
     "box": [32, 232, 14, 14], "states": ["checked"]},
    {"path": "3.0.29.5", "role": "push button", "name": "Save", "depth": 6, "showing": True,
     "box": [80, 263, 38, 22]},
]


class FakeA11yHelper:
    """Stands in for a11y_helper.py: answers requests from canned nodes."""

    def __init__(self):
        self.nodes = {n["path"]: dict(n) for n in FORM_NODES}
        self.requests = []
        self.apps = [{"name": "Firefox", "pid": 1, "windows": ["Form — Mozilla Firefox"]}]
        self.tree_error: str | None = None

    def __call__(self, argv, timeout):
        req = json.loads(argv[-1])
        self.requests.append(req)
        if req["op"] == "apps":
            return json.dumps({"apps": self.apps})
        if req["op"] == "tree" and self.tree_error:
            return json.dumps({"error": self.tree_error, "no_window": True})
        if req["op"] == "tree":
            nodes = list(self.nodes.values())
            if req.get("query"):
                nodes = [n for n in nodes if req["query"].lower() in n["name"].lower()]
            return json.dumps({"window": "Form — Mozilla Firefox", "app": "Firefox", "pid": 1,
                               "nodes": nodes[: req["max_nodes"]], "truncated": len(nodes) > req["max_nodes"]})
        node = self.nodes.get(req["path"])
        return json.dumps({"node": node} if node else {"error": "element is gone; call ui_tree() again"})


@pytest.fixture
def helper():
    return FakeA11yHelper()


@pytest.fixture
def audit_path(tmp_path):
    return tmp_path / "audit.log"


@pytest.fixture
def make_desk(fake, audit_path, tmp_path, helper):
    def make(screen_diff=False, **policy):
        # screen_diff is off by default so other tests see only the backend calls they make.
        desk = Desktop(backend=fake, policy=Policy(**policy), audit=AuditLog(audit_path),
                       shots_dir=tmp_path / "shots", a11y=A11y(runner=helper, pythons=("/bin/sh",)),
                       screen_diff=screen_diff)
        desk.clock = fake.clock
        return desk
    return make


@pytest.fixture
def read_audit(audit_path):
    def read():
        if not audit_path.exists():
            return []
        return [json.loads(line) for line in audit_path.read_text().splitlines()]
    return read


def pytest_collection_modifyitems(config, items):
    """Display tests click and type for real: keep them off the user's own screen."""
    display = os.environ.get("DISPLAY", "")
    if os.environ.get("CCDESK_TEST_REAL_DISPLAY") == "1" or display not in (":0", ":0.0", ":1"):
        return
    skip = pytest.mark.skip(reason=f"DISPLAY={display} looks like your real screen; run under xvfb-run "
                                   f"(or set CCDESK_TEST_REAL_DISPLAY=1)")
    for item in items:
        if "display" in item.keywords:
            item.add_marker(skip)
