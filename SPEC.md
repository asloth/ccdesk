# ccdesk — Desktop control for Claude Code on Linux

## Problem

Claude Code can edit files and run shell commands, but it cannot see or drive the
graphical desktop. On Linux there is no Claude Desktop app, so "Claude Cowork"-style
computer use is unavailable. Sara wants Claude Code to open Firefox, read what is on
screen, click, type and verify, without burning tokens on hand-written automation.

## Goal

A small, dependency-light tool that gives Claude Code eyes and hands on a Linux
desktop through MCP, plus a skill that teaches the correct workflow.

## Hard requirements

1. **Linux first.** Must work on X11 and degrade gracefully on Wayland.
2. **Zero Python dependencies for the core.** Use system CLI tools:
   `scrot` or `import` (screenshots), `xdotool` (input, windows),
   `wmctrl` (window list/activate), `xclip` (clipboard).
   Wayland: `grim`, `ydotool`/`wtype`, `wl-copy`. Detect at runtime.
3. **MCP server over stdio**, started with `ccdesk mcp`. Python, standard library plus
   the official `mcp` package only (declared in pyproject).
4. **Tools exposed** (exact names):
   - `screenshot(region?, path?)` -> saved PNG path + width/height
   - `click(x, y, button="left", count=1)`
   - `move(x, y)`
   - `drag(from_x, from_y, to_x, to_y, duration_ms=400)`
   - `scroll(x, y, dx, dy)`
   - `type_text(text)`
   - `press_key(key, modifiers=[])`
   - `list_windows()` -> id, title, class, geometry
   - `activate_window(title_or_id)`
   - `clipboard(action="get"|"set", text?)`
   - `wait(seconds)`
   - `doctor()` -> environment report: session type, tools found, screen size, scale
5. **Safety.** `launch(command)` is disabled unless the server was started with
   `--allow-launch`. Destructive key combos (`Alt+F4`, `Ctrl+Q`, `Ctrl+W`, `Super+Q`)
   require `confirm=true`. Every action is appended to a JSONL audit log at
   `~/.cache/ccdesk/audit.log`. `--read-only` disables all input tools.
6. **CLI**: `ccdesk doctor`, `ccdesk install` (registers the MCP server with Claude Code
   at user scope and copies the skill to `~/.claude/skills/computer-use/`),
   `ccdesk mcp`, `ccdesk version`.
7. **Skill** at `skills/computer-use/SKILL.md`: screenshot-first loop, one action then
   verify, coordinate rules, Firefox recipe, what not to do (never type passwords,
   never dismiss dialogs blindly).
8. **Tests.** Pure unit tests for parsing/validation, plus an integration test that
   runs under `xvfb-run`: spawn a Tk window with a button and an entry, screenshot it,
   click the button by computed coordinates, assert the label changed, type into the
   entry and assert the content. Tests must skip cleanly when no display is present.
9. **Docs.** README with install (`pipx install .`), Claude Code wiring, a real demo
   transcript, security model, Wayland caveats, and a support matrix.
10. **License:** MIT.

## Non-goals

- No cloud sandboxes, no VMs, no OCR engine, no LLM calls inside the tool.
- Not a replacement for Cua Driver; if `cua-driver` is on PATH, `doctor` should detect it
  and say so, but ccdesk must not depend on it.

## Acceptance

- `xvfb-run -a pytest -q` passes.
- `xvfb-run -a ccdesk doctor` prints a sane report.
- `ccdesk install --dry-run` prints exactly what it would write.
- No Python file imports a third-party module outside `mcp` in the server path.
