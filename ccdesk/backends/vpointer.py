"""A virtual mouse through the wlr-virtual-pointer Wayland protocol, spoken directly over the socket.

Hyprland, sway, river and Wayfire offer it. It needs no root, daemon or extra package, and unlike
warping the cursor it sends real motion events, so an app knows where the pointer is before the
press. The device lives as long as the connection. Closing it first lets go of any button still
down, since the protocol does not promise that destroying the device does.
"""

from __future__ import annotations

import os
import select
import socket
import struct
import time

from ccdesk.errors import CcdeskError

MANAGER = "zwlr_virtual_pointer_manager_v1"
TIMEOUT = 5.0
# Requests, in the order the protocol lists them.
MOTION_ABSOLUTE, BUTTON, FRAME, AXIS_SOURCE, AXIS_DISCRETE, DESTROY = 1, 2, 4, 5, 7, 8
LINUX_BUTTON = {1: 0x110, 2: 0x112, 3: 0x111}  # X-style numbers to BTN_LEFT, BTN_MIDDLE, BTN_RIGHT
WHEEL_SOURCE = 0
VERTICAL, HORIZONTAL = 0, 1
WHEEL_STEP = 15  # what one wheel click measures in wl_pointer's units


class VirtualPointerError(CcdeskError):
    """The connection failed, or the compositor refused the virtual pointer."""


class NoVirtualPointer(VirtualPointerError):
    """This compositor does not offer the protocol, and that won't change while it runs."""


def _string(text: str) -> bytes:
    data = text.encode() + b"\0"
    return struct.pack("<I", len(data)) + data + b"\0" * (-len(data) % 4)


def _message(obj: int, opcode: int, body: bytes = b"") -> bytes:
    return struct.pack("<II", obj, (len(body) + 8) << 16 | opcode) + body


def _fixed(value: int) -> int:
    return value * 256


class VirtualPointer:
    DISPLAY, REGISTRY = 1, 2

    def __init__(self, env: dict):
        display = env.get("WAYLAND_DISPLAY") or "wayland-0"
        path = display if display.startswith("/") else os.path.join(env.get("XDG_RUNTIME_DIR") or "", display)
        self._last_id = self.REGISTRY  # a compositor only accepts new ids in strict order
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(TIMEOUT)
        self._buffer = b""
        self._held: set[int] = set()
        self._globals: dict[str, tuple[int, int]] = {}
        try:
            self._sock.connect(path)
            self._send(_message(self.DISPLAY, 1, struct.pack("<I", self.REGISTRY)))  # get_registry
            self._roundtrip()
            if MANAGER not in self._globals:
                raise NoVirtualPointer(f"this compositor does not offer {MANAGER}")
            name, version = self._globals[MANAGER]
            manager = self._new_id()
            bind = struct.pack("<I", name) + _string(MANAGER) + struct.pack("<II", min(version, 2), manager)
            self._send(_message(self.REGISTRY, 0, bind))
            self.pointer = self._new_id()
            # A null seat means the default one.
            self._send(_message(manager, 0, struct.pack("<II", 0, self.pointer)))
            self._roundtrip()
        except OSError as exc:
            self._sock.close()
            raise VirtualPointerError(f"cannot talk to the compositor at {path}: {exc}") from None
        except CcdeskError:
            self._sock.close()
            raise

    def __enter__(self) -> VirtualPointer:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            for button in list(self._held):
                self.button(button, False)
            self._send(_message(self.pointer, DESTROY))
            self._roundtrip()  # so everything sent has been applied when this returns
        except (OSError, CcdeskError):
            pass
        finally:
            self._sock.close()

    def _new_id(self) -> int:
        self._last_id += 1
        return self._last_id

    def _send(self, data: bytes) -> None:
        self._sock.sendall(data)

    def _roundtrip(self) -> None:
        callback = self._new_id()
        self._send(_message(self.DISPLAY, 0, struct.pack("<I", callback)))  # sync
        deadline = time.monotonic() + TIMEOUT
        while True:
            for obj, opcode, body in self._read_messages():
                if obj == self.DISPLAY and opcode == 0:  # wl_display.error
                    _, code = struct.unpack_from("<II", body)
                    raise VirtualPointerError(f"the compositor refused the virtual pointer (error {code})")
                if obj == self.REGISTRY and opcode == 0:  # wl_registry.global
                    self._on_global(body)
                if obj == callback:
                    return
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self._sock], [], [], remaining)[0]:
                raise VirtualPointerError("the compositor did not answer")
            data = self._sock.recv(65536)
            if not data:
                raise VirtualPointerError("the compositor closed the connection")
            self._buffer += data

    def _read_messages(self):
        while len(self._buffer) >= 8:
            obj, word = struct.unpack_from("<II", self._buffer)
            size = word >> 16
            if size < 8 or len(self._buffer) < size:
                return
            body, self._buffer = self._buffer[8:size], self._buffer[size:]
            yield obj, word & 0xFFFF, body

    def _on_global(self, body: bytes) -> None:
        name, length = struct.unpack_from("<II", body)
        interface = body[8:8 + length - 1].decode(errors="replace")
        version = struct.unpack_from("<I", body, 8 + length + (-length % 4))[0]
        self._globals[interface] = (name, version)

    @staticmethod
    def _time() -> int:
        return int(time.monotonic() * 1000) & 0xFFFFFFFF

    def _frame(self, *messages: bytes) -> None:
        self._send(b"".join(messages) + _message(self.pointer, FRAME))

    def move(self, x: int, y: int, width: int, height: int) -> None:
        """To (x, y) in a layout of width x height."""
        self._frame(_message(self.pointer, MOTION_ABSOLUTE, struct.pack("<IIIII", self._time(), x, y, width, height)))

    def button(self, button: int, down: bool) -> None:
        if down:
            self._held.add(button)  # before sending: a failure may still have pressed it
        body = struct.pack("<III", self._time(), LINUX_BUTTON[button], 1 if down else 0)
        self._frame(_message(self.pointer, BUTTON, body))
        if not down:
            self._held.discard(button)  # after sending: a failed release is retried on close

    def scroll(self, dx: int, dy: int) -> None:
        """Whole wheel clicks: dy > 0 is down, dx > 0 is right."""
        for axis, amount in ((VERTICAL, dy), (HORIZONTAL, dx)):
            step = 1 if amount > 0 else -1
            for _ in range(abs(amount)):
                body = struct.pack("<IIii", self._time(), axis, _fixed(step * WHEEL_STEP), step)
                self._frame(_message(self.pointer, AXIS_SOURCE, struct.pack("<I", WHEEL_SOURCE)),
                            _message(self.pointer, AXIS_DISCRETE, body))
