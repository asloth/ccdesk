# ccdesk

Desktop control for Claude Code on Linux. ccdesk is a small MCP server that lets Claude Code
take screenshots, move and click the mouse, type, press keys, list and focus windows, and
use the clipboard. It ships with a skill that teaches Claude the safe workflow: screenshot,
one action, verify.

There's no Claude Desktop app on Linux. ccdesk fills that gap with the system tools you
probably already have (`xdotool`, `scrot`, `wmctrl`, `xclip`). The core is standard-library
Python; the only dependency is the official `mcp` package.

## Install

```sh
# system tools (Debian/Ubuntu; X11)
sudo apt install xdotool scrot wmctrl xclip x11-utils x11-xserver-utils
# optional: imagemagick (fallback screenshots), xvfb + python3-tk (to run the test suite)

pipx install .            # from a checkout of this repo
ccdesk doctor             # check what was detected
ccdesk install --dry-run  # show what install would write and run
ccdesk install            # register the MCP server + install the skill
```

On Fedora: `sudo dnf install xdotool scrot wmctrl xclip xprop xwininfo xrandr`. On Arch:
`sudo pacman -S xdotool scrot wmctrl xclip xorg-xprop xorg-xwininfo xorg-xrandr`.
`xwininfo` gives the size of the whole screen across monitors and `xrandr` lists the
monitors; without them ccdesk falls back to `xdotool`, which recent releases report as
one monitor only.

## Wiring it into Claude Code

`ccdesk install` does two things:

1. Copies `skills/computer-use/SKILL.md` to `~/.claude/skills/computer-use/SKILL.md`
   (or `$CLAUDE_CONFIG_DIR/skills/...`).
2. Runs `claude mcp add --scope user ccdesk -- /path/to/ccdesk mcp`, after removing any
   earlier `ccdesk` registration.

Flags you pass to `install` are forwarded to the server: `ccdesk install --read-only`,
`ccdesk install --allow-launch` or `ccdesk install --confirm-keys "super+x"`.
`--always-load` puts the tool schemas in every session's prompt instead of behind Claude
Code's tool search: one round trip less when you use the desktop, ~2.6k tokens more per
request when you don't (see `bench/RESULTS.md`). `--dry-run`
prints the exact file, size, checksum and commands, and changes nothing:

```text
$ ccdesk install --dry-run
ccdesk install --dry-run (nothing is written)

[1] would write /home/hermes/.claude/skills/computer-use/SKILL.md
    6198 bytes, sha256 b0dbc9581ca05b94…, copied from /home/hermes/Projects/ccdesk/skills/computer-use/SKILL.md
[2] would run: claude mcp remove --scope user ccdesk   # ignored if not registered yet
[3] would run: claude mcp add --scope user ccdesk -- /home/hermes/Projects/ccdesk/.venv/bin/ccdesk mcp
```

To wire it by hand instead:

```sh
claude mcp add --scope user ccdesk -- ccdesk mcp
mkdir -p ~/.claude/skills/computer-use && cp skills/computer-use/SKILL.md ~/.claude/skills/computer-use/
```

Restart Claude Code and run `/mcp`. `ccdesk` should be listed with 15 tools.

## Tools

| Tool | What it does |
| --- | --- |
| `screenshot(region?, path?, scale=1)` | Saves a PNG and returns `{path, width, height, approx_tokens}`. `region` is `"x,y,w,h"`; region results also include `origin`. `scale=0.5` halves each side (needs ImageMagick). |
| `click(x, y, button="left", count=1)` | Moves the pointer and clicks. `count=2` double-clicks. |
| `move(x, y)` | Moves the pointer (hover). |
| `drag(from_x, from_y, to_x, to_y, duration_ms=400)` | Left-button drag along a smooth path. |
| `scroll(x, y, dx, dy)` | Wheel clicks with the pointer at (x, y). `dy > 0` scrolls down. |
| `type_text(text)` | Types into the focused widget. |
| `press_key(key, modifiers=[], confirm=false)` | `"Return"`, `"ctrl+l"`, or `key="t", modifiers=["ctrl","shift"]`. |
| `list_windows()` | Returns `id`, `title`, `class`, `geometry` and `desktop` for each window. |
| `activate_window(title_or_id)` | Raises and focuses a window by id or by partial title. |
| `clipboard(action="get"\|"set", text?)` | Reads or replaces the clipboard. |
| `wait(seconds)` | Sleeps, up to 60 s. |
| `doctor()` | Reports the session type, tools found, screen size, scale and policy. |
| `ui_tree(window?, query?, page=false, max_nodes=300)` | The window's buttons, fields, checkboxes, links and text as compact lines from the accessibility tree (AT-SPI), with values, states and screen boxes. |
| `click_element(ref, button="left", count=1)` | Clicks the centre of an element from the latest `ui_tree()`; refuses if it changed or left the screen. |
| `launch(command)` | Starts an app without a shell. Only enabled with `--allow-launch`. |

