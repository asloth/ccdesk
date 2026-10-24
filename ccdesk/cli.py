"""Command line entry point: ccdesk doctor | install | mcp | version."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys

from ccdesk import __version__


def _policy_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--allow-launch", action="store_true", help="enable the launch(command) tool")
    p.add_argument("--read-only", action="store_true", help="disable every input tool (screenshots only)")
    p.add_argument("--confirm-keys", metavar="COMBOS", type=_confirm_keys,
                   help='extra key combos that need confirm=true, e.g. "super+x,ctrl+alt+super+p"')
    p.add_argument("--yield-ms", metavar="MS", type=_yield_ms, default=0,
                   help="on your own screen: hold input until you have left mouse and keyboard alone this "
                        "long (e.g. 3000; needs xprintidle). Default 0, never wait")


def _headless_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--headless", action="store_true",
                   help="work on a private invisible screen (Xvfb) instead of yours; implies --allow-launch")
    p.add_argument("--size", default="1920x1080", metavar="WxH", help="headless screen size (default 1920x1080)")
    p.add_argument("--wm", default="auto", metavar="CMD",
                   help='headless window manager: "auto" (default), "none", or a command')


def _confirm_keys(spec: str) -> str:
    from ccdesk.errors import ValidationError
    from ccdesk.parsing import parse_confirm_keys

    try:
        parse_confirm_keys(spec)
    except ValidationError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    return spec


def _yield_ms(spec: str) -> int:
    try:
        ms = int(spec)
    except ValueError:
        ms = -1
    if not 0 <= ms <= 60_000:
        raise argparse.ArgumentTypeError(f"--yield-ms must be 0 to 60000 milliseconds, got {spec!r}")
    return ms


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ccdesk", description="Desktop control for Claude Code on Linux.")
    sub = parser.add_subparsers(dest="command", metavar="{doctor,install,mcp,peek,version}")

    p = sub.add_parser("doctor", help="report session type, helper tools, screen size and scale")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    p.add_argument("--backend", choices=["x11", "wayland"], help="force a backend instead of auto-detecting")

    p = sub.add_parser("install", help="register the MCP server with Claude Code and install the skill")
    p.add_argument("--dry-run", action="store_true", help="print what would be written and run, then exit")
    p.add_argument("--always-load", action="store_true",
                   help="load the tool schemas in every session (saves a round trip when you use the "
                        "desktop, costs ~2.6k tokens per request when you don't)")
    _policy_flags(p)
    _headless_flags(p)

    p = sub.add_parser("mcp", help="run the MCP server over stdio")
    _policy_flags(p)
    p.add_argument("--backend", choices=["x11", "wayland"], help="force a backend instead of auto-detecting")
    p.add_argument("--audit-log", metavar="PATH", help="audit log path (default ~/.cache/ccdesk/audit.log)")
    _headless_flags(p)

    p = sub.add_parser("peek", help="screenshot the headless screens of running ccdesk servers")
    p.add_argument("--open", action="store_true", help="open each screenshot with xdg-open")

    sub.add_parser("version", help="print the version")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "version":
        print(f"ccdesk {__version__}")
        return 0

    if args.command == "doctor":
        from ccdesk.backends import get_backend
        from ccdesk.doctor import build_report, format_report
        from ccdesk.safety import Policy, default_audit_path

        report = build_report(get_backend(args.backend), Policy(), default_audit_path())
        print(json.dumps(report, indent=2) if args.json else format_report(report))
        return 0 if report["ready"] and not report["missing"] else 1

    if args.command == "install":
        from ccdesk.install import install

        flags = [f for f, on in (("--allow-launch", args.allow_launch), ("--read-only", args.read_only)) if on]
        if args.confirm_keys:
            flags += ["--confirm-keys", args.confirm_keys]
        if args.yield_ms:
            flags += ["--yield-ms", str(args.yield_ms)]
        if args.headless:
            flags.append("--headless")
            if args.size != "1920x1080":
                flags += ["--size", args.size]
            if args.wm != "auto":
                flags += ["--wm", args.wm]
        return install(flags, dry_run=args.dry_run, always_load=args.always_load)

    if args.command == "mcp":
        return _mcp(args)

    if args.command == "peek":
        return _peek(args.open)

    parser.print_help()
    return 2


def _mcp(args) -> int:
    from ccdesk.backends import X11Backend, get_backend
    from ccdesk.desktop import Desktop
    from ccdesk.errors import CcdeskError
    from ccdesk.headless import Headless
    from ccdesk.parsing import parse_confirm_keys
    from ccdesk.safety import AuditLog, Policy

    try:
        from ccdesk.server import serve
    except ImportError as exc:
        print(f"ccdesk mcp needs the 'mcp' package: {exc}", file=sys.stderr)
        return 1
    headless = None
    if args.headless:
        try:
            headless = Headless(size=args.size, wm=args.wm)
            backend = X11Backend(env=headless.start())
        except CcdeskError as exc:
            print(f"ccdesk mcp --headless: {exc}", file=sys.stderr)
            return 1
        # A private screen starts empty: the agent has to open the apps it works with.
        args.allow_launch = True
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    else:
        backend = get_backend(args.backend)
    try:
        desktop = Desktop(
            backend=backend,
            policy=Policy(read_only=args.read_only, allow_launch=args.allow_launch,
                          confirm_keys=parse_confirm_keys(args.confirm_keys), yield_ms=args.yield_ms),
            audit=AuditLog(args.audit_log),
            headless=headless,
        )
        desktop.audit.record("server_start", {"read_only": args.read_only, "allow_launch": args.allow_launch,
                                              "confirm_keys": args.confirm_keys, "yield_ms": args.yield_ms,
                                              "backend": desktop.backend.name,
                                              "headless": headless.info() if headless else None}, "ok")
        serve(desktop)
    finally:
        if headless:
            headless.stop()
    return 0


def _peek(open_it: bool) -> int:
    import subprocess

    from ccdesk.backends import X11Backend
    from ccdesk.errors import CcdeskError
    from ccdesk.headless import running
    from ccdesk.safety import cache_dir

    screens = running()
    if not screens:
        print("no headless ccdesk server is running")
        return 1
    for info in screens:
        path = cache_dir() / f"peek-{info['display'].lstrip(':')}.png"
        backend = X11Backend(env={**os.environ, "DISPLAY": info["display"]})
        try:
            backend.screenshot(str(path), None)
        except CcdeskError as exc:
            print(f"{info['display']}: {exc}", file=sys.stderr)
            continue
        print(f"{info['display']} ({info['size']}, server pid {info['pid']}): {path}")
        if open_it:
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0
