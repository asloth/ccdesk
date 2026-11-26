"""Calc cell writes and exclusive selection, without a display or input fallback."""
from types import SimpleNamespace as NS

import pytest

from tests.test_a11y_helper_walk import Node, helper  # noqa: F401


class Cell(Node):
    def __init__(self, table, row=0, col=0):
        super().__init__("table cell", "A1")
        self.table, self.row, self.col = table, row, col
        self.generation = table.generation

    def get_parent(self):
        return self.table

    def get_table_cell_iface(self):
        return True

    def get_value_iface(self):
        return True


class Table(Node):
    def __init__(self):
        super().__init__("table", "Sheet1")
        self.generation = 0
        self.text, self.value = "old", 0
        self.selected = (0, 0)
        self.count = 1
        self.frame = Node("frame", "Calc")
        self.frame.kids = [self]
        self.resolutions = []

    def get_parent(self):
        return self.frame

    def get_table_iface(self):
        return True

    def get_selection_iface(self):
        return True

    def get_child_count(self):
        raise AssertionError("must never enumerate the sheet")


@pytest.fixture
def calc(helper, monkeypatch):
    mod, a = helper
    table = Table()
    calls = []
    def resolve(_table, row, col):
        table.resolutions.append((row, col))
        return Cell(table, row, col)
    def value(cell, number):
        table.value, table.text = number, str(number)
        table.generation += 1
        return True
    def read(cell, _start, _end):
        assert cell.generation == table.generation, "stale cell proxy"
        return table.text
    a.Table = NS(get_accessible_at=resolve, get_n_rows=lambda _: 1048576,
                 get_n_columns=lambda _: 16384, get_index_at=lambda _, r, c: r * 16384 + c)
    a.TableCell = NS(get_position=lambda c: (True, c.row, c.col))
    def select(_table, index):
        calls.append(("select", index))
        table.selected = divmod(index, 16384)
        table.generation += 1
        return True
    a.Selection = NS(clear_selection=lambda _: calls.append(("clear",)) or True,
                     select_child=select, get_n_selected_children=lambda _: table.count,
                     get_selected_child=lambda _, i: resolve(table, *table.selected))
    a.Value = NS(set_current_value=value, get_current_value=lambda _: table.value)
    a.Text = NS(get_text=read, get_character_count=lambda _: len(table.text))
    monkeypatch.setattr(mod.time, "sleep", lambda _: None)
    monkeypatch.setattr(mod, "find", lambda _: table)
    return mod, a, table, calls


def set_cell(calc, text, **extra):
    mod, _, _, _ = calc
    return mod.handle({"op": "set", "path": "0.0", "cell": "B7", "text": text, **extra})


@pytest.mark.parametrize("text", ["42", "-3.5", "+.25", "1e3", "0"])
def test_number_readbacks_use_fresh_proxies(calc, text):
    mod, a, table, calls = calc
    assert set_cell(calc, text)["verified"]
    assert table.value == float(text)
    assert len(table.resolutions) >= 3
    assert set(table.resolutions) == {(6, 1)}
    assert not calls


@pytest.mark.parametrize("text", ["hello", "=1+2", "NaN", "Infinity", "12 USD", "1,200", " 5", "2026-11-26", "", "1_000"])
def test_text_formula_and_non_numbers_are_unsupported_without_mutation(calc, text):
    _, a, table, calls = calc
    a.Value.set_current_value = lambda *_: pytest.fail("unsupported text must not write Value")
    result = set_cell(calc, text)
    assert result["code"] == "background_text_unsupported"
    assert "numbers work through set_value" in result["error"]
    assert "activate_window takes the user's focus" in result["error"]
    assert not result.get("verified")
    assert table.text == "old" and not calls


def test_single_cell_ref_is_resolved_by_position(calc, monkeypatch):
    mod, _, table, _ = calc
    monkeypatch.setattr(mod, "find", lambda _: Cell(table, 3, 2))
    assert mod.handle({"op": "set", "path": "0.0", "text": "17"})["verified"]
    assert set(table.resolutions) == {(3, 2)}


