"""AT-SPI helper, run as a separate process by a Python that has PyGObject (usually /usr/bin/python3).

ccdesk itself stays standard-library only: it starts this file with `python3 -I a11y_helper.py
'<json request>'` and reads one JSON object from stdout. It must not import ccdesk.

Requests:
  {"op": "apps"}
  {"op": "tree", "window": str|None, "query": str|None, "page": bool, "max_nodes": int,
   "settle_ms": int}: settle_ms > 0 watches for a change before answering (see tree()).
  {"op": "resolve", "path": "3.0.1.4"}
  {"op": "detail", "path": "3.0.1.4"}: full text of one node, up to FULL_TEXT
  {"op": "scroll", "path": "3.0.1.4"}: reveal through Component, without input or focus
  {"op": "action", "path": "3.0.1.4", "names": ["click", ...]}
  {"op": "select", "path": "3.0.1.4"}: select it in its parent through AT-SPI Selection
  {"op": "set", "path": "3.0.1.4", "text": str}: pass "-" as the argument and the request on stdin,
  so the text stays out of the process list.

Started with no argument it serves requests as JSON lines on stdin/stdout until stdin closes, so
ccdesk pays for one Python start and one AT-SPI handshake per session instead of per read.
"""

import json
import math
import re
import sys
import time

try:
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
except (ImportError, ValueError) as exc:  # pragma: no cover - depends on the system
    print(json.dumps({"error": f"PyGObject with Atspi is not available: {exc}"}))
    sys.exit(0)

# Roles worth listing even without a name: things you can click, type into or toggle.
ACTIONABLE = {
    "push button", "toggle button", "button", "entry", "password text", "text", "check box",
    "radio button", "combo box", "link", "menu item", "check menu item", "radio menu item",
    "page tab", "spin button", "slider", "list item", "tree item", "switch",
}
# Containers that only add depth when they have no name.
NOISE = {"panel", "section", "filler", "unknown", "redundant object", "document frame", "grouping"}
STATES = {
    "focused": Atspi.StateType.FOCUSED,
    "checked": Atspi.StateType.CHECKED,
    "selected": Atspi.StateType.SELECTED,
    "expanded": Atspi.StateType.EXPANDED,
    "required": Atspi.StateType.REQUIRED,
}
EDITABLE = {"entry", "text", "password text", "document text"}
NUMERIC = {"slider", "spin button", "scroll bar", "dial"}
MAX_VISIT = 20000
# Shown on a node whose children are not walked: it says it manages its descendants, AT-SPI's mark
# for huge or virtual containers (a spreadsheet's cells). Walking them took past the timeout.
UNLISTED = "children not listed"
MAX_TEXT = 200
FULL_TEXT = 10_000  # desktop.MAX_TEXT; this helper cannot import ccdesk


def desktop():
    return Atspi.get_desktop(0)


def children(obj):
    try:
        n = obj.get_child_count()
    except Exception:
        return
    for i in range(n):
        try:
            child = obj.get_child_at_index(i)
        except Exception:
            continue
        if child is not None:
            yield i, child


def text_of(obj, role, limit=MAX_TEXT):
    """Typed value of an entry, or the text of a paragraph-like node."""
    try:
        iface = obj.get_text_iface()
        if iface is None:
            return None
        count = Atspi.Text.get_character_count(obj)
        if count <= 0:
            return None
        text = Atspi.Text.get_text(obj, 0, min(count, limit + 1))
    except Exception:
        return None
    if role == "password text":
        return "•" * min(count, 8)
    # U+FFFC marks an embedded child (a link, a button...); the child is listed on its own.
    text = " ".join(text.replace("￼", " ").split())
    if len(text) > limit:
        text = text[:limit] + ("…" if limit == MAX_TEXT else "")
    return text or None