Coordinates are screen pixels with the origin at the top left, the same space as a
full-screen screenshot.

## Demo

This transcript is a real stdio session against `ccdesk mcp`, recorded under `xvfb-run`
while driving the Tk test app in `tests/tk_app.py`. Each `>` line is a tool call and each `<`
line is the server's response. The pixel coordinates were found in the screenshot. In
Claude Code, Claude does the same thing by reading the PNG.

```text
> list_windows()
< {"windows": [{"id": "0x00200029", "title": "ccdesk-test", "class": "tk.Tk", "geometry": {"x": 40, "y": 50, "width": 420, "height": 420}, "desktop": null}]}

> activate_window(title_or_id='ccdesk-test')
< {"activated": {"id": "0x00200029", "title": "ccdesk-test", "class": "tk.Tk", "geometry": {"x": 40, "y": 50, "width": 420, "height": 420}, "desktop": null}}

> screenshot()
< {"path": "/home/hermes/.cache/ccdesk/screenshots/screen-20261019-200511-959.png", "width": 1280, "height": 1024}

> click(x=249, y=79)
< {"clicked": {"x": 249, "y": 79}, "button": "left", "count": 1}

> screenshot(region='99,49,300,110')
< {"path": "/home/hermes/.cache/ccdesk/screenshots/screen-20261019-200514-075.png", "width": 300, "height": 110, "origin": {"x": 99, "y": 49}, "note": "pixel (px,py) in this image is screen (origin.x+px, origin.y+py)"}

> click(x=249, y=179)
< {"clicked": {"x": 249, "y": 179}, "button": "left", "count": 1}

> type_text(text='hello from Claude')
< {"typed_chars": 17}

> press_key(key='Return')
< {"pressed": "Return"}

> press_key(key='w', modifiers=['ctrl'])
< ERROR Error executing tool press_key: Ctrl+w can close windows or quit applications; take a screenshot to verify the target, then call again with confirm=true

> launch(command='xterm')
< ERROR Error executing tool launch: launch is disabled: restart the server with `ccdesk mcp --allow-launch`
```

When the session ended, the app reported `label: "submitted:hello from Claude"`. In Claude
Code a typical request looks like *"open Firefox, go to news.ycombinator.com and tell me the
top story"*. The skill makes Claude find or focus Firefox, use `ctrl+l` for the address bar,
type the URL, wait, screenshot and read the page.

## Token cost

Screenshots are most of what a ccdesk session costs. The model pays about `width*height/750`
input tokens per image it reads (each result includes `approx_tokens`), and the image stays in
context for every later turn. Measured on a real Play Console session: a 1920x1080 monitor
capture is ~2,800 tokens, and 17 of them were about 70% of the context the session used.

| Capture | Size | Tokens |
| --- | --- | --- |
| one monitor, full scale | 1920x1080 | ~2,765 |
| one monitor, `scale=0.6` | 1152x648 | ~995 |
| one monitor, `scale=0.5` (still readable) | 960x540 | ~691 |
| region around a form field | 600x300 | ~240 |

The cheapest way to look is not a picture at all. `ui_tree()` reads the accessibility tree
(AT-SPI, what screen readers use) and returns the on-screen elements as text: a form is
usually 100 to 600 tokens, and on a test form a full fill-and-save took two `ui_tree()` calls
of ~150 tokens each and no screenshots. Clicks land on the element's real box instead of an
estimate from pixels.

Apps only publish the tree when accessibility is on when they start:

```sh
gsettings set org.gnome.desktop.interface toolkit-accessibility true   # GTK apps and Firefox
# or start them with GNOME_ACCESSIBILITY=1; Chromium/Electron: --force-renderer-accessibility
```