@pytest.mark.parametrize("address", ["A0", "A-1", "A1:B2", "1", "XFE1", "A1048577", "", " A1", None])
def test_invalid_addresses_never_write(calc, address):
    assert "error" in set_cell(calc, "text", cell=address)
    assert not calc[3]


@pytest.mark.parametrize("failure", ["clear", "select", "count", "wrong_cell"])
def test_selection_failure_is_explicit_without_fallback(calc, failure):
    mod, a, table, _ = calc
    if failure == "clear": a.Selection.clear_selection = lambda _: False
    if failure == "select": a.Selection.select_child = lambda *_: False
    if failure == "count": table.count = 2
    if failure == "wrong_cell": a.Selection.get_selected_child = lambda *_: Cell(table, 1, 1)
    result = mod.handle({"op": "calc_select", "path": "0.0", "cell": "B7"})
    assert "error" in result and not result.get("selected")
    assert table.text == "old"


@pytest.mark.parametrize("failure", ["refused", "value", "text", "stale"])
def test_numeric_failure_requires_both_readbacks(calc, failure):
    _, a, _, calls = calc
    if failure == "refused": a.Value.set_current_value = lambda *_: False
    if failure == "value": a.Value.get_current_value = lambda _: 999
    if failure == "text": a.Text.get_text = lambda *_: "wrong"
    if failure == "stale": a.Table.get_accessible_at = lambda *_: None
    assert "error" in set_cell(calc, "42")
    assert not calls


def test_temporary_stale_read_re_resolves_by_coordinates(calc):
    _, a, _, _ = calc
    read = a.Text.get_text
    attempts = []
    def stale_once(*args):
        attempts.append(1)
        if len(attempts) == 1: raise RuntimeError("stale")
        return read(*args)
    a.Text.get_text = stale_once
    assert set_cell(calc, "42")["verified"]
    assert len(attempts) == 2


def test_distant_click_selects_exact_cell_without_walking(calc):
    mod, _, _, calls = calc
    assert mod.handle({"op": "calc_select", "path": "0.0", "cell": "AA1001"}) == {"selected": True}
    assert calls == [("clear",), ("select", 1000 * 16384 + 26)]


def test_gone_cell_never_writes(calc, monkeypatch):
    mod = calc[0]
    monkeypatch.setattr(mod, "find", lambda _: None)
    assert set_cell(calc, "hello") == mod.GONE


@pytest.mark.parametrize("failure", ["no_table", "no_table_iface", "no_cell_iface", "bad_position", "no_value", "no_selection", "bad_index", "no_selected"])
def test_invalid_interfaces_and_coordinates_fail_closed(calc, monkeypatch, failure):
    mod, a, table, calls = calc
    request = {"op": "set", "path": "0.0", "text": "42", "cell": "B7"}
    if failure in ("no_table", "no_cell_iface", "bad_position"):
        request.pop("cell")
        cell = Cell(table)
        monkeypatch.setattr(mod, "find", lambda _: cell)
        if failure == "no_table": cell.get_parent = lambda: None
        if failure == "no_cell_iface": cell.get_table_cell_iface = lambda: None
        if failure == "bad_position": a.TableCell.get_position = lambda _: (False, -1, -1)
    if failure == "no_table_iface": table.get_table_iface = lambda: None
    if failure == "no_value": monkeypatch.setattr(Cell, "get_value_iface", lambda _: None)
    if failure == "no_selection": table.get_selection_iface = lambda: None
    if failure == "bad_index": a.Table.get_index_at = lambda *_: -1
    if failure == "no_selected": a.Selection.get_selected_child = lambda *_: None
    if failure in ("no_selection", "bad_index", "no_selected"):
        request["op"] = "calc_select"
    assert "error" in mod.handle(request)
    assert table.text == "old"


def test_calc_identity_is_exposed_without_changing_other_table_cells(calc):
    mod, _, table, _ = calc
    assert mod.describe(Cell(table), "0.0", 0)["calc_cell"] is True
    assert not mod.describe(Node("table cell", "generic"), "0.1", 0).get("calc_cell")
