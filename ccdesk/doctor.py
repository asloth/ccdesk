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


def build_report(backend: Backend, policy: Policy, audit_path: str | Path) -> dict:
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
        "warnings": [],
    }
    warnings = report["warnings"]
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
    if report["mcp"] is None:
        warnings.append("python package 'mcp' is not installed: `ccdesk mcp` will not start")
    if report["cua_driver"]:
        warnings.append(
            f"cua-driver found at {report['cua_driver']}; ccdesk works independently and does not use it"
        )
    return report


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
        f"cua-driver:   {report['cua_driver'] or 'not found'}",
        f"policy:       read_only={report['policy']['read_only']} allow_launch={report['policy']['allow_launch']}",
        f"audit log:    {report['audit_log']}",
    ]
    if report["policy"]["confirm_keys"]:
        lines.insert(-1, f"confirm keys: {', '.join(report['policy']['confirm_keys'])} (plus the built-in list)")
    if report["warnings"]:
        lines += ["", "Warnings"] + [f"  ! {w}" for w in report["warnings"]]
    return "\n".join(lines)