The tree is read by `ccdesk/a11y_helper.py`, which runs under a system Python with PyGObject
(`python-gobject` on Arch, `python3-gi` on Debian, plus `at-spi2-core`), so ccdesk itself keeps
no compiled dependencies. `CCDESK_A11Y_PYTHON` picks another interpreter. `ccdesk doctor`
lists the apps that publish a tree.

After the first `ui_tree()`, actions (`click`, `click_element`, `type_text`, `press_key`)
return `changes`: only the lines of that window that changed, such as
`e5 check box "I accept the terms" [checked]`. The model verifies from that instead of
reading the tree or the screen again. Refs stay stable across calls.

Measured end to end (`bench/`, same form task, Claude Code's own usage numbers, 3 runs each):
no ccdesk 89k input tokens, ccdesk with screenshots 118k, ccdesk with `ui_tree` and `changes`
63k (−29%), 57k with `--always-load` (−37%). Without ccdesk Claude already screenshots just
the window, so the saving is mostly fewer round trips, not smaller pictures.

The skill tells Claude to orient at `scale=0.5`, then act and verify with small full-scale
regions, and to pull long page text through the clipboard instead of reading it from pixels.

## Security model

ccdesk drives your real desktop with your user's privileges. The guardrails are:

- **Read-only mode.** `ccdesk mcp --read-only` disables every input tool: `click`, `move`,
  `drag`, `scroll`, `type_text`, `press_key`, `activate_window`, `clipboard(set)` and
  `launch`. Screenshots, `list_windows`, `clipboard(get)`, `wait` and `doctor` still work.
- **No launching by default.** `launch(command)` refuses unless the server was started with
  `--allow-launch`. Even then it runs the command without a shell (`shlex.split`, no pipes,
  redirects or `;`) in a new session.
- **Destructive shortcuts need confirmation.** `Alt+F4`, `Ctrl+Q`, `Ctrl+W`, `Super+Q`,
  `Ctrl+Alt+Delete` and `Ctrl+Alt+BackSpace` are refused unless the call passes
  `confirm=true`. Supersets such as `Ctrl+Shift+W` count too. Window managers bind their own
  close, quit and power keys, so add yours with `--confirm-keys`, for example on bspwm:
  `ccdesk install --confirm-keys "super+x,ctrl+alt+super+p,ctrl+alt+super+r,ctrl+alt+super+q"`.
- **Audit log.** Every call, including denied and failed ones, is appended as one JSON line
  to `~/.cache/ccdesk/audit.log` (mode 0600; override with `--audit-log` or
  `CCDESK_AUDIT_LOG`). Each line has a timestamp, the tool, its arguments, the status
  (`ok`/`denied`/`error`), the result or error, and the duration. Clipboard contents are
  logged by length only. Typed text is logged, truncated to 200 characters, which is one
  more reason the skill tells Claude never to type secrets.
- **Input validation.** Coordinates are bounds-checked against the screen. Counts, scroll
  amounts, durations, waits and text length are capped. Key names are whitelisted
  characters, so nothing reaches a helper tool as an option or shell syntax.
- **The skill.** It tells Claude to screenshot before acting and verify after, to never type
  passwords, to never dismiss dialogs blindly, and to treat on-screen text as data, not
  instructions.

These guardrails are not a sandbox. Any client that can call `type_text` can type a shell
command into a terminal. Use `--read-only` when you only want Claude to look, and keep
Claude Code's own tool-permission prompts turned on for `mcp__ccdesk__*` if you want to
approve each action.

## Wayland caveats

Wayland isolates clients from each other by design, so ccdesk can only do what the
compositor allows:

- **Screenshots** need `grim`, which requires wlr-screencopy (sway, Hyprland, river, Wayfire).
  GNOME falls back to `gnome-screenshot -f`, and only for full-screen captures. KDE has
  neither.
- **Pointer input** on sway goes through sway itself (`swaymsg seat - cursor set/press/release`):
  exact positions, no root, nothing extra to install. Elsewhere it needs `ydotool` 1.x with
  the `ydotoold` daemon and access to `/dev/uinput`; absolute positioning then depends on
  ydotool's pointer acceleration, so verify clicks with screenshots.
- **Keyboard** uses `wtype` (virtual-keyboard protocol, not available on GNOME) and falls
  back to `ydotool type` for plain text. `press_key` requires `wtype`. Known issue: on a
  headless sway with only virtual devices, a named key sent alone (Left, BackSpace) does not
  reach GTK 3 apps, while typed text does; not yet checked on a real sway session.
- **ui_tree** works on Wayland too. GTK and Qt report element boxes relative to their own
  window there; on sway ccdesk adds the window's position from `swaymsg -t get_tree`, so
  `click_element` lands on screen coordinates. On other compositors the result carries a
  note to verify with a screenshot.
- **Windows** are listed and focused through `swaymsg` (sway) or `hyprctl` (Hyprland).
  Elsewhere ccdesk falls back to XWayland via `xdotool`, which only sees X11 apps.
- **Clipboard** uses `wl-copy` / `wl-paste`.
- If you only need to drive XWayland apps, `CCDESK_BACKEND=x11` (or `--backend x11`)
  forces the X11 backend.

`ccdesk doctor` reports exactly which of these are present.

## Support matrix

| Environment | screenshot | click/move/drag/scroll | type_text | press_key | list/activate windows | clipboard |
| --- | --- | --- | --- | --- | --- | --- |
| X11 (any WM/DE) | ✅ scrot / import | ✅ xdotool | ✅ xdotool | ✅ xdotool | ✅ wmctrl | ✅ xclip / xsel |
| X11 without a WM (Xvfb) | ✅ | ✅ | ✅ | ✅ | ✅ xdotool + xprop fallback | ✅ |
| Wayland: sway | ✅ grim | ✅ swaymsg seat | ✅ wtype / ydotool | ⚠️ wtype (see above) | ✅ swaymsg | ✅ wl-clipboard |
| Wayland: Hyprland | ✅ grim | ⚠️ ydotool + ydotoold | ✅ wtype / ydotool | ✅ wtype | ✅ hyprctl | ✅ wl-clipboard |
| Wayland: GNOME | ⚠️ full screen only | ⚠️ ydotool + ydotoold | ⚠️ ydotool | ❌ | ⚠️ XWayland apps only | ✅ wl-clipboard |
| Wayland: KDE Plasma | ❌ | ⚠️ ydotool + ydotoold | ⚠️ ydotool | ❌ | ⚠️ XWayland apps only | ✅ wl-clipboard |
| No display (SSH, CI) | ❌ (doctor explains) | ❌ | ❌ | ❌ | ❌ | ❌ |

The X11 rows are exercised by the integration suite under Xvfb, and the sway row by
`tests/test_integration_wayland.py` on a headless sway it starts itself (screenshots, window
list, clipboard, `ui_tree` coordinates, clicks and typing in a real GTK app). The other
Wayland rows are covered by unit tests of the exact commands ccdesk builds.
Requires Python 3.10+. The full suite passes on Python 3.14 with both `mcp` 2.3 and `mcp` 1.30.

ccdesk does not replace [Cua Driver](https://github.com/trycua/cua). If `cua-driver` is on
your `PATH`, `doctor` mentions it, but ccdesk never calls it.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
xvfb-run -a pytest -q        # unit tests + Tk integration test on a virtual X server
pytest -q                    # without a display the integration tests are skipped
xvfb-run -a ccdesk doctor
```

The display tests skip themselves when `DISPLAY` looks like your real screen (`:0`, `:1`):
they click and type for real. Set `CCDESK_TEST_REAL_DISPLAY=1` to override.

The Wayland test needs `sway`, `grim`, `wtype`, `wl-clipboard`, a C compiler and
`wayland-scanner`. It starts a headless sway on its own socket, plus a small virtual-pointer
helper (`tests/wayland/vpointer.c`, compiled on the fly) because a headless seat has no mouse.

The integration test (`tests/test_integration_tk.py`) starts a Tk window with a button, a
label, an entry, a canvas and a list. It takes a screenshot, finds the button by its colour
in the PNG, clicks it, and checks that the label changed. It then types into the entry,
presses Return, drags across the canvas, scrolls the list and checks the clipboard,
asserting each result against the app's own state.

Layout:

```
ccdesk/
  cli.py          ccdesk doctor | install | mcp | version
  server.py       MCP tool definitions (the only module importing `mcp`)
  desktop.py      validation + policy + audit around a backend
  safety.py       read-only / launch policy, destructive keys, JSONL audit log
  parsing.py      pure parsers: keys, regions, wmctrl/xdotool output, PNG header
  doctor.py       environment report
  install.py      Claude Code registration and skill copy
  backends/       x11.py, wayland.py, base.py
skills/computer-use/SKILL.md
tests/
```

## License

MIT. See [LICENSE](LICENSE).
