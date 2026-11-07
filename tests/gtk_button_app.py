"""A window with one big button that counts its clicks in a file: python3 gtk_button_app.py STATE_FILE TITLE."""

import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

state_path, title = sys.argv[1], sys.argv[2]
count = 0


def clicked(_button):
    global count
    count += 1
    with open(state_path, "w") as fh:
        fh.write(f"CLICKED {count}")


win = Gtk.Window(title=title)
win.set_default_size(300, 200)
button = Gtk.Button(label="Go")
button.connect("clicked", clicked)
win.add(button)
win.show_all()
with open(state_path, "w") as fh:
    fh.write("CLICKED 0")
win.connect("destroy", Gtk.main_quit)
Gtk.main()
