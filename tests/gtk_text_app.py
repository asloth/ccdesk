"""A multi-line text box that keeps its contents in a file: python3 gtk_text_app.py STATE_FILE."""

import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

state_path = sys.argv[1]
win = Gtk.Window(title="ccdesk-text")
view = Gtk.TextView()
win.add(view)
win.show_all()
view.grab_focus()


def save():
    buf = view.get_buffer()
    with open(state_path, "w") as fh:
        fh.write(buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True))
    return True


GLib.timeout_add(100, save)
win.connect("destroy", Gtk.main_quit)
Gtk.main()
