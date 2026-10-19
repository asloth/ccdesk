"""GTK 3 fixture app for the accessibility-tree integration test. Run with a system Python
that has PyGObject: python3 gtk_app.py STATE_FILE. Writes its state to STATE_FILE as JSON."""

import json
import os
import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

state_path = sys.argv[1]
state = {"ready": False, "clicks": 0, "agree": False, "entry": ""}


def save(*_):
    state["entry"] = entry.get_text()
    state["agree"] = check.get_active()
    tmp = state_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, state_path)


def clicked(_):
    state["clicks"] += 1
    label.set_text(f"clicked {state['clicks']}")
    save()


win = Gtk.Window(title="ccdesk-gtk")
win.move(300, 200)
win.connect("destroy", Gtk.main_quit)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
button = Gtk.Button(label="Apply settings")
button.connect("clicked", clicked)
label = Gtk.Label(label="idle")
entry = Gtk.Entry()
entry.get_accessible().set_name("Display name")
entry.connect("changed", save)
check = Gtk.CheckButton(label="I agree")
check.connect("toggled", save)
for w in (button, label, entry, check):
    box.pack_start(w, False, False, 0)
win.add(box)
win.show_all()


def ready():
    state["ready"] = True
    save()
    return False


GLib.timeout_add(500, ready)
Gtk.main()
