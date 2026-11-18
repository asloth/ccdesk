"""Element actions work in the window their ref was read from (bench pilot round 2).

Two windows, the agent acts in A, then on a ref from B's tree: with an own pointer
(--own-pointer) and with --input cua-driver it acts on B through accessibility, with no
activate_window() and nothing sent to the window manager.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from ccdesk.a11y import A11y
from ccdesk.backends.cua_driver import CuaDriverBackend
from ccdesk.desktop import Desktop
from ccdesk.errors import PolicyError
from ccdesk.parsing import Window
from ccdesk.safety import AuditLog, Policy
from ccdesk.seat import SeatLock
from tests.conftest import FakeA11yHelper, FakeBackend

MAIL = "0x00000020"
CALC = "0x00000030"
WM_CALLS = ("activate", "focus", "click", "drag", "scroll", "type_text", "press_key", "move")


def nodes(prefix: str, x: int, y: int, button: str) -> list[dict]:
    return [{"path": f"{prefix}", "role": "frame", "name": "", "depth": 0, "showing": True,
             "box": [x, y, 600, 400]},
            {"path": f"{prefix}.1", "role": "push button", "name": button, "depth": 1, "showing": True,
             "box": [x + 100, y + 100, 80, 24]},
            {"path": f"{prefix}.2", "role": "entry", "name": f"{button} field", "depth": 1, "showing": True,
             "box": [x + 100, y + 200, 200, 24]}]


@pytest.fixture
def two(tmp_path):
    """Mail at 0,0 over Calc at 300,200: Calc's button at 400,300 is under Mail."""
    session = FakeBackend()
    session.windows = [Window(MAIL, "Mail", "mail", 0, 0, 600, 400, 0),
                       Window(CALC, "kestrel.ods - LibreOffice Calc", "soffice", 300, 200, 600, 400, 0)]
    session.under = MAIL
    session.active = "user-window"
    helper = FakeA11yHelper()
    helper.trees = {"Mail": ("Mail", nodes("2.0", 0, 0, "Send")),
                    "Calc": ("kestrel.ods - LibreOffice Calc", nodes("3.0", 300, 200, "Recalculate"))}
    helper.actions = {"2.0.1": "click", "3.0.1": "press"}
    return session, helper


def desk(backend, helper, tmp_path) -> Desktop:
    return Desktop(backend=backend, policy=Policy(), audit=AuditLog(tmp_path / "audit.log", agent="a"),
                   shots_dir=tmp_path / "shots", a11y=A11y(runner=helper, pythons=("/bin/sh",)),
                   screen_diff=False, agent="a", seat=SeatLock("test", directory=tmp_path / "locks"))


def refs(d: Desktop, window: str) -> dict[str, str]:
    """name -> ref in the tree of `window`."""
    out = {}
    for line in d.ui_tree(window=window)["tree"].splitlines():
        ref, _, rest = line.strip().partition(" ")
        if '"' in rest:
            out[rest.split('"')[1]] = ref
    return out


@pytest.fixture(params=["own-pointer", "cua-driver"])
def agent(request, two, tmp_path):
    session, helper = two
    if request.param == "own-pointer":
        session.own_pointer = True
        backend = session
    else:
        session.env = dict(os.environ, FAKE_LOG=str(tmp_path / "calls.jsonl"))
        session.env["FAKE_WINDOWS"] = json.dumps([
            {"window_id": int(w.id, 16), "pid": 1, "title": w.title,
             "bounds": {"x": w.x, "y": w.y, "width": w.width, "height": w.height}} for w in session.windows])
        fake = tmp_path / "cua-driver"
        shutil.copyfile(Path(__file__).with_name("fake_cua_driver.py"), fake)
        fake.chmod(0o755)
        backend = CuaDriverBackend(session, str(fake))
        request.addfinalizer(backend.close)
    d = desk(backend, helper, tmp_path)
    d.clock = session.clock

    def driver_input():
        log = tmp_path / "calls.jsonl"
        if not log.exists():
            return []
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        return [r for r in rows if r.get("method") == "tools/call"
                and r["params"]["name"] not in ("check_permissions", "list_windows")]

    return d, session, helper, driver_input


def test_an_element_of_window_b_after_working_in_a_acts_on_b(agent):
    d, session, helper, driver_input = agent
    mail = refs(d, "Mail")
    assert d.click_element(mail["Send"])["activated"] == "click"
    assert d._target.id == MAIL
    calc = refs(d, "Calc")
    r = d.click_element(calc["Recalculate"])  # its spot is under Mail: no "covered" refusal
    assert r["activated"] == "press" and d._target.id == CALC
    assert "your window is now 'kestrel.ods - LibreOffice Calc'" in r["note"]
    assert d.set_value(calc["Recalculate field"], "42")["verified"]
    assert [c for c in session.calls if c[0] in WM_CALLS] == [] and driver_input() == []
    assert session.active == "user-window"


def test_a_ref_from_an_older_tree_acts_in_its_own_window(agent):
    """ui_tree(Mail), then ui_tree(Calc), then a Mail ref: it used to say "no window yet"."""
    d, session, helper, driver_input = agent
    mail = refs(d, "Mail")
    refs(d, "Calc")
    r = d.click_element(mail["Send"])
    assert r["activated"] == "click" and r["window"] == {"title": "Mail"}
    assert d._target.id == MAIL
    assert [c for c in session.calls if c[0] in WM_CALLS] == [] and driver_input() == []
    # the changes it reports are read from Mail, the window of the ref
    assert helper.requests[-1]["op"] == "tree" and helper.requests[-1]["window"] == "Mail"


def test_set_value_in_another_window_after_set_value_in_the_first(agent):
    d, session, helper, driver_input = agent
    mail, calc = refs(d, "Mail"), refs(d, "Calc")
    assert d.set_value(mail["Send field"], "to@x.org")["verified"] and d._target.id == MAIL
    assert d.set_value(calc["Recalculate field"], "7")["verified"] and d._target.id == CALC
    sets = [q["path"] for q in helper.requests if q["op"] == "set"]
    assert sets == ["2.0.2", "3.0.2"]
    assert [c for c in session.calls if c[0] in WM_CALLS] == [] and driver_input() == []


def test_a_shared_pointer_agent_keeps_its_window_and_gets_working_advice(two, tmp_path):
    session, helper = two
    d = desk(session, helper, tmp_path)  # session.own_pointer is False
    mail, calc = refs(d, "Mail"), refs(d, "Calc")
    d.click_element(mail["Send"])
    with pytest.raises(PolicyError) as exc:
        d.set_value(calc["Recalculate field"], "7")
    assert f"activate_window({CALC!r})" in str(exc.value)
    assert d._target.id == MAIL
    d.activate_window(CALC)
    assert d.set_value(calc["Recalculate field"], "7")["verified"]
