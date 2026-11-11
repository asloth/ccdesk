"""Where the time of type_text, press_key and scroll goes on the pixel path: the action itself, the fixed waits
(KEY_SETTLE, FOCUS_SETTLE, SETTLE, SETTLE_POLL), the subprocess calls, and the rest (captures, guards, audit).

    env -u DISPLAY python bench/waits.py

Starts its own Xvfb with tests/tk_app.py. Nothing is changed in the code under test: backend.sleep and
backend.run are wrapped from here. Also times xdotool itself with and without --clearmodifiers and with the
wheel/click --delay at 0, to price those flags. Refuses to run on DISPLAY :0.
"""

import sys

# bench/typing.py would shadow the standard library's typing module: take this folder off sys.path.
sys.path[:] = [p for p in sys.path if p != __import__("os").path.dirname(__import__("os").path.abspath(__file__))]

import json
import linecache
import os
import shutil
import signal
import statistics
import subprocess
import tempfile
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
N = 10
TEXT = "the quick brown fox jumps over!"[:30]


def main():
    if os.environ.get("DISPLAY", "").split(".")[0] == ":0":
        print("refusing to run: DISPLAY is :0. This sends keys and clicks; it starts its own Xvfb.", file=sys.stderr)
        return 2
    sys.path.insert(0, str(ROOT))
    from ccdesk.backends import X11Backend
    from ccdesk.desktop import Desktop
    from ccdesk.safety import AuditLog, Policy

    r, w = os.pipe()
    xvfb = subprocess.Popen([shutil.which("Xvfb"), "-displayfd", str(w), "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
                            pass_fds=(w,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    os.close(w)
    display = f":{os.read(r, 32).decode().strip()}"
    tmp = Path(tempfile.mkdtemp(prefix="ccdesk-waits-"))
    env = dict(os.environ, DISPLAY=display)
    env.pop("WAYLAND_DISPLAY", None)
    app = subprocess.Popen([sys.executable, str(ROOT / "tests/tk_app.py"), str(tmp / "s.json")], env=env,
                           start_new_session=True)
    try:
        for _ in range(100):
            try:
                st = json.loads((tmp / "s.json").read_text())
                if st.get("ready"):
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        time.sleep(0.5)
        desk = Desktop(backend=X11Backend(env=env), policy=Policy(), audit=AuditLog(tmp / "audit.log"),
                       shots_dir=tmp / "shots")
        desk.activate_window("ccdesk-test")
        lx, ly, lw, lh = st["listbox"]
        bx, by, bw, bh = st["button"]
        listbox = (lx + lw // 2, ly + lh // 2)
        from tests.pngutil import read_png
        e = read_png(desk.screenshot(region="0,0,600,600")["path"]).find_color((255, 255, 0))
        entry = ((e[0] + e[2]) // 2, (e[1] + e[3]) // 2)

        be = desk.backend
        log = {"sleeps": [], "runs": []}
        real_sleep, real_run = be.sleep, be.run

        def sleep(sec):
            line = linecache.getline(sys._getframe(1).f_code.co_filename, sys._getframe(1).f_lineno).strip()
            name = next((n for n in ("KEY_SETTLE", "FOCUS_SETTLE", "SETTLE_POLL", "SETTLE") if n in line), line[:40])
            t = time.perf_counter()
            real_sleep(sec)
            log["sleeps"].append((name, (time.perf_counter() - t) * 1000))

        def run(argv, *a, **kw):
            t = time.perf_counter()
            try:
                return real_run(argv, *a, **kw)
            finally:
                log["runs"].append((" ".join(argv[:3])[:40], (time.perf_counter() - t) * 1000))

        be.sleep, be.run = sleep, run

        def measure(label, fn, setup=None):
            rows = []
            for i in range(N):
                if setup:
                    setup()
                log["sleeps"].clear()
                log["runs"].clear()
                t = time.perf_counter()
                fn()
                total = (time.perf_counter() - t) * 1000
                sl, rn = defaultdict(float), defaultdict(float)
                for n, ms in log["sleeps"]:
                    sl[n] += ms
                for n, ms in log["runs"]:
                    rn[n] += ms
                rows.append((total, dict(sl), dict(rn)))
            keys_s = sorted({k for _, s, _ in rows for k in s})
            keys_r = sorted({k for _, _, x in rows for k in x})
            med = lambda vals: statistics.median(vals)
            tot = med([t for t, _, _ in rows])
            print(f"\n{label}: total {tot:.0f} ms (median of {N})")
            waits = 0.0
            for k in keys_s:
                v = med([s.get(k, 0) for _, s, _ in rows])
                waits += v
                print(f"    wait  {k:14} {v:7.1f}")
            acts = 0.0
            for k in keys_r:
                v = med([x.get(k, 0) for _, _, x in rows])
                acts += v
                print(f"    run   {k:28} {v:7.1f}")
            print(f"    other (capture, guards, audit) {tot - waits - acts:7.1f}")

        clear = lambda: (desk.click(*entry, count=3), desk.press_key("BackSpace"))
        print(f"{'(medians; the ms of every wait and subprocess in the call)':}")
        measure("type_text 30 chars", lambda: desk.type_text(TEXT), setup=clear)
        measure("type_text 30 chars replace=True", lambda: desk.type_text(TEXT, replace=True), setup=clear)
        measure("press_key Tab", lambda: desk.press_key("Tab"), setup=lambda: desk.click(*entry))
        measure("press_key ctrl+l (moves focus)", lambda: desk.press_key("l", modifiers=["ctrl"]))
        measure("scroll dy=3", lambda: desk.scroll(*listbox, dy=3))
        measure("scroll dy=10", lambda: desk.scroll(*listbox, dy=10))

        # xdotool alone, from outside ccdesk
        def xd(*args, text=None):
            samples = []
            for _ in range(N):
                t = time.perf_counter()
                subprocess.run(["xdotool", *args], env=env, input=text, text=True, capture_output=True)
                samples.append((time.perf_counter() - t) * 1000)
            return statistics.median(samples)

        print("\nxdotool alone (median ms)")
        for d in (12, 0):
            print(f"    type 30 chars --clearmodifiers --delay {d:2}: {xd('type', '--clearmodifiers', '--delay', str(d), '--file', '-', text=TEXT):7.1f}")
        print(f"    type 30 chars (no --clearmodifiers) --delay 12: {xd('type', '--delay', '12', '--file', '-', text=TEXT):7.1f}")
        print(f"    key --clearmodifiers Tab: {xd('key', '--clearmodifiers', 'Tab'):7.1f}")
        print(f"    key Tab (no --clearmodifiers): {xd('key', 'Tab'):7.1f}")
        for d in (30, 0):
            print(f"    mousemove + click --repeat 3 --delay {d:2} 5: {xd('mousemove', str(listbox[0]), str(listbox[1]), 'click', '--repeat', '3', '--delay', str(d), '5'):7.1f}")
        for d in (80, 0):
            print(f"    mousemove + click --repeat 2 --delay {d:2} 1: {xd('mousemove', str(bx), str(by), 'click', '--repeat', '2', '--delay', str(d), '1'):7.1f}")
    finally:
        for p in (app, xvfb):
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
