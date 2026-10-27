"""Your own Firefox, driven by Mozilla's firefox-devtools MCP server over WebDriver BiDi.

ccdesk does not speak BiDi itself. `ccdesk install --firefox` registers Mozilla's server in
connect-existing mode, and `ccdesk firefox` starts Firefox with the two flags it attaches to.
While those flags are on, sites can see `navigator.webdriver` and some block the browser as a
bot, so `ccdesk firefox --off` and doctor() exist to make turning them off again easy.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

SERVER_NAME = "firefox-devtools"
PACKAGE = "@mozilla/firefox-devtools-mcp@0.10.6"  # pinned: npx -y would run whatever is newest
AUTOMATION_FLAGS = ("--marionette", "--remote-debugging-port")
MARIONETTE_PORT = 2828

AUTOMATION_WARNING = (
    "While Firefox runs with --marionette, sites can see navigator.webdriver=true and some "
    "(Cloudflare, Akamai) block it as a bot. Turn it on for a task, then `ccdesk firefox --off`."
)


def server_command() -> list[str]:
    return ["npx", "-y", PACKAGE, "--connect-existing", "--marionette-port", str(MARIONETTE_PORT),
            "--tool-preset", "slim"]


def _is_firefox(argv: list[str]) -> bool:
    if not argv or "-contentproc" in argv:  # the tabs' child processes, not the browser
        return False
    return os.path.basename(argv[0]) in ("firefox", "firefox-bin", "firefox-esr")


def processes(proc: Path = Path("/proc"), uid: int | None = None) -> list[dict]:
    """The user's running Firefox browsers: pid, and whether each has the automation flags."""
    uid = os.getuid() if uid is None else uid
    found = []
    for entry in proc.iterdir() if proc.is_dir() else []:
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            argv = [a for a in (entry / "cmdline").read_bytes().decode(errors="replace").split("\0") if a]
        except OSError:  # the process exited while we looked
            continue
        if _is_firefox(argv):
            on = all(any(a == f or a.startswith(f + "=") for a in argv) for f in AUTOMATION_FLAGS)
            found.append({"pid": int(entry.name), "automation": on})
    return sorted(found, key=lambda p: p["pid"])


def status(proc: Path = Path("/proc")) -> dict:
    running = processes(proc)
    return {"running": bool(running), "automation": any(p["automation"] for p in running),
            "pids": [p["pid"] for p in running]}


def start(extra: list[str], automation: bool = True, proc: Path = Path("/proc"), popen=subprocess.Popen,
          out=print) -> int:
    """Start Firefox with (or without) the automation flags. Never quits a running Firefox:
    a second `firefox` would only open a window in it, without the flags."""
    state = status(proc)
    if state["running"]:
        if state["automation"] == automation:
            out(f"Firefox is already running with automation {'on' if automation else 'off'}")
            return 0
        out(f"Firefox is running with automation {'on' if state['automation'] else 'off'}. Quit it "
            f"(Ctrl+Q; with session restore on, your tabs come back), then run this again.")
        return 1
    exe = shutil.which("firefox")
    if not exe:
        out("firefox is not on PATH")
        return 1
    argv = [exe, *(AUTOMATION_FLAGS if automation else ()), *extra]
    popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
          start_new_session=True)  # outlives this command
    if automation:
        out(f"started Firefox with automation on. {AUTOMATION_WARNING}")
    else:
        out("started Firefox with automation off")
    return 0
