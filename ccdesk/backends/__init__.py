"""Backend selection. Detection happens at runtime from the environment."""

from __future__ import annotations

import os

from ccdesk.backends.base import Backend
from ccdesk.backends.kde import KdeBackend
from ccdesk.backends.wayland import WaylandBackend
from ccdesk.backends.x11 import X11Backend

__all__ = ["Backend", "KdeBackend", "WaylandBackend", "X11Backend", "detect_session", "get_backend",
           "is_kde"]


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


def is_kde(env: dict | None = None) -> bool:
    """Whether this is KDE Plasma. XDG_CURRENT_DESKTOP lists several names, colon-separated."""
    env = os.environ if env is None else env
    parts = [p.strip().upper() for p in (env.get("XDG_CURRENT_DESKTOP") or "").split(":")]
    return "KDE" in parts or "PLASMA" in parts


def get_backend(name: str | None = None, env: dict | None = None) -> Backend:
    session = name or detect_session(env)
    if session == "x11":
        return X11Backend(env=env)
    if session == "wayland":
        # Plasma needs its own backend: grim can't grab the screen and windows need kdotool.
        if is_kde(env):
            return KdeBackend(env=env)
        return WaylandBackend(env=env)
    return Backend(env=env)
