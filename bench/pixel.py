"""Per-action latency of the pixel path (apps with no accessibility tree), against tests/tk_app.py.

    xvfb-run -a -s "-screen 0 1920x1080x24" python bench/pixel.py --out new.json
    python bench/pixel.py --compare old.json new.json

Refuses to run on DISPLAY :0 because it sends input. To compare with an older checkout:

    CCDESK_SRC=/tmp/ccdesk-old PYTHONPATH=/tmp/ccdesk-old xvfb-run -a -s "-screen 0 1920x1080x24" \
        python bench/pixel.py --out old.json

CCDESK_SRC is the checkout whose tests/tk_app.py is used (default: this one). PYTHONPATH picks
the ccdesk under test. Each row is the median of 10 runs; all samples are kept in the JSON.
`correct` says the app received what was sent (label changed, text typed, rows scrolled, window
active). A faster action that drops input is a regression.
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(os.environ.get("CCDESK_SRC") or Path(__file__).resolve().parent.parent)
N = 10
TITLE, OTHER = "ccdesk-test", "ccdesk-other"
TEXT = "the quick brown fox jumps over!"[:30]
YELLOW = (255, 255, 0)


def compare(a_path, b_path):
    a, b = (json.loads(Path(p).read_text()) for p in (a_path, b_path))
    ra = {r["action"]: r for r in a.get("rows", [])}
    rb = {r["action"]: r for r in b.get("rows", [])}

    def ms(v):
        return f"{v:9.1f}" if isinstance(v, (int, float)) else f"{'-':>9}"

    def sp(r):
        v = r.get("spawns_per_call")
        return f"{v:8.1f}" if isinstance(v, (int, float)) else f"{'-':>8}"

    print(f"{'action':34} {'A ms':>9} {'B ms':>9} {'B/A':>7}  {'spawnsA':>8} {'spawnsB':>8}  correct A/B")
    for name in list(ra) + [k for k in rb if k not in ra]:
        x, y = ra.get(name, {}), rb.get(name, {})
        ma, mb = x.get("median_ms"), y.get("median_ms")
        ratio = f"{mb / ma:6.2f}x" if ma and mb else "    n/a"
        print(f"{name:34} {ms(ma)} {ms(mb)} {ratio:>7}  {sp(x)} {sp(y)}  {x.get('correct', '-')}/{y.get('correct', '-')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    args = ap.parse_args()
    if args.compare:
        return compare(*args.compare)

    display = os.environ.get("DISPLAY", "")
    if not display or display.split(".")[0] == ":0":
        print(f"refusing to run: DISPLAY is {display!r}. This benchmark sends clicks and keys, and :0 is the "
              "real desktop. Run it under xvfb-run -a -s \"-screen 0 1920x1080x24\".", file=sys.stderr)
        return 2

    sys.path.insert(0, str(ROOT))
    from ccdesk.backends import X11Backend
    from ccdesk.desktop import Desktop
    from ccdesk.safety import AuditLog, Policy
    from tests.pngutil import read_png

    tmp = Path(tempfile.mkdtemp(prefix="ccdesk-pixel-"))
    st_main, st_other = tmp / "main.json", tmp / "other.json"
    env = dict(os.environ)
    env.pop("WAYLAND_DISPLAY", None)
    src = (ROOT / "tests/tk_app.py").read_text()
    other_src = src.replace(f'TITLE = "{TITLE}"', f'TITLE = "{OTHER}"').replace("+40+50", "+700+50")
    runner = f"import sys; exec(compile({other_src!r}, 'tk_app_other', 'exec'))"
    procs = [subprocess.Popen([sys.executable, "-c", runner, str(st_other)], env=env),
             subprocess.Popen([sys.executable, str(ROOT / "tests/tk_app.py"), str(st_main)], env=env)]

    def state():
        try:
            return json.loads(st_main.read_text())
        except (OSError, ValueError):
            return {}

    def wait_for(pred, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            s = state()
            if s and pred(s):
                return True
            time.sleep(0.02)
        return False

    try:
        if not wait_for(lambda s: s.get("ready"), 10):
            raise SystemExit("tk app never became ready")
        desk = Desktop(backend=X11Backend(env=env), policy=Policy(), audit=AuditLog(tmp / "audit.log"),
                       shots_dir=tmp / "shots")
        end = time.monotonic() + 10
        titles = []
        while time.monotonic() < end:
            titles = [w.title for w in desk.backend.list_windows()]
            if TITLE in titles and OTHER in titles:
                break
            time.sleep(0.05)
        else:
            raise SystemExit(f"windows not listed: {titles}")
        time.sleep(0.5)

        spawns = [0]
        real_run = desk.backend.run

        def counting_run(*a, **kw):
            spawns[0] += 1
            return real_run(*a, **kw)

        desk.backend.run = counting_run

        bx, by, bw, bh = state()["button"]
        button = (bx + bw // 2, by + bh // 2)
        lx, ly, lw, lh = state()["listbox"]
        listbox = (lx + lw // 2, ly + lh // 2)
        img = read_png(desk.screenshot(region="0,0,600,600")["path"])  # main window only; the other has a yellow entry too
        e = img.find_color(YELLOW)
        entry = ((e[0] + e[2]) // 2, (e[1] + e[3]) // 2)
        empty = (48, 300)  # left of the canvas, inside the window, plain background

        # Tk publishes no tree, so a click reports pixels (screen_changed), never tree changes.
        probe = desk.click(*button)
        if "screen_changed" not in probe or "changes" in probe:
            raise SystemExit(f"Tk app seems to publish a tree (click result: {probe}); not the pixel path")

        rows = []

        def measure(label, fn, setup=None, check=None, n=N):
            samples, ok, counts = [], True, []
            for i in range(n):
                if setup:
                    setup(i)
                spawns[0] = 0
                t = time.perf_counter()
                res = fn(i)
                samples.append((time.perf_counter() - t) * 1000)
                counts.append(spawns[0])
                if check:
                    try:
                        good = bool(check(i, res))
                    except Exception as exc:
                        good, res = False, f"{type(exc).__name__}: {exc}"
                    if not good and os.environ.get("CCDESK_BENCH_DEBUG"):
                        print(f"    check failed, run {i}: {res} state={state()}", file=sys.stderr)
                    ok = good and ok
            rows.append({"action": label, "median_ms": round(statistics.median(samples), 1),
                         "min_ms": round(min(samples), 1), "max_ms": round(max(samples), 1),
                         "spawns_per_call": round(statistics.mean(counts), 2),
                         "correct": ok, "samples_ms": [round(s, 1) for s in samples]})
            r = rows[-1]
            print(f"  {label:34} {r['median_ms']:8.1f} {r['min_ms']:8.1f} {r['max_ms']:8.1f} "
                  f"{r['spawns_per_call']:7.1f}  {r['correct']}", flush=True)

        print(f"  {'action':34} {'median':>8} {'min':>8} {'max':>8} {'spawns':>7}  correct")
        measure("list_windows", lambda i: desk.list_windows(),
                check=lambda i, r: {TITLE, OTHER} <= {w["title"] for w in r["windows"]})
        measure("screenshot full", lambda i: desk.screenshot(),
                check=lambda i, r: Path(r["path"]).exists())
        measure("screenshot region 100,100,800,600", lambda i: desk.screenshot(region="100,100,800,600"),
                check=lambda i, r: Path(r["path"]).exists())
        measure("screenshot scale=0.5", lambda i: desk.screenshot(scale=0.5),
                check=lambda i, r: Path(r["path"]).exists())
        measure("screenshot window scale=0.5", lambda i: desk.screenshot(window=TITLE, scale=0.5),
                check=lambda i, r: Path(r["path"]).exists())

        desk.activate_window(TITLE)

        def to_submitted(i):  # label text becomes "submitted:..." so the next button click changes it
            desk.click(*entry)
            desk.press_key("Return")
            wait_for(lambda s: s["label"].startswith("submitted"))
            time.sleep(0.1)

        measure("click (changes window)", lambda i: desk.click(*button), setup=to_submitted,
                check=lambda i, r: wait_for(lambda s: s["label"] == "clicked", 1.0)
                and r.get("screen_changed") is True)

        before = []

        def label_before(i):
            desk.click(*listbox)  # take focus off the entry: its blinking caret would count as a change
            wait_for(lambda s: s["label"] == "clicked", 1.0)
            before.append(state()["label"])

        measure("click (nothing changes)", lambda i: desk.click(*empty), setup=label_before,
                check=lambda i, r: state()["label"] == before[i] and r.get("screen_changed") is False)

        def clear_entry(i):
            desk.click(*entry, count=3)  # triple click selects the whole line (ctrl+a is "line start" in Tk)
            desk.press_key("BackSpace")
            wait_for(lambda s: s["entry"] == "", 1.0)

        measure("type_text 30 chars", lambda i: desk.type_text(TEXT), setup=clear_entry,
                check=lambda i, r: wait_for(lambda s: s["entry"] == TEXT, 2.0))

        measure("press_key Tab", lambda i: desk.press_key("Tab"), setup=lambda i: desk.click(*entry),
                check=lambda i, r: r.get("pressed") is not None and state()["entry"] == TEXT)

        tops = []
        measure("scroll dy=3", lambda i: desk.scroll(*listbox, dy=3), setup=lambda i: tops.append(state()["list_top"]),
                check=lambda i, r: wait_for(lambda s: s["list_top"] > tops[i], 1.5))

        wanted = [OTHER, TITLE]

        # Xvfb runs no window manager, so the X server never reports a focused window
        # (getwindowfocus says PointerRoot). The check is that the answer names the requested window.
        measure("activate_window", lambda i: desk.activate_window(wanted[i % 2]),
                check=lambda i, r: r["activated"]["title"] == wanted[i % 2])

        result = {"ccdesk_src": str(ROOT), "display": display, "n": N, "rows": rows}
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(json.dumps(result, indent=1))
            print(f"wrote {args.out}")
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
