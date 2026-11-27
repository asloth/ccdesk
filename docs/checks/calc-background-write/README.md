# Covered Calc numeric writes

Only plain numbers can be written through `set_value(cell_ref, text)` or
`set_value(sheet_table_ref, text, cell="B7")`. The helper writes through AT-SPI
Value, reacquires the cell by row/column, and verifies both Value and Text.
`click_element(sheet_table_ref, cell="AA1001")` exclusively selects the requested
cell through AT-SPI Selection without enumerating the sheet's virtual children.

Non-numeric text and formulas return the structured error
`background_text_unsupported`. Numbers work through `set_value`; text needs the
window in front. Calling `activate_window` takes the user's focus. The caller must
then enter text through the foreground window; `set_value` does not automatically
activate, click or type into a Calc cell.

## Retained numeric evidence

Measured 2026-11-26 in disposable `t-calc-check`, created with:

```sh
docker run -d --name t-calc-check --shm-size=2g -e CUA_RESOLUTION=1920x1080 ghcr.io/trycua/linux:24.04
```

Calc 24.2.7.2 used `SAL_USE_VCLPLUGIN=gtk3`, `SAL_ACCESSIBILITY_ENABLED=1`,
`GTK_MODULES=gail:atk-bridge`, an isolated profile, `DISPLAY=:1` and
`DBUS_SESSION_BUS_ADDRESS=unix:path=/run/cua-desktop/bus`. The fixture and sentinel
came from `spike/background-cell-edit:docs/spikes/background-cell-edit/{cells.fods,sentinel.py}`.
The initial Tip of the Day dialog was identified and dismissed during preparation.
The sentinel was activated last, above Calc.

`results.json` retains only the numeric request and its focus, stacking, pointer and
key observations from the original measurement. It is historical numeric evidence,
not a new live run following the product decision. `A1=43` returned `verified: true`;
an independent fresh read returned Text `43` and Value `43.0`.

The numeric operation preserved:

- `_NET_ACTIVE_WINDOW`: `[31457297, 0]` (User activity).
- Full `_NET_CLIENT_LIST_STACKING`: `[16777235, 29360414, 31457297, 14680067, 14680079, 14680083]`.
- Pointer: `[960, 540]`.
- Sentinel key log: empty.
- Every sampled state: zero observed transient changes at 5 ms sampling intervals.
  Sampling cannot exclude shorter changes.

The original selection check found exactly one selected cell at `(1000, 26)` after
public `click_element(table_ref, cell="AA1001")`. Numeric writes and exclusive
selection remain covered by unit tests, including stale proxies, invalid addresses,
selection failures and mismatched readbacks.

`check.py` now exercises only numeric writes and exclusive selection. Copy `ccdesk/`
to `/tmp/ccdesk`, the fixture to `/tmp/cells.fods`, the sentinel to `/tmp/sentinel.py`
and the check to `/tmp/calc-check.py` in a disposable container. Start Calc and the
sentinel as user `cua`, enable toolkit accessibility, then run:

```sh
docker exec -u cua -e DISPLAY=:1 -e DBUS_SESSION_BUS_ADDRESS=unix:path=/run/cua-desktop/bus t-calc-check python3 /tmp/calc-check.py
```

The disposable container was removed after evidence extraction. Neither
`bench-xfce` nor `bench-xfce-2` was accessed. Document saving remains a separate
operation from `set_value`.

## Suite environment

The host accessibility socket stopped responding during an earlier plain Xvfb
run. `dbus-run-session` alone was insufficient because the AT-SPI launcher tried
to use unavailable dbus-broker/systemd services. `test-private-bus.py` starts
private session and accessibility buses directly, starts its registry, runs
`xvfb-run -a uv run pytest -q` without excluding tests, and cleans up its buses.

Run from the repository root:

```sh
python3 /tmp/ccdesk-private-bus-tests.py
```

The same wrapper is retained as `test-private-bus.py` in this directory.

After restricting background writes to numbers, the full requested wrapper run passed:
**1019 passed, 27 skipped, 1 xfailed in 180.44 seconds**.

## Cell selection removed (2026-11-26)

`click_element(..., cell=...)` and the AT-SPI Selection route for Calc cells were removed before merge.
`Selection.select_child` on a Calc sheet makes LibreOffice release and retake PRIMARY (2 owner-change
callbacks per selection, `docs/checks/calc-xdnd-text` on fix/calc-xdnd-text, 500f24d). That changes the user's
middle-click selection, the same kind of disturbance as borrowing CLIPBOARD. Numeric `set_value` never selects and
is unaffected. The selection lines above describe the measurement made before the removal.
