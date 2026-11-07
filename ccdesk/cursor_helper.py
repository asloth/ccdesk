"""The drawn cursor of one agent: an arrow and a name pill in a small override-redirect window.

Run with a system Python that has PyGObject (see ccdesk.cursor). Reads lines on stdin:
`move X Y` (the window's top-left corner glides there), `hide`, `show`. Exits when stdin closes.
Usage: cursor_helper.py NAME '#RRGGBB'

Only the arrow and pill pixels exist (XShape), and the input shape is empty, so the window never
takes a click and never answers a "which window is under the pointer" query.
"""

import sys
import threading

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

GLIDE_MS = 120
STEPS = 8
ARROW = [(0, 0), (0, 17), (4.5, 13), (8, 20.5), (11, 19), (7.5, 12), (13, 12)]


def parse_color(text: str) -> tuple[float, float, float]:
    h = text.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


class Cursor(Gtk.Window):
    def __init__(self, name: str, color: str):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.set_title(f"ccdesk-cursor:{name}")
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_accept_focus(False)
        self.name_text, self.rgb = name, parse_color(color)
        probe = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
        cr = cairo.Context(probe)
        cr.select_font_face("sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(11)
        ext = cr.text_extents(name)
        self.text_w, self.text_h = ext.x_advance, ext.height
        self.pill = (12, 17, int(self.text_w) + 12, int(self.text_h) + 8)  # x, y, w, h
        self.w = self.pill[0] + self.pill[2] + 1
        self.h = self.pill[1] + self.pill[3] + 1
        self.set_default_size(self.w, self.h)
        self.set_size_request(self.w, self.h)
        self.set_app_paintable(True)
        self.connect("draw", lambda _w, cr: self.paint(cr) or False)
        self.connect("realize", self.on_realize)
        self.pos = (0, 0)
        self.glide_id = None

    def paint(self, cr) -> None:
        cr.set_antialias(cairo.ANTIALIAS_NONE if cr.get_target().get_content() == cairo.CONTENT_ALPHA else cairo.ANTIALIAS_DEFAULT)
        px, py, pw, ph = self.pill
        r = ph / 2
        cr.new_sub_path()
        cr.arc(px + r, py + r, r, 1.5707963, 4.712389)
        cr.arc(px + pw - r, py + r, r, -1.5707963, 1.5707963)
        cr.close_path()
        cr.set_source_rgb(*self.rgb)
        cr.fill()
        cr.select_font_face("sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(11)
        cr.set_source_rgb(1, 1, 1)
        cr.move_to(px + 6, py + 4 + self.text_h)
        cr.show_text(self.name_text)
        cr.move_to(*ARROW[0])
        for pt in ARROW[1:]:
            cr.line_to(*pt)
        cr.close_path()
        cr.set_source_rgb(*self.rgb)
        cr.fill_preserve()
        cr.set_source_rgb(1, 1, 1)
        cr.set_line_width(1.3)
        cr.stroke()

    def on_realize(self, _w) -> None:
        # Shape: every pixel the drawing touches. Input shape: nothing, so clicks pass through.
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.w, self.h)
        cr = cairo.Context(surface)
        self.paint(cr)
        gdk_win = self.get_window()
        gdk_win.shape_combine_region(Gdk.cairo_region_create_from_surface(surface), 0, 0)
        gdk_win.input_shape_combine_region(cairo.Region(), 0, 0)

    def put(self, x: int, y: int) -> None:
        self.pos = (x, y)
        self.move(x, y)
        gdk_win = self.get_window()
        if gdk_win is not None:
            gdk_win.raise_()

    def glide_to(self, x: int, y: int) -> None:
        if self.glide_id is not None:
            GLib.source_remove(self.glide_id)
            self.glide_id = None
        if not self.get_visible():
            self.show()
            self.put(x, y)
            return
        x0, y0 = self.pos
        step = [0]

        def tick():
            step[0] += 1
            t = step[0] / STEPS
            t = 1 - (1 - t) ** 2
            self.put(round(x0 + (x - x0) * t), round(y0 + (y - y0) * t))
            if step[0] >= STEPS:
                self.glide_id = None
                return False
            return True

        self.glide_id = GLib.timeout_add(GLIDE_MS // STEPS, tick)


def main() -> None:
    cursor = Cursor(sys.argv[1], sys.argv[2])

    def handle(line: str) -> bool:
        parts = line.split()
        if parts[:1] == ["move"] and len(parts) == 3:
            cursor.glide_to(int(parts[1]), int(parts[2]))
        elif parts == ["hide"]:
            cursor.hide()
        elif parts == ["show"]:
            cursor.show()
            cursor.put(*cursor.pos)
        return False

    def reader() -> None:
        for line in sys.stdin:
            GLib.idle_add(handle, line)
        GLib.idle_add(Gtk.main_quit)

    threading.Thread(target=reader, daemon=True).start()
    Gtk.main()


if __name__ == "__main__":
    main()
