"""The helper's tree walk, against a fake AT-SPI: no bus, no apps."""

import importlib
import sys
import types

import pytest

STATES = ("SHOWING", "ENABLED", "ACTIVE", "FOCUSED", "CHECKED", "SELECTED", "EXPANDED", "REQUIRED",
          "MANAGES_DESCENDANTS")


class StateSet:
    def __init__(self, names):
        self.names = set(names)

    def contains(self, state):
        return state in self.names


class Extents:
    def __init__(self, x, y, w, h):
        self.x, self.y, self.width, self.height = x, y, w, h


class Node:
    visits = 0

    def __init__(self, role, name="", states=("SHOWING", "ENABLED"), kids=()):
        self.role, self.name, self.states, self.kids = role, name, states, list(kids)

    def get_role_name(self):
        Node.visits += 1
        return self.role

    def get_name(self):
        return self.name

    def get_state_set(self):
        return StateSet(self.states)

    def get_extents(self, _coords):
        return Extents(0, 0, 10, 10)

    def get_text_iface(self):
        return None

    def get_child_count(self):
        return len(self.kids)

    def get_child_at_index(self, i):
        return self.kids[i]

    def get_process_id(self):
        return 1


@pytest.fixture
def helper(monkeypatch):
    atspi = types.SimpleNamespace(
        StateType=types.SimpleNamespace(**{s: s for s in STATES}),
        CoordType=types.SimpleNamespace(SCREEN="screen"),
        Text=types.SimpleNamespace(),
    )
    gi = types.ModuleType("gi")
    gi.require_version = lambda *a: None
    repo = types.ModuleType("gi.repository")
    repo.Atspi = atspi
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repo)
    monkeypatch.delitem(sys.modules, "ccdesk.a11y_helper", raising=False)
    mod = importlib.import_module("ccdesk.a11y_helper")
    yield mod, atspi
    sys.modules.pop("ccdesk.a11y_helper", None)


def test_a_table_that_manages_its_descendants_is_not_walked(helper):
    """LibreOffice Calc's sheet: visiting every showing cell ran past the helper's timeout."""
    mod, atspi = helper
    cells = [Node("table cell", f"A{i}") for i in range(5000)]
    table = Node("table", "Sheet1", ("SHOWING", "ENABLED", "FOCUSED", "MANAGES_DESCENDANTS"), cells)
    frame = Node("frame", "book.ods — LibreOffice Calc", ("SHOWING", "ENABLED", "ACTIVE"),
                 [table, Node("push button", "Save")])
    app = Node("application", "soffice", kids=[frame])
    atspi.get_desktop = lambda _i: Node("desktop", kids=[app])
    Node.visits = 0
    out = mod.read_tree({"max_nodes": 300})
    nodes = {n["name"]: n for n in out["nodes"]}
    assert nodes["Sheet1"]["states"] == ["focused", mod.UNLISTED]
    assert "Save" in nodes and "A0" not in nodes
    assert Node.visits < 20  # the frame, the table and the button, not 5000 cells
    assert out["truncated"] is False
