"""Token benchmark: the same desktop task with and without ccdesk, measured from Claude Code's own
usage numbers (not estimates). Each run gets a fresh form on a private Xvfb display.

    python3 bench/run.py --arms none,shots,tree --reps 3 --out bench/results

Arms:
  none   no ccdesk. Bash + Read only; xdotool/scrot/ImageMagick are on PATH.
  shots  ccdesk without ui_tree/click_element: screenshots and coordinates.
  tree   ccdesk with every tool.
  loaded ccdesk with every tool and alwaysLoad: schemas up front, no ToolSearch round trip.
  base, base-shots, base-loaded  the same three on another ccdesk, to measure a change. $CCDESK_BASE
         is its command.
  cul, cul-loaded  agent-sh/computer-use-linux instead of ccdesk. $CUL_BIN is its binary. Its overlay,
         uinput pointer and portal screenshots are off, to keep it on the private screen.
All arms: same prompt, same model, no user settings/plugins/hooks, Skill tool off, each run on the same
private screen with a window manager and its own D-Bus, so nothing can reach the real session.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ccdesk.headless import Headless  # noqa: E402

HERE = Path(__file__).resolve().parent
APP = HERE / "form_app.py"
SYS_PY = os.environ.get("CCDESK_A11Y_PYTHON", "/usr/bin/python3")
CCDESK = os.environ.get("CCDESK_BIN") or shutil.which("ccdesk") or "ccdesk"
CUL_ENV = {"COMPUTER_USE_LINUX_INDICATOR": "0", "CU_DISABLE_ABS_POINTER": "1",
           "COMPUTER_USE_LINUX_SCREENSHOT_BACKEND": "x11"}
SHOTS_DENY = ["mcp__ccdesk__ui_tree", "mcp__ccdesk__click_element", "mcp__ccdesk__set_value"]
PROMPT = (
    'A GTK window titled "Signup" is open on the X display in $DISPLAY (a test display, not the '
    'user\'s screen). Fill it in: Full name "Ana Torres", Email "ana@example.org", plan Pro, '
    'untick the newsletter, tick "I accept the terms", then click Submit and check that the window '
    "confirms the account was created. Use whatever tools you have. Reply DONE when it is "
    "confirmed, or FAILED with the reason."
)
EXPECTED = {"Full name": "Ana Torres", "Email": "ana@example.org", "plan": "Pro",
            "newsletter": False, "terms": True}
ARMS = {
    "none": {"deny": []},
    "shots": {"server": "ccdesk", "deny": SHOTS_DENY},
    "tree": {"server": "ccdesk", "deny": []},
    "loaded": {"server": "ccdesk", "deny": [], "always_load": True},
    "base-shots": {"server": "base", "deny": SHOTS_DENY},
    "base": {"server": "base", "deny": []},
    "base-loaded": {"server": "base", "deny": [], "always_load": True},
    "cul": {"server": "cul", "deny": []},
    "cul-loaded": {"server": "cul", "deny": [], "always_load": True},
}


def start_form(env, state):
    state.unlink(missing_ok=True)
    proc = subprocess.Popen([SYS_PY, str(APP), str(state)], env=dict(env, GDK_BACKEND="x11",
                            GNOME_ACCESSIBILITY="1"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if subprocess.run(["xdotool", "search", "--name", "^Signup$"], env=env,
                          capture_output=True).returncode == 0:
            time.sleep(1)
            return proc
        time.sleep(0.1)
    proc.kill()
    sys.exit("form did not appear")


def claude_cmd(arm, workdir, model):
    cfg = {"mcpServers": {}}
    server = ARMS[arm].get("server")
    if server == "cul":
        entry = {"type": "stdio", "command": os.environ["CUL_BIN"], "args": ["mcp"], "env": CUL_ENV}
    elif server:
        command = os.environ["CCDESK_BASE"] if server == "base" else CCDESK
        entry = {"type": "stdio", "command": command, "args": ["mcp", "--audit-log", str(workdir / "audit.log")]}
    if server:
        if ARMS[arm].get("always_load"):
            entry["alwaysLoad"] = True
        cfg["mcpServers"]["computer-use-linux" if server == "cul" else "ccdesk"] = entry
    (workdir / "mcp.json").write_text(json.dumps(cfg))
    return ["claude", "-p", PROMPT, "--model", model, "--output-format", "stream-json", "--verbose",
            "--setting-sources", "", "--strict-mcp-config", "--mcp-config", str(workdir / "mcp.json"),
            "--permission-mode", "bypassPermissions", "--no-session-persistence",
            "--disallowedTools", "Skill", "Agent", "WebFetch", "WebSearch", *ARMS[arm]["deny"]]


def measure(events):
    """Per-request context sizes, from the usage Claude Code reports for each API call."""
    seen, requests, images = set(), [], 0
    for ev in events:
        if ev.get("type") == "user":
            for block in ev.get("message", {}).get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    content = block.get("content")
                    if isinstance(content, list):
                        images += sum(1 for c in content if c.get("type") == "image")
        if ev.get("type") != "assistant":
            continue
        msg = ev["message"]
        if msg["id"] in seen:
            continue
        seen.add(msg["id"])
        u = msg["usage"]
        ctx = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + \
            u.get("cache_read_input_tokens", 0)
        requests.append({"context": ctx})
    result = next((e for e in events if e.get("type") == "result"), {})
    tools = [b["name"] for e in events if e.get("type") == "assistant"
             for b in e["message"]["content"] if b.get("type") == "tool_use"]
    return {
        "requests": len(requests),
        "first_context": requests[0]["context"] if requests else 0,
        "final_context": requests[-1]["context"] if requests else 0,
        "input_processed": sum(r["context"] for r in requests),
        "output": result.get("usage", {}).get("output_tokens", 0),
        "cost_usd": result.get("total_cost_usd"),
        "duration_s": round(result.get("duration_ms", 0) / 1000, 1),
        "images": images,
        "tool_calls": len(tools),
        "tools": tools,
        "answer": (result.get("result") or "")[-300:],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="none,shots,tree")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--model", default="opus")
    ap.add_argument("--out", default=str(HERE / "results"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    screen = Headless(size="1920x1080")
    env = {k: v for k, v in screen.start().items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    try:
        for rep in range(1, args.reps + 1):
            for arm in args.arms.split(","):
                workdir = out / f"{arm}-{rep}"
                workdir.mkdir(exist_ok=True)
                state = workdir / "state.json"
                form = start_form(env, state)
                try:
                    proc = subprocess.run(claude_cmd(arm, workdir, args.model), env=env, cwd=workdir,
                                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900)
                finally:
                    form.kill()
                (workdir / "stream.jsonl").write_text(proc.stdout)
                events = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
                row = {"arm": arm, "rep": rep, **measure(events)}
                try:
                    submitted = json.loads(state.read_text())
                except (OSError, ValueError):
                    submitted = {}
                row["success"] = all(submitted.get(k) == v for k, v in EXPECTED.items())
                (workdir / "row.json").write_text(json.dumps(row, indent=1))
                print(json.dumps({k: v for k, v in row.items() if k not in ("tools", "answer")}), flush=True)
    finally:
        screen.stop()


if __name__ == "__main__":
    main()
