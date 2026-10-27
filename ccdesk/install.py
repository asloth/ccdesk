"""`ccdesk install`: register the MCP server with Claude Code and install the skill."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from ccdesk.firefox import AUTOMATION_WARNING
from ccdesk.firefox import profile_dir as firefox_profile
from ccdesk.firefox import SERVER_NAME as FIREFOX_NAME
from ccdesk.firefox import server_command as firefox_command

SERVER_NAME = "ccdesk"
SKILL_NAME = "computer-use"


def claude_config_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override).expanduser() if override else Path.home() / ".claude"


def skill_source() -> Path:
    """The packaged SKILL.md (wheel install) or the repository copy (source checkout)."""
    packaged = resources.files("ccdesk") / "skill" / "SKILL.md"
    if packaged.is_file():
        return Path(str(packaged))
    repo = Path(__file__).resolve().parent.parent / "skills" / SKILL_NAME / "SKILL.md"
    if repo.is_file():
        return repo
    raise FileNotFoundError("SKILL.md not found in the package or the source tree")


def server_command(flags: list[str]) -> list[str]:
    exe = shutil.which("ccdesk")
    base = [exe] if exe else [sys.executable, "-m", "ccdesk"]
    return [*base, "mcp", *flags]


@dataclass
class Plan:
    skill_src: Path
    skill_dst: Path
    skill_bytes: bytes
    commands: list[list[str]] = field(default_factory=list)
    claude: str | None = None

    def describe(self, dry_run: bool) -> str:
        digest = hashlib.sha256(self.skill_bytes).hexdigest()[:16]
        verb = "would" if dry_run else "will"
        lines = [f"ccdesk install{' --dry-run (nothing is written)' if dry_run else ''}", ""]
        lines.append(f"[1] {verb} write {self.skill_dst}")
        lines.append(f"    {len(self.skill_bytes)} bytes, sha256 {digest}…, copied from {self.skill_src}")
        if self.claude:
            for i, argv in enumerate(self.commands, start=2):
                note = "   # ignored if not registered yet" if "remove" in argv else ""
                lines.append(f"[{i}] {verb} run: {shlex.join(argv)}{note}")
        else:
            lines.append("[2] `claude` is not on PATH; register the server(s) yourself with:")
            lines += [f"    {shlex.join(argv)}" for argv in self.commands if "remove" not in argv]
        firefox = next((argv for argv in self.commands if FIREFOX_NAME in argv and "add" in argv), None)
        if firefox and "--connect-existing" in firefox:
            lines += ["", f"firefox-devtools attaches to a Firefox started with `ccdesk firefox`. {AUTOMATION_WARNING}"]
        elif firefox:
            lines += ["", f"firefox-devtools starts Claude's own Firefox on first use, with its profile in "
                          f"{firefox_profile()}. Sign in there to the sites Claude should use; your own "
                          f"Firefox is not touched."]
        return "\n".join(lines)


def make_plan(flags: list[str], always_load: bool = False, firefox: str | None = None) -> Plan:
    src = skill_source()
    claude = shutil.which("claude")
    cmd = server_command(flags)
    add = ["claude", "mcp", "add", "--scope", "user", SERVER_NAME, "--", *cmd]
    if always_load:
        # Tool schemas go in every session's prompt up front: no tool-search round trip on first use.
        config = {"type": "stdio", "command": cmd[0], "args": cmd[1:], "alwaysLoad": True}
        add = ["claude", "mcp", "add-json", "--scope", "user", SERVER_NAME, json.dumps(config)]
    return Plan(
        skill_src=src,
        skill_dst=claude_config_dir() / "skills" / SKILL_NAME / "SKILL.md",
        skill_bytes=src.read_bytes(),
        claude=claude,
        commands=[
            ["claude", "mcp", "remove", "--scope", "user", SERVER_NAME],
            add,
            *([["claude", "mcp", "remove", "--scope", "user", FIREFOX_NAME],
               ["claude", "mcp", "add", "--scope", "user", FIREFOX_NAME, "--", *firefox_command(firefox)]]
              if firefox else []),
        ],
    )


def install(flags: list[str], dry_run: bool = False, out=print, always_load: bool = False,
            firefox: str | None = None) -> int:
    plan = make_plan(flags, always_load, firefox)
    out(plan.describe(dry_run))
    if dry_run:
        return 0
    plan.skill_dst.parent.mkdir(parents=True, exist_ok=True)
    plan.skill_dst.write_bytes(plan.skill_bytes)
    out(f"\nwrote {plan.skill_dst}")
    if firefox == "agent":
        firefox_profile().mkdir(mode=0o700, parents=True, exist_ok=True)  # it will hold logins
    if not plan.claude:
        out("skill installed; MCP server NOT registered because the claude CLI was not found")
        return 1
    for remove, add in zip(plan.commands[::2], plan.commands[1::2]):
        subprocess.run(remove, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc = subprocess.run(add, capture_output=True, text=True)
        if proc.returncode != 0:
            out(f"claude mcp add failed: {(proc.stderr or proc.stdout).strip()}")
            return proc.returncode
        out(f"registered MCP server '{remove[-1]}' at user scope")
    out("restart Claude Code and run /mcp to check")
    return 0
