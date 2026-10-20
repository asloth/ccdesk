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
            lines.append("[2] `claude` is not on PATH; register the server yourself with:")
            lines.append(f"    {shlex.join(self.commands[-1])}")
        return "\n".join(lines)


def make_plan(flags: list[str], always_load: bool = False) -> Plan:
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
        ],
    )


def install(flags: list[str], dry_run: bool = False, out=print, always_load: bool = False) -> int:
    plan = make_plan(flags, always_load)
    out(plan.describe(dry_run))
    if dry_run:
        return 0
    plan.skill_dst.parent.mkdir(parents=True, exist_ok=True)
    plan.skill_dst.write_bytes(plan.skill_bytes)
    out(f"\nwrote {plan.skill_dst}")
    if not plan.claude:
        out("skill installed; MCP server NOT registered because the claude CLI was not found")
        return 1
    remove, add = plan.commands
    subprocess.run(remove, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc = subprocess.run(add, capture_output=True, text=True)
    if proc.returncode != 0:
        out(f"claude mcp add failed: {(proc.stderr or proc.stdout).strip()}")
        return proc.returncode
    out(f"registered MCP server '{SERVER_NAME}' at user scope; restart Claude Code and run /mcp to check")
    return 0
