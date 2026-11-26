# Background action routes check

Measured 2026-11-26 in disposable `t-routes-check`, image
`ghcr.io/trycua/linux:24.04`, 1920x1080, 2 GB shared memory.
Chromium 156.0.8078.73, xfwm4 4.18.0, AT-SPI GI 2.52.0.

The form is copied unchanged from `spike/paste-by-menu`'s
`docs/spikes/background-cell-edit/form.html`. All writes and clicks use the
public `Desktop` methods and real AT-SPI helper. Page status is read through
AT-SPI, with no DOM scripting. A Tk sentinel completely covers Chromium.

| Attempt | Choice | Submit action | Text result | Unchanged samples |
| --- | --- | --- | --- | --- |
| 1 | old (already selected) | press | background_text_unsupported | 153 |
| 2 | new | press | background_text_unsupported | 67 |
| 3 | old | press | background_text_unsupported | 65 |

All three submissions held `['old', 'old', choice]` in the DOM readback and
the same choice in the event listener state. Changing the choice produced
`input` and `change` events. The already-selected choice stayed selected;
calling its `select` action again can toggle it off on this Chromium build.
The harness first checks the child menu's Selection to avoid that call.

All before/after snapshots and 285 samples (approximately 5 ms polling)
retained `_NET_ACTIVE_WINDOW=[44040208,0]`, keyboard focus `44040208`, pointer
`[960,540]`, and complete stacking
`[12582931,29360131,44040208,14680067,14680079,14680083]`.
The text refusal reported `input_emitted=false` and left Input at `old`.
No popup appeared in these snapshots. Sampling cannot rule out shorter
transients. Earlier exploratory attempts appear in the form's cumulative
event log; only these final three attempts are counted here.

`evidence.json` retains the public method results (pixel/tree change reports
omitted), full page status, snapshots, and sample counts. `check.py` and
`sentinel.py` reproduce the final check. Copy ccdesk to `/tmp/ccdesk`, this
form to `/tmp/form.html`, check.py to `/tmp/routes-check.py` and sentinel.py
to `/tmp/routes-sentinel.py` in the disposable container. Install
`python3-xlib python3-tk python3-gi gir1.2-atspi-2.0 scrot imagemagick` there.
Enable the accessibility bus, then launch Chromium and the check as `cua`
with `DISPLAY=:1`, `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/cua-desktop/bus`
and `XDG_RUNTIME_DIR=/run/cua-desktop`. Use `runuser -u cua -- env ...` from
root so Chromium receives cua's home directory.

Chromium flags:

```text
--no-sandbox --force-renderer-accessibility --user-data-dir=/tmp/chromium-routes2
--no-first-run --disable-dev-shm-usage --window-position=80,80 --window-size=800,650
file:///tmp/form.html
```

GTK replacement, delete/insert fallback, readback mismatches, missing
interfaces, refused actions and foreground typing are covered by unit tests.
This container check does not claim a live GnuCash or Electron measurement.
No Calc table-cell route is part of this change.

Validation of the final code:

```text
xvfb-run -a uv run pytest -q
987 passed, 36 skipped in 165.63s
```

The new route tests independently passed `34 passed`. Focused real GTK,
headless and Wayland integrations on a private D-Bus session with an
explicitly started `/usr/lib/at-spi2-registryd` passed `10 passed, 1 xfailed`.
Graphical prerequisites affect skips on this host. Earlier full runs had
intermittent headless isolation / Wayland coordinate failures; the final
required-command run above passed. This change does not fix those separate
integration-session issues.
