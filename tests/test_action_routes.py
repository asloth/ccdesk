"""Native background routes against fake AT-SPI objects and desktop input recording."""
from types import SimpleNamespace

import pytest

from tests.test_a11y_helper_walk import helper as atspi_helper, Node, StateSet
from ccdesk.a11y import NATIVE_ACTIONS
from ccdesk.errors import CcdeskError
from tests.test_a11y import no_settle


class Widget(Node):
    def __init__(self, role, name="", kids=(), actions=(), selection=False, editable=False):
        super().__init__(role, name, kids=kids)
        self.actions, self.selection, self.editable = actions, selection, editable
        self.text = "old"

    def get_action_iface(self):
        return self if self.actions else None

    def get_selection_iface(self):
        return self if self.selection else None

    def get_editable_text_iface(self):
        return self if self.editable else None

    def get_text_iface(self):
        return self

    def get_state_set(self):
        return StateSet(["EDITABLE"])


def setup(mod, atspi, monkeypatch, obj):
    monkeypatch.setattr(mod, "find", lambda _: obj)
    monkeypatch.setattr(mod.time, "sleep", lambda _: None)
    atspi.StateType.EDITABLE = "EDITABLE"
    atspi.Text = SimpleNamespace(get_character_count=lambda o: len(o.text),
                                get_text=lambda o, start, end: o.text[start:end])
    atspi.Action = SimpleNamespace(get_n_actions=lambda o: len(o.actions),
                                  get_action_name=lambda o, i: o.actions[i])


@pytest.mark.parametrize("actions,expected", [
    (("jump", "toggle", "select", "activate", "click", "press"), "press"),
    (("click", "activate"), "click"), (("activate", "toggle"), "activate"),
    (("toggle", "select"), "select"), (("jump", "toggle"), "toggle"),
    (("other", "jump"), "jump"), (("custom",), "custom"), (("other", "unknown"), None),
    ((), None),
])
def test_action_discovery_priority(atspi_helper, monkeypatch, actions, expected):
    mod, atspi = atspi_helper
    obj = Widget("push button", actions=actions)
    setup(mod, atspi, monkeypatch, obj)
    calls = []
    atspi.Action.do_action = lambda o, i: calls.append(o.actions[i]) or True
    assert mod.action({"path": "0", "names": NATIVE_ACTIONS}) == {"action": expected}
    assert calls == ([expected] if expected else [])


def test_failed_action_is_not_repeated(atspi_helper, monkeypatch):
    mod, atspi = atspi_helper
    obj = Widget("button", actions=("press", "click"))
    setup(mod, atspi, monkeypatch, obj)
    calls = []
    atspi.Action.do_action = lambda o, i: calls.append(i) or False
    assert mod.action({"path": "0", "names": NATIVE_ACTIONS}) == {"action": None}
    assert calls == [0]


@pytest.mark.parametrize("selection,changes,action,success", [
    (True, True, "select", True), (False, True, "select", True),
    (False, False, "select", False), (False, True, "open", False),
])
def test_chromium_option_action_and_readback(atspi_helper, monkeypatch, selection, changes, action, success):
    mod, atspi = atspi_helper
    option = Widget("menu item", "NEW", actions=(action,))
    menu = Widget("menu", kids=[Widget("menu item", "new"), option], selection=True)
    combo = Widget("combo box", "old", kids=[menu], selection=selection)
    setup(mod, atspi, monkeypatch, combo)
    atspi.Selection = SimpleNamespace(select_child=lambda o, i: False,
                                     is_child_selected=lambda o, i: False)
    calls = []
    def run(o, i):
        calls.append(o.name)
        if changes:
            combo.name = o.name
        return True
    atspi.Action.do_action = run
    result = mod.set_value({"path": "0", "text": "NEW"})
    assert result["verified"] is success
    assert calls == (["NEW"] if action == "select" else [])  # exact before casefold


def test_combo_casefold_and_missing_option(atspi_helper, monkeypatch):
    mod, atspi = atspi_helper
    option = Widget("menu item", "New", actions=("select",))
    combo = Widget("combo box", "old", kids=[Widget("menu", kids=[option])])
    setup(mod, atspi, monkeypatch, combo)
    atspi.Action.do_action = lambda o, i: setattr(combo, "name", o.name) or True
    assert mod.set_value({"path": "0", "text": "NEW"}) == {"verified": True}
    assert "no option named" in mod.set_value({"path": "0", "text": "absent"})["error"]


def test_gtk_combo_keeps_selection_route(atspi_helper, monkeypatch):
    mod, atspi = atspi_helper
    combo = Widget("combo box", kids=[Widget("menu item", "New")], selection=True)
    setup(mod, atspi, monkeypatch, combo)
    atspi.Selection = SimpleNamespace(select_child=lambda o, i: True, is_child_selected=lambda o, i: True)
    assert mod.set_value({"path": "0", "text": "new"}) == {"verified": True}