def child_text(obj, limit=MAX_TEXT):
    """Rich-text editors (X, Slack, Gmail) keep the typed text in unnamed child paragraphs."""
    parts = []
    for _, child in children(obj):
        text = text_of(child, "", limit)
        if text:
            parts.append(text)
        if sum(map(len, parts)) > limit:
            break
    text = " / ".join(parts)
    return (text[:limit] + ("…" if limit == MAX_TEXT else "") if len(text) > limit else text) or None


def describe(obj, path, depth, full=False):
    try:
        role = obj.get_role_name() or ""
        name = (obj.get_name() or "").strip()
        states = obj.get_state_set()
    except Exception:
        return None
    limit = FULL_TEXT if full else MAX_TEXT
    node = {"path": path, "role": role,
            "name": name[:limit] + ("…" if not full and len(name) > limit else ""), "depth": depth,
            "visible": states.contains(Atspi.StateType.VISIBLE),
            "showing": states.contains(Atspi.StateType.SHOWING)}
    if role == "table cell":
        try:
            node["calc_cell"] = obj.get_value_iface() is not None and obj.get_table_cell_iface() is not None
        except Exception:
            pass
    try:
        ext = obj.get_extents(Atspi.CoordType.SCREEN)
        if ext.width > 0 and ext.height > 0:
            node["box"] = [ext.x, ext.y, ext.width, ext.height]
    except Exception:
        pass
    flags = [k for k, v in STATES.items() if states.contains(v)]
    if node["visible"] and not node["showing"]:
        flags.append("offscreen")
    if states.contains(Atspi.StateType.MANAGES_DESCENDANTS):
        flags.append(UNLISTED)
    if not states.contains(Atspi.StateType.ENABLED) and role in ACTIONABLE:
        flags.append("disabled")
    if flags:
        node["states"] = flags
    value = text_of(obj, role, limit)
    if not value and role in EDITABLE:
        value = child_text(obj, limit)
    if full:
        node["truncated"] = len(name) > FULL_TEXT
        try:
            if obj.get_text_iface() is not None:
                count = Atspi.Text.get_character_count(obj)
                value = ("•" * min(count, 8) if role == "password text" else
                         Atspi.Text.get_text(obj, 0, min(count, FULL_TEXT)))
                node["text"] = value
                node["truncated"] |= role != "password text" and count > FULL_TEXT
        except Exception:
            pass
        if role in NUMERIC and obj.get_value_iface() is not None:
            value = str(Atspi.Value.get_current_value(obj))
    if value and (full or value != name):
        node["value"] = value[:FULL_TEXT]
    return node


def frames():
    """(path, app, frame) for every top-level window of every registered app."""
    for ai, app in children(desktop()):
        for fi, frame in children(app):
            yield f"{ai}.{fi}", app, frame


def pick_window(title):
    found = list(frames())
    if not found:
        return None, "no app has a window on the accessibility bus"
    if title:
        needle = title.lower()
        matches = [f for f in found if needle in (f[2].get_name() or "").lower()]
        if not matches:
            names = ", ".join(repr(f.get_name()) for _, _, f in found[:15])
            return None, f"no accessible window title contains {title!r}; windows: {names}"
        # Same title twice (two copies of an app, or apps on another X display): prefer the active one.
        active = [m for m in matches if is_active(m[2])]
        return (active or matches)[0], None
    for picked in found:
        if is_active(picked[2]):
            return picked, None
    if len(found) == 1:
        return found[0], None  # no window manager to mark it active, but there is only one
    names = ", ".join(repr(f.get_name()) for _, _, f in found[:15])
    return None, f"no active accessible window; pass window=... (windows: {names})"


def matching_windows(title):
    needle = (title or "").lower()
    return sum(1 for _, _, f in frames() if needle and needle in (f.get_name() or "").lower())


def is_active(frame):
    try:
        return frame.get_state_set().contains(Atspi.StateType.ACTIVE)
    except Exception:
        return False


