"""Tk fixture app for the xvfb integration test.

Usage: python tk_app.py STATE_FILE. Widgets use flat, distinctive colours so the test can
find them in a screenshot. Every state change is written to STATE_FILE as JSON.
"""

import json
import os
import sys
import tkinter as tk

TITLE = "ccdesk-test"
BUTTON_BG = "#ff00ff"
LABEL_BEFORE = "#ff0000"
LABEL_AFTER = "#00ff00"
ENTRY_BG = "#ffff00"
CANVAS_BG = "#0000ff"

state_path = sys.argv[1]
state = {"ready": False, "label": "waiting", "entry": "", "drag": {"press": None, "motion": 0, "release": None},
         "list_top": 0.0}

root = tk.Tk()
root.title(TITLE)
root.geometry("420x420+40+50")
root.configure(bg="#808080")


def save():
    state["entry"] = entry.get()
    state["list_top"] = listbox.yview()[0]
    tmp = state_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, state_path)


def set_label(text, colour):
    state["label"] = text
    label.configure(text=text, bg=colour)
    save()


flat = dict(relief="flat", bd=0, highlightthickness=0)
button = tk.Button(root, text="Press me", bg=BUTTON_BG, activebackground=BUTTON_BG,
                   command=lambda: set_label("clicked", LABEL_AFTER), width=12, height=2, **flat)
button.pack(pady=8)
label = tk.Label(root, text="waiting", bg=LABEL_BEFORE, width=30, height=2)
label.pack(pady=4)
entry = tk.Entry(root, bg=ENTRY_BG, width=30, **flat)
entry.pack(pady=8, ipady=6)
entry.bind("<Return>", lambda e: set_label("submitted:" + entry.get(), LABEL_AFTER))
entry.bind("<KeyRelease>", lambda e: save())

canvas = tk.Canvas(root, bg=CANVAS_BG, width=300, height=60, **flat)
canvas.pack(pady=8)


def drag_event(kind):
    def handler(e):
        if kind == "motion":
            state["drag"]["motion"] += 1
        else:
            state["drag"][kind] = [e.x_root, e.y_root]
        save()
    return handler


canvas.bind("<ButtonPress-1>", drag_event("press"))
canvas.bind("<B1-Motion>", drag_event("motion"))
canvas.bind("<ButtonRelease-1>", drag_event("release"))

listbox = tk.Listbox(root, height=5, width=30)
for i in range(200):
    listbox.insert("end", f"row {i}")
listbox.pack(pady=4)
listbox.bind("<MouseWheel>", lambda e: root.after(50, save))


def poll():
    save()
    root.after(100, poll)


def ready():
    state["ready"] = True
    state["button"] = [button.winfo_rootx(), button.winfo_rooty(), button.winfo_width(), button.winfo_height()]
    state["listbox"] = [listbox.winfo_rootx(), listbox.winfo_rooty(), listbox.winfo_width(), listbox.winfo_height()]
    poll()


root.after(300, ready)
root.mainloop()
