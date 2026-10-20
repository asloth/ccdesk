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

Reproduce: `PATH=~/.local/opt/xvfb/usr/bin:$PATH python3 bench/run.py --reps 3`