def interesting(node):
    if UNLISTED in node.get("states", ()):
        return True  # the only sign of what is inside
    role, name = node["role"], node["name"]
    # A bare container is depth, unless it is what carries the text: Firefox wraps the
    # contents of a `role="alert"` list or a `span` in an unnamed section whose only
    # content is the message ("This username is already being used"). Dropping it on the
    # role alone hid the reason a form had failed.
    if role in NOISE and not name and not node.get("value"):
        return False
    return bool(name or node.get("value") or role in ACTIONABLE)


def read_tree(req):
    picked, err = pick_window(req.get("window"))
    if err:
        return {"error": err, "no_window": True}
    path, app, frame = picked
    query = (req.get("query") or "").lower() or None
    max_nodes = int(req.get("max_nodes") or 300)
    page_only = bool(req.get("page"))
    out, visited, truncated = [], 0, False

    def walk(obj, p, depth, in_page):
        nonlocal visited, truncated
        if truncated:
            return
        visited += 1
        if visited > MAX_VISIT:
            truncated = True
            return
        node = describe(obj, p, depth)
        if node is None or not node["visible"]:
            return  # hidden subtrees: other tabs, collapsed menus and hidden panels
        here = in_page or node["role"] == "document web"
        keep = interesting(node) and (here or not page_only)
        if keep and query:
            keep = query in node["name"].lower() or query in (node.get("value") or "").lower()
        if keep:
            if len(out) >= max_nodes:
                truncated = True
                return
            out.append(node)
        if UNLISTED in node.get("states", ()):
            return
        for i, child in children(obj):
            walk(child, f"{p}.{i}", depth + 1, here)

    walk(frame, path, 0, False)
    frame_node = describe(frame, path, 0) or {}
    title = req.get("window")
    return {"window": frame.get_name(), "app": app.get_name(), "pid": app.get_process_id(),
            "nodes": out, "truncated": truncated,
            # Only needed to warn about an ambiguous title. A settle poll skips it: enumerating
            # the desktop on every poll would make the settle expensive.
            "matches": matching_windows(title) if title and not req.get("settle_ms") else 0,
            "frame_box": frame_node.get("box")}


# The latest read: what a poll compares against, without serializing the tree again.
LAST: dict = {"key": None, "marks": None}


def mark(node):
    """Everything a tree line shows, so two reads can be compared cheaply."""
    return (node["role"], node["name"], node.get("value"), tuple(node.get("states") or ()),
            tuple(node.get("box") or ()))


def tree(req):
    """The tree, after watching it for `settle_ms` for a change.

    Re-reading costs a walk, not a process: while the window is still, poll; as soon as a mark
    differs, answer. A slow app still gets the whole settle window, a fast one answers at once."""
    key = (req.get("window"), req.get("query"), bool(req.get("page")), int(req.get("max_nodes") or 300))
    deadline = time.monotonic() + max(0.0, float(req.get("settle_ms") or 0) / 1000)
    previous = LAST["marks"] if LAST["key"] == key else None
    while True:
        started = time.monotonic()
        result = read_tree(req)
        if "error" in result:
            return result  # no window to watch
        marks = {"#window": (result.get("window"), result.get("app"), result.get("pid"))}
        marks.update((n["path"], mark(n)) for n in result["nodes"])
        LAST.update(key=key, marks=marks)
        if previous is None or marks != previous:
            return result  # first read of this request, or something changed
        if time.monotonic() >= deadline:
            return result
        walk = time.monotonic() - started
        time.sleep(min(0.03 if walk < 0.08 else 0.15, max(0.0, deadline - time.monotonic())))


def find(path):
    obj = desktop()
    for i in [int(x) for x in str(path).split(".")]:
        try:
            obj = obj.get_child_at_index(i)
        except Exception:
            obj = None
        if obj is None:
            return None
    return obj


GONE = {"error": "element is gone; call ui_tree() again"}


def resolve(req):
    obj = find(req["path"])
    return {"node": describe(obj, req["path"], 0)} if obj else GONE


def detail(req):
    obj = find(req["path"])
    return {"node": describe(obj, req["path"], 0, full=True)} if obj else GONE


