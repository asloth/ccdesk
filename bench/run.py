"""Token benchmark: the same desktop task with and without ccdesk, measured from Claude Code's own
usage numbers (not estimates). Each run gets a fresh form on a private Xvfb display.

    python3 bench/run.py --arms none,shots,tree --reps 3 --out bench/results

Arms:
  none   no ccdesk. Bash + Read only; xdotool/scrot/ImageMagick are on PATH.
  shots  ccdesk without ui_tree/click_element: screenshots and coordinates.
  tree   ccdesk with every tool.
All arms: same prompt, same model, no user settings/plugins/hooks, Skill tool off.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / "form_app.py"
SYS_PY = os.environ.get("CCDESK_A11Y_PYTHON", "/usr/bin/python3")
CCDESK = shutil.which("ccdesk") or "ccdesk"
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
    "none": {"mcp": False, "deny": []},
    "shots": {"mcp": True, "deny": ["mcp__ccdesk__ui_tree", "mcp__ccdesk__click_element"]},
    "tree": {"mcp": True, "deny": []},
}


def start_xvfb(display):
    # A second benchmark (or a leftover form) on the same display would corrupt every result.
    if subprocess.run(["xdpyinfo", "-display", display], capture_output=True).returncode == 0:
        sys.exit(f"display {display} is already in use; stop whatever runs there first")
    proc = subprocess.Popen(["Xvfb", display, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if subprocess.run(["xdpyinfo", "-display", display], capture_output=True).returncode == 0:
            return proc
        time.sleep(0.1)
    sys.exit("Xvfb did not start")


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
    if ARMS[arm]["mcp"]:
        cfg["mcpServers"]["ccdesk"] = {"type": "stdio", "command": CCDESK,
                                       "args": ["mcp", "--audit-log", str(workdir / "audit.log")]}
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
    ap.add_argument("--display", default=":77")
    ap.add_argument("--out", default=str(HERE / "results"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, DISPLAY=args.display)
    env.pop("WAYLAND_DISPLAY", None)
    xvfb = start_xvfb(args.display)
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
        xvfb.kill()


if __name__ == "__main__":
    main()
