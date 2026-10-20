"""AT-SPI helper, run as a separate process by a Python that has PyGObject (usually /usr/bin/python3).

ccdesk itself stays standard-library only: it starts this file with `python3 -I a11y_helper.py
'<json request>'` and reads one JSON object from stdout. It must not import ccdesk.

Requests:
  {"op": "apps"}
  {"op": "tree", "window": str|None, "query": str|None, "page": bool, "max_nodes": int}
  {"op": "resolve", "path": "3.0.1.4"}
"""

import json
import sys

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
        return None, "no application is publishing an accessibility tree (see ccdesk doctor)"
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


def tree(req):
    picked, err = pick_window(req.get("window"))
    if err:
        return {"error": err}
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
    return {"window": frame.get_name(), "app": app.get_name(), "pid": app.get_process_id(),
            "nodes": out, "truncated": truncated, "matches": matching_windows(req.get("window"))}


def resolve(req):
    parts = [int(x) for x in str(req["path"]).split(".")]
    obj = desktop()
    for i in parts:
        try:
            obj = obj.get_child_at_index(i)
        except Exception:
            obj = None
        if obj is None:
            return {"error": "element is gone; call ui_tree() again"}
    return {"node": describe(obj, req["path"], 0)}


def apps(_req):
    return {"apps": [{"name": app.get_name(), "pid": app.get_process_id(),
                      "windows": [f.get_name() for _, f in children(app)]}
                     for _, app in children(desktop())]}


def main():
    req = json.loads(sys.argv[1])
    ops = {"apps": apps, "tree": tree, "resolve": resolve}
    try:
        result = ops[req["op"]](req)
    except Exception as exc:  # report, never traceback into the MCP server's stdout parsing
        result = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
