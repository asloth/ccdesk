# Token benchmark, 2026-10-19

Task: fill a GTK signup form (2 text fields, a radio choice, untick one box, tick another,
Submit, confirm). Fresh form on a private 1920x1080 Xvfb display for each run. Claude Code
2.1.304, Opus 5.5, `claude -p`, no user settings, plugins or hooks. Numbers are Claude Code's
own usage report, not estimates. 3 runs per arm, all 9 succeeded.

| | no ccdesk (Bash + Read) | ccdesk, screenshots only | ccdesk with ui_tree |
|---|---|---|---|
| Input tokens processed, median | 89,456 | 117,743 | **64,519** |
| Range | 81,889 to 95,074 | 115,021 to 130,724 | 63,943 to 79,490 |
| API requests | 7 to 8 | 8 to 9 | **5 to 6** |
| Images read | 0 to 3 | 3 | **0** |
| Wall time, median | 30 s | 22 s | **15 s** |
| First request (fixed overhead) | 9,719 | 10,212 | 10,242 |

- ui_tree vs no ccdesk: **28% fewer input tokens**, 2x faster. vs ccdesk screenshots: 45% fewer.
- Most input is the ~10k system prompt re-read on every request, so the saving comes mainly from
  needing fewer requests, not from each look being cheaper.
- Without ccdesk, Claude does not take full-screen shots: it captures only the window
  (`import -window`, ~560 tokens) and batches several xdotool clicks in one Bash call. One run
  wrote its own AT-SPI script. The "2,765 vs 47 tokens" figure compares a full-screen
  screenshot with a filtered tree query; it is one look, not a task.
- ccdesk screenshots-only was the most expensive: a full-screen first look (~2,800 tokens) and
  one tool call per click.
- ccdesk costs ~500 tokens of fixed overhead per request (server instructions, tool names).
- Cost in USD is noisy here because runs share the prompt cache; input tokens are the fair metric.

Not measured: big pages (a browser form like Play Console) and wide multi-monitor screens, where
screenshots grow with the screen and the tree grows with the page. One small app, n=3.

## After: actions report their `changes` (same day)

Once `ui_tree()` has been called, `click_element`/`type_text`/`press_key` return the lines of
the window that changed, so verifying needs no extra `ui_tree()` call.

| | ui_tree, before | ui_tree + changes | + alwaysLoad |
|---|---|---|---|
| Input tokens, median | 64,519 | 63,361 | **56,681** |
| Range | 63,943 to 79,490 | 62,757 to 64,233 | 56,681 (all 3) |
| API requests | 5 to 6 | 5 | **4** |
| ui_tree calls | 3 to 4 | **1** | 1 |
| Fixed overhead per request | 10,242 | 10,312 | 12,908 |

- `changes` removed the verify calls (ui_tree 3-4 → 1) and the variance, but most of those calls
  already ran in parallel with other actions, so tokens dropped only ~2%. Five requests is the
  floor for this flow: find the tools, look, fill, submit, answer.
- The remaining fixed cost is the tool-search round trip (Claude Code defers MCP tools).
  `alwaysLoad` removes it, −11% here, but it adds ~2.6k tokens to **every** request of every
  session, desktop or not. Opt in with `ccdesk install --always-load` only if you mostly drive
  the desktop; the default stays deferred.

Overall vs no ccdesk: 89k → 63k (−29%), or 57k (−37%) with alwaysLoad.

Reproduce: `PATH=~/.local/opt/xvfb/usr/bin:$PATH python3 bench/run.py --reps 3`

## Pictures in the answer, and the old against the new (2026-11-01)

Same form and prompt, Claude Code 2.1.316, Opus 5.5, 5 runs per arm, all succeeded. The setup changed:
every arm now runs on a private screen with a window manager and its own D-Bus, so these numbers
compare with each other, not with the tables above.

| Input tokens, median (range) | before | after | requests |
|---|---|---|---|
| Screenshots only | 135,660 (127,297 to 286,811) | **77,776** (76,800 to 213,784) | 8 → **5** |
| ui_tree, deferred | 70,547 (68,645 to 125,690) | 72,704 (69,522 to 96,976) | 5 → 5 |
| ui_tree, alwaysLoad | 64,489 (64,489 to 66,522) | 63,286 (63,286 to 63,877) | 4 → 4 |

- `screenshot` now shows the picture in its answer. The three Reads, one request each, are gone and
  the pictures cost the same: **43% fewer input tokens** on the screenshot path, and 20 s → 15 s.
- With a tree there is no real change: the differences are inside the noise. `set_value` took tool
  calls from 13 to 11, and 10 to 7 with alwaysLoad, but they already ran in parallel, so requests did
  not drop. "before" for the tree arms comes from an earlier batch the same day.
- Each arm had the odd long run (213,784 is one run of 13 requests), so read the medians with that.
- `see=true` on actions was tried first and dropped: in 5 runs the model never used it.
- Not measured: computer-use-linux (the `cul` arms exist in `run.py` but have not been run), web
  pages, wide screens. One small app.

Reproduce: `CCDESK_BIN=.venv/bin/ccdesk CCDESK_BASE=<older ccdesk> python bench/run.py --arms base-shots,shots,tree,loaded --reps 5`

## Smaller tool schemas (2026-11-01)

Same form and prompt, Opus 5.5, 5 runs per arm, all filled the form correctly. `before` is the build
merged in PR #3; `after` adds the slimmer schemas and the tab guard.

| Input tokens, median (range) | before | after | first request |
|---|---|---|---|
| ui_tree, alwaysLoad | 63,322 (63,318 to 63,909) | **60,294** (60,294, every run) | 14,868 → **14,112** |
| ui_tree, deferred | 68,064 (67,473 to 68,960) | 67,270 (66,902 to 68,506) | 10,615 → 10,617 |

- With alwaysLoad the schemas are in every request, so the saving repeats: **4.8% fewer input tokens**.
- Deferred, the 1.2% is inside the noise: the ranges overlap, and only the tools the model loads count.

Reproduce: `CCDESK_BIN=.venv/bin/ccdesk CCDESK_BASE=<older ccdesk> python bench/run.py --arms base-loaded,loaded,base,tree --reps 5`