def scroll(req):
    """Reveal a visible element through Component only, without input or focus changes."""
    obj = find(req["path"])
    if obj is None:
        return GONE
    node = describe(obj, req["path"], 0)
    if node and node["visible"] and not node["showing"]:
        try:
            Atspi.Component.scroll_to(obj, Atspi.ScrollType.ANYWHERE)
        except Exception:
            pass
        for _ in range(10):
            node = describe(obj, req["path"], 0)
            if node and node["showing"] and node.get("box"):
                return {"scrolled": True}
            time.sleep(0.05)
    return {"scrolled": bool(node and node["showing"] and node.get("box"))}


def action(req):
    """Run the first of `names` the element offers. {"action": None} means nothing ran."""
    obj = find(req["path"])
    if obj is None:
        return GONE
    if obj.get_action_iface() is None:
        return {"action": None}
    offered = [Atspi.Action.get_action_name(obj, i) or "" for i in range(Atspi.Action.get_n_actions(obj))]
    lowered = [name.lower() for name in offered]
    for want in req["names"]:
        if want in lowered:
            i = lowered.index(want)
            return {"action": offered[i] if Atspi.Action.do_action(obj, i) else None}
    if len(offered) == 1:
        return {"action": (offered[0] or "action 0") if Atspi.Action.do_action(obj, 0) else None}
    return {"action": None}


def select(req):
    """Select the element in its parent through Selection, the way a click on a sheet tab or a page
    tab would, with no pointer and no focus. {"selected": None}: the parent has no Selection, or the
    element is not one of its children."""
    path = str(req["path"])
    obj = find(path)
    if obj is None:
        return GONE
    parent_path, _, index = path.rpartition(".")
    parent = find(parent_path) if parent_path else None
    if parent is None or parent.get_selection_iface() is None:
        return {"selected": None}
    index = int(index)
    if Atspi.Selection.is_child_selected(parent, index):
        return {"selected": True, "already": True}
    if not Atspi.Selection.select_child(parent, index):
        return {"selected": False}
    for _ in range(10):  # the app may apply it a moment later
        if Atspi.Selection.is_child_selected(parent, index):
            return {"selected": True}
        time.sleep(0.05)
    return {"selected": False}


def options(obj):
    """A combo box's options as (index for Selection, name): its own children, or those of the
    menu or list it holds (GTK puts the items in a menu)."""
    kids = list(children(obj))
    if len(kids) == 1 and (kids[0][1].get_role_name() or "") in ("menu", "list", "list box"):
        kids = list(children(kids[0][1]))
    return [(i, (c.get_name() or "").strip()) for i, c in kids]


def choose(obj, text):
    """Choose by name through Selection or an option action, without opening a popup."""
    found = options(obj)
    want = text.strip()
    match = ([i for i, name in found if name == want]
             or [i for i, name in found if name.casefold() == want.casefold()])
    if not match:
        names = ", ".join(repr(n) for _, n in found if n)[:MAX_TEXT] or "none listed"
        return {"error": f"no option named {text!r} (options: {names})"}
    index = match[0]
    if obj.get_selection_iface() is not None and Atspi.Selection.select_child(obj, index):
        for _ in range(10):
            if Atspi.Selection.is_child_selected(obj, index):
                return {"verified": True}
            time.sleep(0.05)
    # Chromium exposes options below a menu, but refuses Selection.select_child.
    # Invoke only the option's advertised select action, never the combo's open action.
    kids = list(children(obj))
    if len(kids) == 1 and (kids[0][1].get_role_name() or "") in ("menu", "list", "list box"):
        parent = kids[0][1]
        if (parent.get_selection_iface() is not None
                and Atspi.Selection.is_child_selected(parent, index)):
            return {"verified": True}
        option = parent.get_child_at_index(index)
        if option is not None and option.get_action_iface() is not None:
            for i in range(Atspi.Action.get_n_actions(option)):
                if (Atspi.Action.get_action_name(option, i) or "").casefold() == "select":
                    if not Atspi.Action.do_action(option, i):
                        break
                    expected = dict(found)[index]
                    for _ in range(10):
                        if ((obj.get_name() or "").strip() == expected
                                or text_of(obj, "combo box") == expected
                                or (parent.get_selection_iface() is not None
                                    and Atspi.Selection.is_child_selected(parent, index))):
                            return {"verified": True}
                        time.sleep(0.05)
                    break
    return {"verified": False, "value": text_of(obj, "combo box") or ""}


