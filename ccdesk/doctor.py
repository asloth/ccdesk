"""Environment report for `ccdesk doctor` and the doctor() tool."""

from __future__ import annotations

import importlib.metadata
import platform
from pathlib import Path

from ccdesk import __version__
from ccdesk.backends import Backend, detect_session
from ccdesk.errors import CcdeskError
from ccdesk.safety import Policy

SESSION_VARS = ("XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "DISPLAY", "WAYLAND_DISPLAY")


def _mcp_version() -> str | None:
    try:
        return importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        return None


def build_report(backend: Backend, policy: Policy, audit_path: str | Path, a11y=None,
                 headless=None) -> dict:
    env = backend.env
    problem = backend.available()
    tools = backend.tools_report()
    report: dict = {
        "ccdesk": __version__,
        "python": platform.python_version(),
        "session": {
            "detected": detect_session(env),
            **{var: env.get(var) for var in SESSION_VARS},
        },
        "backend": backend.name,
        "ready": problem is None,
        "tools": tools,
        "missing": sorted(cap for cap, found in tools.items() if not any(found.values())),
        "screen": None,
        "monitors": [],
        "scale": {},
        "cua_driver": backend.which("cua-driver"),
        "mcp": _mcp_version(),
        "policy": {"read_only": policy.read_only, "allow_launch": policy.allow_launch,
                   "confirm_keys": [str(c) for c in policy.confirm_keys]},
        "audit_log": str(audit_path),
        "headless": headless.info() if headless else None,
        "warnings": [],
    }
    warnings = report["warnings"]
    if headless:
        warnings.extend(headless.notes)
    if problem:
        warnings.append(problem)
    else:
        try:
            w, h = backend.screen_size()
            report["screen"] = {"width": w, "height": h}
        except CcdeskError as exc:
            warnings.append(f"cannot read screen size: {exc}")
        report["scale"] = backend.scale_info()
        report["monitors"] = backend.monitors()
    for cap in report["missing"]:
        names = " or ".join(backend.TOOLS[cap])
        warnings.append(f"no tool for {cap}: install {names}")
    if backend.name == "wayland":
        warnings.append(
            "Wayland: input needs ydotoold running with /dev/uinput access; "
            "window listing only works on sway/Hyprland or for XWayland apps"
        )
        if env.get("DISPLAY"):
            warnings.append("XWayland is available: set CCDESK_BACKEND=x11 to drive X11 apps only")
    if backend.name == "x11" and not tools.get("windows", {}).get("wmctrl"):
        if tools.get("windows", {}).get("xprop"):
            warnings.append("wmctrl not found: window listing falls back to xdotool + xprop")
        else:
            warnings.append("wmctrl not found: window listing falls back to xdotool (no desktop numbers)")
    report["a11y"] = _a11y_report(a11y, warnings, private=bool(headless and headless.private_bus))
    if report["mcp"] is None:
        warnings.append("python package 'mcp' is not installed: `ccdesk mcp` will not start")
    if report["cua_driver"]:
        warnings.append(
            f"cua-driver found at {report['cua_driver']}; ccdesk works independently and does not use it"
        )
    return report


A11Y_HINT = (
    "apps missing from the accessibility tree need it switched on before they start: GTK apps and "
    "Firefox read `gsettings set org.gnome.desktop.interface toolkit-accessibility true` "
    "(or GNOME_ACCESSIBILITY=1); Chromium and Electron apps need --force-renderer-accessibility"
)


def _a11y_report(a11y, warnings: list[str], private: bool = False) -> dict:
    if a11y is None:
        from ccdesk.a11y import A11y

        a11y = A11y()
    try:
        apps = a11y.apps()
    except CcdeskError as exc:
        warnings.append(f"ui_tree unavailable: {exc}")
        return {"ready": False, "apps": [], "error": str(exc)}
    names = [a["name"] for a in apps if a.get("windows")]
    if not any(n.lower() in ("firefox", "chromium", "google-chrome") for n in names) and not private:
        warnings.append(A11Y_HINT)
    return {"ready": True, "apps": names}


def format_report(report: dict) -> str:
    s = report["session"]
    lines = [
        f"ccdesk {report['ccdesk']} (python {report['python']}, mcp {report['mcp'] or 'not installed'})",
        "",
        "Session",
        f"  detected:            {s['detected']}",
    ]
    for var in SESSION_VARS:
        lines.append(f"  {var + ':':<20} {s.get(var) or '-'}")
    if report.get("headless"):
        hl = report["headless"]
        lines.append(f"  headless:            {hl['display']} {hl['size']}, wm {hl['window_manager'] or 'none'}")
    lines += ["", f"Backend: {report['backend']} ({'ready' if report['ready'] else 'not ready'})"]
    screen = report["screen"]
    screen_text = "{width}x{height}".format(**screen) if screen else "unknown"
    scale_text = ", ".join(f"{k}={v}" for k, v in report["scale"].items()) or "1.0 (no scaling hints)"
    lines.append(f"  screen:              {screen_text}")
    lines.append(f"  scale:               {scale_text}")
    for m in report["monitors"]:
        lines.append("  monitor:             {name} {width}x{height}+{x}+{y}{p}".format(
            **m, p=" (primary)" if m["primary"] else ""))
    lines += ["", "Tools"]
    for cap, found in report["tools"].items():
        parts = [f"{name} ({path})" if path else f"{name} (missing)" for name, path in found.items()]
        mark = "ok " if any(found.values()) else "-- "
        lines.append(f"  {mark}{cap + ':':<12} {', '.join(parts)}")
    lines += [
        "",
        f"ui_tree:      {', '.join(report['a11y']['apps']) or 'no apps' if report['a11y']['ready'] else 'unavailable'}",
        f"cua-driver:   {report['cua_driver'] or 'not found'}",
        f"policy:       read_only={report['policy']['read_only']} allow_launch={report['policy']['allow_launch']}",
        f"audit log:    {report['audit_log']}",
    ]
    if report["policy"]["confirm_keys"]:
        lines.insert(-1, f"confirm keys: {', '.join(report['policy']['confirm_keys'])} (plus the built-in list)")
    if report["warnings"]:
        lines += ["", "Warnings"] + [f"  ! {w}" for w in report["warnings"]]
    return "\n".join(lines)
