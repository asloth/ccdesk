from __future__ import annotations

import json

import pytest

from ccdesk.backends.base import Backend
from ccdesk.desktop import Desktop
from ccdesk.parsing import Window
from ccdesk.safety import AuditLog, Policy
from tests.pngutil import write_png


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

    def available(self):
        return None

    def screen_size(self):
        return self.size

    def screenshot(self, path, region):
        self.calls.append(("screenshot", region))
        w, h = (region.width, region.height) if region else self.size
        write_png(path, w, h)

    def scale_png(self, path, width, height):
        self.calls.append(("scale_png", width, height))
        write_png(path, width, height)

    def move(self, x, y):
        self.calls.append(("move", x, y))

    def click(self, x, y, button, count):
        self.calls.append(("click", x, y, button, count))

    def drag(self, x0, y0, x1, y1, ms):
        self.calls.append(("drag", x0, y0, x1, y1, ms))

    def scroll(self, x, y, dx, dy):
        self.calls.append(("scroll", x, y, dx, dy))

    def type_text(self, text):
        self.calls.append(("type_text", text))

    def press_key(self, combo):
        self.calls.append(("press_key", combo.xdotool()))

    def list_windows(self):
        return list(self.windows)

    def activate_window(self, window):
        self.calls.append(("activate", window.id))

    def clipboard_get(self):
        return self.clip

    def clipboard_set(self, text):
        self.clip = text


@pytest.fixture
def fake():
    return FakeBackend()


@pytest.fixture
def audit_path(tmp_path):
    return tmp_path / "audit.log"


@pytest.fixture
def make_desk(fake, audit_path, tmp_path):
    def make(**policy):
        return Desktop(backend=fake, policy=Policy(**policy), audit=AuditLog(audit_path),
                       shots_dir=tmp_path / "shots")
    return make


@pytest.fixture
def read_audit(audit_path):
    def read():
        if not audit_path.exists():
            return []
        return [json.loads(line) for line in audit_path.read_text().splitlines()]
    return read