def full_text(obj):
    return Atspi.Text.get_text(obj, 0, Atspi.Text.get_character_count(obj))


def calc_target(req, obj):
    """Resolve sheet coordinates without enumerating Calc's virtual children."""
    if req.get("cell") is not None:
        if obj.get_role_name() != "table" or obj.get_table_iface() is None:
            raise ValueError("cell requires a sheet table ref with the Table interface")
        match = re.fullmatch(r"([A-Za-z]{1,3})([1-9][0-9]{0,6})", str(req["cell"]))
        if not match:
            raise ValueError("cell must be an A1 address, for example B7")
        col = 0
        for letter in match[1].upper():
            col = col * 26 + ord(letter) - ord("A") + 1
        row, col = int(match[2]) - 1, col - 1
        table = obj
    else:
        if obj.get_role_name() != "table cell" or obj.get_table_cell_iface() is None:
            raise ValueError("expected a Calc table cell with TableCell")
        table = obj.get_parent()
        if table is None or table.get_role_name() != "table" or table.get_table_iface() is None:
            raise ValueError("cell has no sheet table")
        position = Atspi.TableCell.get_position(obj)
        if not position[0]:
            raise ValueError("cannot read the cell's row and column")
        row, col = position[1:]
    if not (0 <= row < Atspi.Table.get_n_rows(table) and 0 <= col < Atspi.Table.get_n_columns(table)):
        raise ValueError("cell address is outside the sheet")
    cell = Atspi.Table.get_accessible_at(table, row, col)
    if cell is None or cell.get_role_name() != "table cell" or cell.get_value_iface() is None:
        raise ValueError("target is not a Calc cell with Value")
    return table, row, col


def plain_number(text):
    # No dates, currency, NaN/Inf, whitespace, formulas, or locale coercion.
    if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", text):
        return None
    number = float(text)
    return number if math.isfinite(number) else None


def calc_set(req, obj):
    table, row, col = calc_target(req, obj)
    number = plain_number(req["text"])
    if number is None:
        return {"error": "Calc numbers work through set_value. Text and formulas need the window "
                         "in front: activate_window takes the user's focus. Then enter the text "
                         "through the foreground window.",
                "code": "background_text_unsupported"}
    cell = Atspi.Table.get_accessible_at(table, row, col)
    if not Atspi.Value.set_current_value(cell, number):
        raise ValueError("Calc refused the numeric cell write")
    last = None
    for _ in range(10):
        try:
            cell = Atspi.Table.get_accessible_at(table, row, col)  # old proxies go stale
            got = full_text(cell)
            value = Atspi.Value.get_current_value(cell)
            if value == number and plain_number(got) == number:
                return {"verified": True, "value": got}
            last = f"readback mismatch: cell holds {got!r}"
        except Exception as exc:
            last = f"cell readback failed: {exc}"
        time.sleep(0.05)
    raise ValueError(f"Calc {last}; check the cell before retrying")


