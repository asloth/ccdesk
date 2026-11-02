"""Firefox for Claude, driven by Mozilla's firefox-devtools MCP server over WebDriver BiDi.

ccdesk does not speak BiDi itself; `ccdesk install --firefox` registers Mozilla's server.

- "agent" (the default): the server starts Claude's own Firefox on first use, with a profile
  kept in profile_dir(). The user signs in there once. Their own Firefox is never touched.
- "mine": the server attaches to the user's Firefox, which `ccdesk firefox` starts with the
  two flags it needs. While they are on, sites can see `navigator.webdriver` and some block the
  browser as a bot, so `ccdesk firefox --off` and doctor() make turning them off again easy.
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
REMOTE_PORT = "9222"  # stated: Firefox takes the argument after this flag as its value

AUTOMATION_WARNING = (
    "While Firefox runs with --marionette, sites can see navigator.webdriver=true and some "
    "(Cloudflare, Akamai) block it as a bot. Turn it on for a task, then `ccdesk firefox --off`."
)


def profile_dir() -> Path:
    """Where Claude's own Firefox keeps its profile, logins included: data, not cache."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "ccdesk" / "firefox"


# Mozilla's server runs Firefox in this subfolder of --profile-path, and reuses one that exists.
PROFILE_SUBDIR = "firefox_devtools_mcp_profile"
MARK = "/* ccdesk: this window is Claude's. Remove this line and ccdesk leaves the file alone. */"
USER_CHROME = MARK + """
#navigator-toolbox { border-top: 6px solid #d97757 !important; }
.tabbrowser-tab[selected] .tab-background { background: #d97757 !important; }
.tabbrowser-tab[selected] .tab-label { color: #fff !important; }
"""
STYLESHEETS_PREF = 'user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);'


def prepare_profile() -> Path:
    """Make Claude's Firefox easy to tell apart: an orange strip on top and orange active tab.
    Leaves a userChrome.css alone once the user has edited it (it no longer starts with MARK)."""
    profile = profile_dir() / PROFILE_SUBDIR
    (profile / "chrome").mkdir(mode=0o700, parents=True, exist_ok=True)
    for private in (profile_dir(), profile):  # mkdir applies mode only to the last folder, and these hold logins
        os.chmod(private, 0o700)
    css = profile / "chrome" / "userChrome.css"
    if not css.exists() or css.read_text(errors="replace").startswith(MARK):
        css.write_text(USER_CHROME)
    user_js = profile / "user.js"
    prefs = user_js.read_text(errors="replace") if user_js.exists() else ""
    if STYLESHEETS_PREF not in prefs:
        user_js.write_text(prefs + ("" if not prefs or prefs.endswith("\n") else "\n") + STYLESHEETS_PREF + "\n")
    return profile


def server_command(mode: str = "agent") -> list[str]:
    if mode == "mine":
        target = ["--connect-existing", "--marionette-port", str(MARIONETTE_PORT)]
    else:
        target = ["--profile-path", str(profile_dir())]
    return ["npx", "-y", PACKAGE, *target, "--tool-preset", "slim"]


def _is_firefox(argv: list[str]) -> bool:
    if not argv or "-contentproc" in argv:  # the tabs' child processes, not the browser
        return False
    return os.path.basename(argv[0]) in ("firefox", "firefox-bin", "firefox-esr")


def processes(proc: Path = Path("/proc"), uid: int | None = None) -> list[dict]:
    """The user's running Firefox browsers: pid, whether each has the automation flags, and
    whether it is Claude's own (its profile is under profile_dir())."""
    own = str(profile_dir())
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
            # Either flag opens a control port, so doctor warns about it. Mozilla's server needs both.
            flags = [any(a == f or a.startswith(f + "=") for a in argv) for f in AUTOMATION_FLAGS]
            agent = any(a == own or a.startswith(own + os.sep) for a in argv)
            found.append({"pid": int(entry.name), "automation": any(flags), "ready": all(flags), "agent": agent})
    return sorted(found, key=lambda p: p["pid"])


def status(proc: Path = Path("/proc")) -> dict:
    """The user's own Firefox (running, automation flags on), and whether Claude's is running."""
    found = processes(proc)
    running = [p for p in found if not p["agent"]]
    return {"running": bool(running), "automation": any(p["automation"] for p in running),
            "ready": any(p["ready"] for p in running),
            "pids": [p["pid"] for p in running], "agent_running": any(p["agent"] for p in found)}


def start(extra: list[str], automation: bool = True, proc: Path = Path("/proc"), popen=subprocess.Popen,
          out=print) -> int:
    """Start Firefox with (or without) the automation flags. Never quits a running Firefox:
    a second `firefox` would only open a window in it, without the flags."""
    state = status(proc)
    if state["running"]:
        if state["ready"] if automation else not state["automation"]:
            out(f"Firefox is already running with automation {'on' if automation else 'off'}")
            return 0
        now = "on" if state["ready"] else "partly on" if state["automation"] else "off"
        out(f"Firefox is running with automation {now}. Quit it "
            f"(Ctrl+Q; with session restore on, your tabs come back), then run this again.")
        return 1
    exe = shutil.which("firefox")
    if not exe:
        out("firefox is not on PATH")
        return 1
    argv = [exe, *((*AUTOMATION_FLAGS, REMOTE_PORT) if automation else ()), *extra]
    popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
          start_new_session=True)  # outlives this command
    if automation:
        out(f"started Firefox with automation on. {AUTOMATION_WARNING}")
    else:
        out("started Firefox with automation off")
    return 0
