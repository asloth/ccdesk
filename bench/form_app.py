"""GTK 3 signup form for the token benchmark. Run with a system Python that has PyGObject:
python3 form_app.py STATE_FILE. Writes what was submitted to STATE_FILE as JSON."""

import json
import os
import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

state_path = sys.argv[1]

win = Gtk.Window(title="Signup")
win.set_default_size(560, 640)
win.move(400, 150)
win.connect("destroy", Gtk.main_quit)
grid = Gtk.Grid(column_spacing=12, row_spacing=10, margin=20)
win.add(grid)

intro = Gtk.Label(label="Create your account. Fields marked * are required. We will never share "
                        "your email. You can change your plan at any time from Settings.")
intro.set_line_wrap(True)
intro.set_max_width_chars(60)
grid.attach(intro, 0, 0, 2, 1)

entries = {}
for row, name in enumerate(["Full name *", "Email *", "Phone", "Company", "Website"], start=1):
    grid.attach(Gtk.Label(label=name, xalign=0), 0, row, 1, 1)
    entry = Gtk.Entry(hexpand=True)
    entry.get_accessible().set_name(name.rstrip(" *"))
    grid.attach(entry, 1, row, 1, 1)
    entries[name.rstrip(" *")] = entry

grid.attach(Gtk.Label(label="Plan", xalign=0), 0, 6, 1, 1)
plans = Gtk.Box(spacing=12)
free = Gtk.RadioButton.new_with_label(None, "Free")
pro = Gtk.RadioButton.new_with_label_from_widget(free, "Pro")
team = Gtk.RadioButton.new_with_label_from_widget(free, "Team")
for b in (free, pro, team):
    plans.pack_start(b, False, False, 0)
grid.attach(plans, 1, 6, 1, 1)

news = Gtk.CheckButton(label="Send me the newsletter")
news.set_active(True)
grid.attach(news, 1, 7, 1, 1)
terms = Gtk.CheckButton(label="I accept the terms")
grid.attach(terms, 1, 8, 1, 1)

status = Gtk.Label(label="Not submitted yet", xalign=0)
cancel = Gtk.Button(label="Cancel")
submit = Gtk.Button(label="Submit")
buttons = Gtk.Box(spacing=8)
buttons.pack_end(submit, False, False, 0)
buttons.pack_end(cancel, False, False, 0)
grid.attach(buttons, 1, 9, 1, 1)
grid.attach(status, 0, 10, 2, 1)


def on_submit(_):
    data = {k: e.get_text() for k, e in entries.items()}
    data["plan"] = next(b.get_label() for b in (free, pro, team) if b.get_active())
    data["newsletter"] = news.get_active()
    data["terms"] = terms.get_active()
    if not data["Full name"] or not data["Email"] or not data["terms"]:
        status.set_text("Error: fill the required fields and accept the terms")
        return
    tmp = state_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, state_path)
    status.set_text("Thanks! Your account was created.")


submit.connect("clicked", on_submit)
cancel.connect("clicked", lambda _: status.set_text("Cancelled"))
win.show_all()
Gtk.main()
