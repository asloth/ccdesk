"""GTK app for the background input test, launched only on a private Xvfb."""
import json
import os
import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

path, role = sys.argv[1:]
state = {"clicks": 0, "focus_events": 0}
win = Gtk.Window(title="ccdesk-cua-" + role)
win.set_default_size(300, 180)
win.move(30 if role == "target" else 650, 50)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20, margin=20)
button = Gtk.Button(label="Count")
entry = Gtk.Entry()
box.pack_start(button, False, False, 0)
box.pack_start(entry, False, False, 0)
win.add(box)


def save():
    state["text"] = entry.get_text()
    state["ready"] = True
    ox, oy = win.get_position()
    for name, widget in (("button", button), ("entry", entry)):
        x, y = widget.translate_coordinates(win, 0, 0)
        state[name] = [ox + x + widget.get_allocated_width() // 2,
                       oy + y + widget.get_allocated_height() // 2]
    with open(path + ".tmp", "w") as fh:
        json.dump(state, fh)
    os.replace(path + ".tmp", path)
    return True


def clicked(*_):
    state["clicks"] += 1
    save()


def focus_event(*_):
    state["focus_events"] += 1
    return False


button.connect("clicked", clicked)
win.connect("focus-in-event", focus_event)
win.connect("focus-out-event", focus_event)
win.connect("destroy", Gtk.main_quit)
win.show_all()
entry.grab_focus()
GLib.timeout_add(100, save)
Gtk.main()
