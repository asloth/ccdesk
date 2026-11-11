"""One X error handler for the whole process, shared by every ctypes Display ccdesk opens.

Xlib's default handler exits the process on any protocol error, and XSetErrorHandler is
process-wide: a second module installing its own would silently replace the first one's. So each
module registers its Display here and gets its errors recorded on its own slot instead.
"""

from __future__ import annotations

import ctypes as C
import threading


class _XErrorEvent(C.Structure):
    _fields_ = [("type", C.c_int), ("display", C.c_void_p), ("resourceid", C.c_ulong), ("serial", C.c_ulong),
                ("error_code", C.c_ubyte), ("request_code", C.c_ubyte), ("minor_code", C.c_ubyte)]


_PROTO = C.CFUNCTYPE(C.c_int, C.c_void_p, C.POINTER(_XErrorEvent))
_sinks: dict[int, object] = {}  # Display pointer -> callable(message)
_lock = threading.Lock()
_installed = None  # keeps the ctypes callback alive


def _on_error(dpy, ev):
    sink = _sinks.get(dpy or 0)
    if sink is not None:
        sink(f"X error {ev.contents.error_code} in request {ev.contents.request_code}")
    return 0


def register(xlib, dpy: int, sink) -> None:
    """Send protocol errors on Display dpy to sink(message), and never exit the process on one."""
    global _installed
    with _lock:
        _sinks[dpy] = sink
        if _installed is None:
            _installed = _PROTO(_on_error)
            xlib.XSetErrorHandler(C.cast(_installed, C.c_void_p))


def unregister(dpy: int) -> None:
    with _lock:
        _sinks.pop(dpy, None)
