# Covered Calc cell writes

Measured 2026-11-26 in disposable `t-calc-check`, created with:

```sh
docker run -d --name t-calc-check --shm-size=2g -e CUA_RESOLUTION=1920x1080 ghcr.io/trycua/linux:24.04
```

Installed `libreoffice-calc libreoffice-gtk3 python3-pyatspi python3-xlib python3-tk libatk-adaptor libglib2.0-bin xclip` in this container. Calc 24.2.7.2 used `SAL_USE_VCLPLUGIN=gtk3`, `SAL_ACCESSIBILITY_ENABLED=1`, `GTK_MODULES=gail:atk-bridge`, an isolated profile, `DISPLAY=:1` and `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/cua-desktop/bus`. The fixture and sentinel came from `spike/background-cell-edit:docs/spikes/background-cell-edit/{cells.fods,sentinel.py}`. The initial Tip of the Day dialog was identified and dismissed during preparation. The sentinel was activated last, above Calc.

`check.py` runs public `Desktop.set_value` against a sheet table ref found with `ui_tree(window="cells.fods", query="Sheet")`, through the real persistent helper. `query` avoids exhausting the tree's node cap on Calc's offscreen menus. Copy `ccdesk/` to `/tmp/ccdesk`, the fixture to `/tmp/cells.fods`, and the sentinel to `/tmp/sentinel.py` in the container. Start Calc and the sentinel as user `cua`, enable toolkit accessibility, then run:

```sh
docker exec -u cua -e DISPLAY=:1 -e DBUS_SESSION_BUS_ADDRESS=unix:path=/run/cua-desktop/bus t-calc-check python3 /tmp/calc-check.py
```

The final requests use different values from the earlier exploration, so the old contents cannot satisfy readback. `results.json` contains the complete final before/after records and 5 ms monitor observations.

| Cell | Requested | Independent fresh readback | Verified |
| --- | --- | --- | --- |
| A1 | `43` | Text `43`, Value `43.0` | true |
| B7 | `final background text` | Text `final background text` | true |
| C8 | `=2+3` | Text `5`, Value `5.0`, Formula attribute `2+3` | true |

All three operations preserved:

- `_NET_ACTIVE_WINDOW`: `[31457297, 0]` (User activity).
- Full `_NET_CLIENT_LIST_STACKING`: `[16777235, 29360414, 31457297, 14680067, 14680079, 14680083]`.
- Pointer: `[960, 540]`.
- Sentinel key log: empty.
- Every sampled state: zero observed transient changes. Sampling cannot exclude changes shorter than its interval.

Text and formula answers explicitly report brief clipboard use and restoration. Each final request restored `previous clipboard`. A separately resolved toolbar Save persisted the document; `saved-cells.fods` contains numeric 43, the exact string, and `table:formula="of:=2+3"` with numeric value 5. Saving was outside the monitored write intervals.

`extra-check.py` additionally put binary bytes containing NUL and 0xff in an `image/png` clipboard target, wrote D9 through Desktop, and compared the complete target snapshot before/after. Both bytes and target type/format matched. Injecting a selection error after taking clipboard ownership also restored that same binary snapshot. Public `click_element(table_ref, cell="AA1001")` returned accessibility selection; an independent AT-SPI check found exactly one cell at `(1000, 26)`. These extra checks do not claim a monitored noninterference run.

The Formula attribute omits the leading equals sign on this Calc build. Matching only the computed Text would not prove that the requested formula landed; the helper compares the original formula attribute too. Numeric writes require both Text and Value readback. Stale reads reacquire the cell by row/column.

## Suite environment

The plain `xvfb-run -a uv run pytest -q` attempt encountered three Wayland failures when the host accessibility socket stopped responding. `dbus-run-session` alone was insufficient on this host: its AT-SPI launcher tried to use dbus-broker/systemd services unavailable in the isolated session. `test-private-bus.py` starts private session and accessibility buses directly, starts its registry, then runs the exact Xvfb/uv/pytest command without excluding tests or changing the test suite. It cleans up its own buses afterward. The affected Wayland module passed there (4 passed, 1 existing xfail). The final complete run passed: **1035 passed, 27 skipped, 1 xfailed in 179.27 seconds**, including all 74 Calc-specific unit tests. No test was excluded by the wrapper.

Run from the repository root:

```sh
python3 docs/checks/calc-background-write/test-private-bus.py
```

The disposable container was removed after evidence extraction. Neither `bench-xfce` nor `bench-xfce-2` was accessed. No benchmark-wide pass is claimed, and document saving remains a separate operation from `set_value`.