@pytest.mark.parametrize("replace,delete,insert,corrupt", [
    (True, True, True, False), (False, True, True, False),
    (False, False, True, False), (False, True, False, False),
    (True, True, True, True), (False, True, True, True),
])
def test_gtk_editable_full_readback(atspi_helper, monkeypatch, replace, delete, insert, corrupt):
    mod, atspi = atspi_helper
    obj = Widget("entry", editable=True)
    setup(mod, atspi, monkeypatch, obj)
    calls = []
    text = "é" * 300 + " end"
    def set_text(o, value):
        calls.append("set")
        if replace:
            o.text = value + ("wrong" if corrupt else "")
        return replace
    def delete_text(o, start, end):
        calls.append("delete")
        assert (start, end) == (0, -1)
        if delete:
            o.text = ""
        return delete
    def insert_text(o, start, value, length):
        calls.append("insert")
        assert start == 0 and length == len(value.encode())
        if insert:
            o.text = value + ("wrong" if corrupt else "")
        return insert
    atspi.EditableText = SimpleNamespace(set_text_contents=set_text, delete_text=delete_text, insert_text=insert_text)
    result = mod.set_value({"path": "0", "text": text})
    assert result["verified"] is ((replace or (delete and insert)) and not corrupt)
    assert calls == (["set"] if replace else ["set", "delete"] + (["insert"] if delete else []))


@pytest.mark.parametrize("covered,background,own", [(True, False, False), (False, True, False), (False, False, True)])
@pytest.mark.parametrize("direct", [False, True])
def test_text_refusal_before_any_pointer_input(make_desk, helper, fake, no_settle, monkeypatch,
                                               covered, background, own, direct):
    fake.background_input = background
    fake.own_pointer = own
    monkeypatch.setattr(fake, "window_covered", lambda _: covered)
    d = make_desk(agent="routes" if own else None)
    d.ui_tree(window="Firefox")
    helper.values["3.0.29.2"] = {"verified": False, "value": "me@x.org", "refused": True}
    fake.calls.clear()
    with pytest.raises(CcdeskError) as caught:
        if direct:
            node = d.a11y.resolve("e2")
            d._type_into("e2", node, "entry", "new", True)
        else:
            d.set_value("e2", "new", force=True)
    assert caught.value.code == "background_text_unsupported"
    assert caught.value.input_emitted is False
    assert "activate_window" in str(caught.value) and "skip" in str(caught.value)
    assert not [c for c in fake.calls if c[0] in ("click", "move", "type_text", "press_key", "focus", "activate")]


@pytest.mark.parametrize("selected,accepted", [(True, True), (False, False), (False, True)])
def test_option_already_selected_or_action_refused(atspi_helper, monkeypatch, selected, accepted):
    mod, atspi = atspi_helper
    option = Widget("menu item", "New", actions=("select",))
    menu = Widget("menu", kids=[option], selection=True)
    combo = Widget("combo box", "Select", kids=[menu], selection=True)
    setup(mod, atspi, monkeypatch, combo)
    # Even a true Selection return must be verified before accepting it.
    atspi.Selection = SimpleNamespace(select_child=lambda o, i: True,
                                     is_child_selected=lambda o, i: selected if o is menu else False)
    calls = []
    atspi.Action.do_action = lambda o, i: calls.append(i) or accepted
    assert mod.set_value({"path": "0", "text": "New"})["verified"] is selected
    assert calls == ([] if selected else [0])


def test_missing_editable_text_is_refused_without_writing(atspi_helper, monkeypatch):
    mod, atspi = atspi_helper
    obj = Widget("entry")
    setup(mod, atspi, monkeypatch, obj)
    assert mod.set_value({"path": "0", "text": "new"}) == {
        "verified": False, "value": "old", "refused": True}


def test_verified_gtk_write_and_toolbar_click_work_while_covered(make_desk, helper, fake, monkeypatch, no_settle):
    fake.background_input = True
    monkeypatch.setattr(fake, "window_covered", lambda _: True)
    helper.actions["3.0.29.5"] = "click"
    d = make_desk()
    d.ui_tree(window="Firefox")
    fake.calls.clear()
    assert d.set_value("e2", "new")["verified"]
    assert d.click_element("e4")["activated"] == "click"
    assert not [c for c in fake.calls if c[0] in ("click", "move", "type_text", "press_key", "focus", "activate")]


def test_own_pointer_foreground_keeps_typing_fallback(make_desk, helper, fake, monkeypatch, no_settle):
    fake.own_pointer = True
    fake.active = fake.windows[0].id
    monkeypatch.setattr(fake, "window_covered", lambda _: False)
    path = "3.0.29.2"
    helper.values[path] = {"verified": False, "value": "me@x.org", "refused": True}
    monkeypatch.setattr(fake, "type_text", lambda text: helper.nodes[path].update(value=text))
    d = make_desk(agent="routes")
    d.ui_tree(window="Firefox")
    assert d.set_value("e2", "new")["typed"] is True
    assert any(c[0] == "click" for c in fake.calls)
