"""Private GTK clipboard owner for a Calc paste transaction, run by a11y_helper.

Save every advertised data target before changing CLIPBOARD. On restore (or parent
EOF), keep serving the previous bytes until another application owns CLIPBOARD.
GTK's set_with_data is not introspectable, so only that API uses ctypes.
"""
import ctypes as C
import ctypes.util
import json
import sys


def snapshot(clipboard):
    """Fail before replacing CLIPBOARD if any original format cannot be saved."""
    ok, targets = clipboard.wait_for_targets()
    saved = {}
    if ok:
        for target in targets:
            name = target.name()
            if name in {"TARGETS", "TIMESTAMP", "MULTIPLE", "SAVE_TARGETS"}:
                continue
            data = clipboard.wait_for_contents(target)
            if data is None or data.get_length() < 0:
                raise ValueError(f"cannot save clipboard target {name}")
            saved[name] = (bytes(data.get_data()), data.get_format(), data.get_data_type().name())
            if len(saved) > 128 or sum(len(v[0]) for v in saved.values()) > 16 * 1024 * 1024:
                raise ValueError("clipboard is too large to save safely")

    return saved


def main():
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, GLib, Gtk

    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    saved = snapshot(clipboard)

    gtk = C.CDLL(ctypes.util.find_library("gtk-3"))
    gdk = C.CDLL(ctypes.util.find_library("gdk-3"))
    gdk.gdk_atom_intern.argtypes = [C.c_char_p, C.c_int]
    gdk.gdk_atom_intern.restype = C.c_void_p
    atom = lambda name: gdk.gdk_atom_intern(name.encode(), 0)
    gtk.gtk_clipboard_get.argtypes = [C.c_void_p]
    gtk.gtk_clipboard_get.restype = C.c_void_p
    ptr = gtk.gtk_clipboard_get(atom("CLIPBOARD"))
    class Entry(C.Structure):
        _fields_ = [("target", C.c_char_p), ("flags", C.c_uint), ("info", C.c_uint)]
    Get = C.CFUNCTYPE(None, C.c_void_p, C.c_void_p, C.c_uint, C.c_void_p)
    Clear = C.CFUNCTYPE(None, C.c_void_p, C.c_void_p)
    gtk.gtk_clipboard_set_with_data.argtypes = [C.c_void_p, C.POINTER(Entry), C.c_uint, Get, Clear, C.c_void_p]
    gtk.gtk_clipboard_set_with_data.restype = C.c_int
    gtk.gtk_selection_data_set.argtypes = [C.c_void_p, C.c_void_p, C.c_int, C.c_void_p, C.c_int]
    gtk.gtk_clipboard_clear.argtypes = [C.c_void_p]
    formats = []
    replacing, restored, lost = False, False, False

    @Get
    def get_data(_clip, selection, info, _user):
        _name, (data, bits, data_type) = formats[info]
        gtk.gtk_selection_data_set(selection, atom(data_type), bits, C.c_char_p(data), len(data))

    @Clear
    def clear(_clip, _user):
        nonlocal lost
        if not replacing:
            lost = True
            if restored:
                Gtk.main_quit()

    def own(payload):
        nonlocal formats, replacing, lost
        replacing = True
        try:
            gtk.gtk_clipboard_clear(ptr)
            formats = list(payload.items())
            if formats:
                entries = (Entry * len(formats))(*(Entry(n.encode(), 0, i) for i, (n, _) in enumerate(formats)))
                if not gtk.gtk_clipboard_set_with_data(ptr, entries, len(entries), get_data, clear, None):
                    raise ValueError("cannot own CLIPBOARD")
            lost = False
        finally:
            replacing = False

    def reply(value):
        try:
            print(json.dumps(value), flush=True)
        except BrokenPipeError:
            pass  # Parent died: still serve the restored clipboard.

    request = json.loads(sys.stdin.readline())
    try:
        own({"UTF8_STRING": (request["text"].encode(), 8, "UTF8_STRING"),
             "text/plain;charset=utf-8": (request["text"].encode(), 8, "UTF8_STRING")})
        reply({"ready": True})
    except Exception:
        own(saved)
        raise

    def restore(_source, _condition):
        nonlocal restored
        try:
            sys.stdin.readline()  # restore request or EOF when the parent exits
            if lost:
                raise ValueError("clipboard ownership changed during paste; current contents preserved")
            own(saved)
            restored = True
            reply({"restored": True})
        except Exception as exc:
            reply({"error": str(exc)})
            Gtk.main_quit()
        if not saved:
            Gtk.main_quit()
        return False

    GLib.io_add_watch(sys.stdin, GLib.IO_IN | GLib.IO_HUP, restore)
    Gtk.main()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error": f"clipboard: {exc}"}), flush=True)
