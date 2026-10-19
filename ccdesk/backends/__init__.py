"""Backend selection. Detection happens at runtime from the environment."""

from __future__ import annotations

import os

from ccdesk.backends.base import Backend
from ccdesk.backends.wayland import WaylandBackend
from ccdesk.backends.x11 import X11Backend

__all__ = ["Backend", "WaylandBackend", "X11Backend", "detect_session", "get_backend"]


def detect_session(env: dict | None = None) -> str:
    """Return "x11", "wayland" or "none". CCDESK_BACKEND=x11|wayland overrides detection."""
    env = os.environ if env is None else env
    forced = (env.get("CCDESK_BACKEND") or "").strip().lower()
    if forced in ("x11", "wayland"):
        return forced
    if env.get("WAYLAND_DISPLAY") or (env.get("XDG_SESSION_TYPE") == "wayland" and not env.get("DISPLAY")):
        return "wayland"
    if env.get("DISPLAY"):
        return "x11"
    return "none"


def get_backend(name: str | None = None, env: dict | None = None) -> Backend:
    session = name or detect_session(env)
    if session == "x11":
        return X11Backend(env=env)
    if session == "wayland":
        return WaylandBackend(env=env)
    return Backend(env=env)