def set_value(req):
    """Write a value and read it back, so the answer is what the app holds, not what was sent."""
    obj = find(req["path"])
    if obj is None:
        return GONE
    role, text = obj.get_role_name() or "", req["text"]
    if (role == "table cell" and obj.get_value_iface() is not None) or req.get("cell") is not None: return calc_set(req, obj)
    if role == "password text":
        return {"error": "this is a password field: ask the user to type it themselves"}
    if role in NUMERIC and obj.get_value_iface() is not None:
        try:
            number = float(text)
        except ValueError:
            return {"error": f"{role} takes a number, not {text!r}"}
        Atspi.Value.set_current_value(obj, number)
        read = lambda: Atspi.Value.get_current_value(obj)
        same = lambda got: abs(got - number) < 1e-6
    elif role == "combo box":
        return choose(obj, text)
    elif obj.get_editable_text_iface() is not None and obj.get_text_iface() is not None:
        if not obj.get_state_set().contains(Atspi.StateType.EDITABLE):
            return {"error": "this field is read-only"}
        if not Atspi.EditableText.set_text_contents(obj, text):
            # Some GTK editors reject replacement but accept delete followed by insertion.
            if not (Atspi.EditableText.delete_text(obj, 0, -1)
                    and Atspi.EditableText.insert_text(obj, 0, text, len(text.encode()))):
                got = full_text(obj)
                return {"verified": False, "value": got[:MAX_TEXT], "refused": True}
        read, same = lambda: full_text(obj), lambda got: got == text
    elif role in EDITABLE:
        # A text field with no way to write it directly (Chromium's web entries): ccdesk types it.
        return {"verified": False, "value": text_of(obj, role) or "", "refused": True}
    else:
        return {"error": f"{role} has no settable value: use click_element, then type_text"}
    got = read()
    for _ in range(10):  # the app may apply it a moment later
        if same(got):
            return {"verified": True}
        time.sleep(0.05)
        got = read()
    return {"verified": False, "value": str(got)[:MAX_TEXT]}


def insert(req):
    """Insert text at the caret (at the end with "at_end", or when the field has no caret) and
    read it back. Needs no keyboard focus: an agent beside the user types this way without
    activating the window."""
    obj = find(req["path"])
    if obj is None:
        return GONE
    role, text = obj.get_role_name() or "", req["text"]
    if role == "password text":
        return {"error": "this is a password field: ask the user to type it themselves"}
    if obj.get_editable_text_iface() is None or obj.get_text_iface() is None:
        return {"inserted": False}
    if not obj.get_state_set().contains(Atspi.StateType.EDITABLE):
        return {"error": "this field is read-only"}
    before = full_text(obj)
    count = Atspi.Text.get_character_count(obj)
    caret = Atspi.Text.get_caret_offset(obj)
    at = caret if 0 <= caret <= count and not req.get("at_end") else count
    if not Atspi.EditableText.insert_text(obj, at, text, len(text.encode())):
        return {"inserted": False}
    want = before[:at] + text + before[at:]
    for _ in range(10):  # the app may apply it a moment later
        if full_text(obj) == want:
            return {"inserted": True}
        time.sleep(0.05)
    return {"inserted": False, "changed": full_text(obj) != before}


def apps(_req):
    return {"apps": [{"name": app.get_name(), "pid": app.get_process_id(),
                      "windows": [f.get_name() for _, f in children(app)]}
                     for _, app in children(desktop())]}


def handle(req):
    ops = {"apps": apps, "tree": tree, "resolve": resolve, "action": action, "set": set_value,
           "insert": insert, "select": select, "detail": detail, "scroll": scroll}
    try:
        return ops[req["op"]](req)
    except Exception as exc:  # report, never traceback into the MCP server's stdout parsing
        return {"error": f"{type(exc).__name__}: {exc}"}


def main():
    # One-shot form, kept for scripts: `a11y_helper.py '<json>'` (or "-" and the request on stdin).
    if len(sys.argv) > 1:
        req = json.loads(sys.stdin.read() if sys.argv[1] == "-" else sys.argv[1])
        print(json.dumps(handle(req), ensure_ascii=False))
        return
    # Persistent form: one JSON request per line, one answer per line, on stdin/stdout. The MCP
    # server keeps this alive so a read does not pay for a Python start and an AT-SPI handshake.
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except ValueError as exc:
            print(json.dumps({"error": f"bad request: {exc}"}), flush=True)
            continue
        if req.get("op") == "shutdown":
            break
        print(json.dumps(handle(req), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
