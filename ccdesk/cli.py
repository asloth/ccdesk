"""Command line entry point: ccdesk doctor | install | mcp | version."""

from __future__ import annotations

import argparse
import json
import sys

from ccdesk import __version__


def _policy_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--allow-launch", action="store_true", help="enable the launch(command) tool")
    p.add_argument("--read-only", action="store_true", help="disable every input tool (screenshots only)")
    p.add_argument("--confirm-keys", metavar="COMBOS", type=_confirm_keys,
                   help='extra key combos that need confirm=true, e.g. "super+x,ctrl+alt+super+p"')


def _confirm_keys(spec: str) -> str:
    from ccdesk.errors import ValidationError
    from ccdesk.parsing import parse_confirm_keys

    try:
        parse_confirm_keys(spec)
    except ValidationError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    return spec


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ccdesk", description="Desktop control for Claude Code on Linux.")
    sub = parser.add_subparsers(dest="command", metavar="{doctor,install,mcp,version}")

    p = sub.add_parser("doctor", help="report session type, helper tools, screen size and scale")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    p.add_argument("--backend", choices=["x11", "wayland"], help="force a backend instead of auto-detecting")

    p = sub.add_parser("install", help="register the MCP server with Claude Code and install the skill")
    p.add_argument("--dry-run", action="store_true", help="print what would be written and run, then exit")
    p.add_argument("--always-load", action="store_true",
                   help="load the tool schemas in every session (saves a round trip when you use the "
                        "desktop, costs ~2.6k tokens per request when you don't)")
    _policy_flags(p)

    p = sub.add_parser("mcp", help="run the MCP server over stdio")
    _policy_flags(p)
    p.add_argument("--backend", choices=["x11", "wayland"], help="force a backend instead of auto-detecting")
    p.add_argument("--audit-log", metavar="PATH", help="audit log path (default ~/.cache/ccdesk/audit.log)")

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
        return install(flags, dry_run=args.dry_run, always_load=args.always_load)

    if args.command == "mcp":
        from ccdesk.backends import get_backend
        from ccdesk.desktop import Desktop
        from ccdesk.parsing import parse_confirm_keys
        from ccdesk.safety import AuditLog, Policy

        try:
            from ccdesk.server import serve
        except ImportError as exc:
            print(f"ccdesk mcp needs the 'mcp' package: {exc}", file=sys.stderr)
            return 1
        desktop = Desktop(
            backend=get_backend(args.backend),
            policy=Policy(read_only=args.read_only, allow_launch=args.allow_launch,
                          confirm_keys=parse_confirm_keys(args.confirm_keys)),
            audit=AuditLog(args.audit_log),
        )
        desktop.audit.record("server_start", {"read_only": args.read_only, "allow_launch": args.allow_launch,
                                              "confirm_keys": args.confirm_keys, "backend": desktop.backend.name},
                             "ok")
        serve(desktop)
        return 0

    parser.print_help()
    return 2
