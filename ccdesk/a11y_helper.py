"""AT-SPI helper, run as a separate process by a Python that has PyGObject (usually /usr/bin/python3).

ccdesk itself stays standard-library only: it starts this file with `python3 -I a11y_helper.py
'<json request>'` and reads one JSON object from stdout. It must not import ccdesk.

Requests:
  {"op": "apps"}
  {"op": "tree", "window": str|None, "query": str|None, "page": bool, "max_nodes": int,
   "settle_ms": int}: settle_ms > 0 watches for a change before answering (see tree()).
  {"op": "resolve", "path": "3.0.1.4"}
  {"op": "action", "path": "3.0.1.4", "names": ["click", ...]}
  {"op": "set", "path": "3.0.1.4", "text": str}: pass "-" as the argument and the request on stdin,
  so the text stays out of the process list.

Started with no argument it serves requests as JSON lines on stdin/stdout until stdin closes, so
ccdesk pays for one Python start and one AT-SPI handshake per session instead of per read.
"""

import json
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
MAX_TEXT = 200


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


def text_of(obj, role):
    """Typed value of an entry, or the text of a paragraph-like node."""
    try:
        iface = obj.get_text_iface()
        if iface is None:
            return None
        count = Atspi.Text.get_character_count(obj)
        if count <= 0:
            return None
        text = Atspi.Text.get_text(obj, 0, min(count, MAX_TEXT + 1))
    except Exception:
        return None
    if role == "password text":
        return "•" * min(count, 8)
    # U+FFFC marks an embedded child (a link, a button...); the child is listed on its own.
    text = " ".join(text.replace("￼", " ").split())
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + "…"
    return text or None


def child_text(obj):
    """Rich-text editors (X, Slack, Gmail) keep the typed text in unnamed child paragraphs."""
    parts = []
    for _, child in children(obj):
        text = text_of(child, "")
        if text:
            parts.append(text)
        if sum(map(len, parts)) > MAX_TEXT:
            break
    text = " / ".join(parts)
    return (text[:MAX_TEXT] + "…" if len(text) > MAX_TEXT else text) or None


def describe(obj, path, depth):
    try:
        role = obj.get_role_name() or ""
        name = (obj.get_name() or "").strip()
        states = obj.get_state_set()
    except Exception:
        return None
    node = {"path": path, "role": role, "name": name[:MAX_TEXT], "depth": depth,
            "showing": states.contains(Atspi.StateType.SHOWING)}
    try:
        ext = obj.get_extents(Atspi.CoordType.SCREEN)
        if ext.width > 0 and ext.height > 0:
            node["box"] = [ext.x, ext.y, ext.width, ext.height]
    except Exception:
        pass
    flags = [k for k, v in STATES.items() if states.contains(v)]
    if not states.contains(Atspi.StateType.ENABLED) and role in ACTIONABLE:
        flags.append("disabled")
    if flags:
        node["states"] = flags
    value = text_of(obj, role)
    if not value and role in EDITABLE:
        value = child_text(obj)
    if value and value != name:
        node["value"] = value
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
    role, name = node["role"], node["name"]
    if role in NOISE and not name:
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
        if node is None or not node["showing"]:
            return  # hidden subtrees (other tabs, collapsed menus, off-screen content)
        here = in_page or node["role"] == "document web"
        keep = interesting(node) and (here or not page_only)
        if keep and query:
            keep = query in node["name"].lower() or query in (node.get("value") or "").lower()
        if keep:
            if len(out) >= max_nodes:
                truncated = True
                return
            out.append(node)
        for i, child in children(obj):
            walk(child, f"{p}.{i}", depth + 1, here)

    walk(frame, path, 0, False)
    frame_node = describe(frame, path, 0) or {}
    title = req.get("window")
    return {"window": frame.get_name(), "app": app.get_name(), "pid": app.get_process_id(),
            "nodes": out, "truncated": truncated,
            # Only needed to warn about an ambiguous title; enumerating the desktop on every poll
            # would make the settle expensive.
            "matches": matching_windows(title) if title else 0,
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
    return {"action": None}


def full_text(obj):
    return Atspi.Text.get_text(obj, 0, Atspi.Text.get_character_count(obj))


def set_value(req):
    """Write a value and read it back, so the answer is what the app holds, not what was sent."""
    obj = find(req["path"])
    if obj is None:
        return GONE
    role, text = obj.get_role_name() or "", req["text"]
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
    elif obj.get_editable_text_iface() is not None and obj.get_text_iface() is not None:
        if not obj.get_state_set().contains(Atspi.StateType.EDITABLE):
            return {"error": "this field is read-only"}
        if not Atspi.EditableText.set_text_contents(obj, text):
            return {"error": "the app refused the new text"}
        read, same = lambda: full_text(obj), lambda got: got == text
    else:
        return {"error": f"{role} has no settable value: use click_element, then type_text"}
    got = read()
    for _ in range(10):  # the app may apply it a moment later
        if same(got):
            return {"verified": True}
        time.sleep(0.05)
        got = read()
    return {"verified": False, "value": str(got)[:MAX_TEXT]}


def apps(_req):
    return {"apps": [{"name": app.get_name(), "pid": app.get_process_id(),
                      "windows": [f.get_name() for _, f in children(app)]}
                     for _, app in children(desktop())]}


def handle(req):
    ops = {"apps": apps, "tree": tree, "resolve": resolve, "action": action, "set": set_value}
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
