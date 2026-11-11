"""How low can the per-character typing delay go before a toolkit drops, duplicates or reorders characters?

    python bench/typing.py --out typing.json                 # every target, every delay, 20 reps
    python bench/typing.py --load --xdelays 2,1,0 --kdelays 3,1,0 --out typing-load.json
    python bench/typing.py --compare typing.json typing-load.json

Every target runs in its own Xvfb (started here, never $DISPLAY): a Tk entry (tests/tk_app.py), a GTK 3
entry, a GTK 3 text view (tests/gtk_text_app.py), a GTK 4 entry, and Firefox on a local page whose input and
textarea mirror their value into the window title. The ones that are not installed are skipped and listed.
Two paths are tried: xdotool (x11.TYPE_DELAY_MS) and the own-pointer keyboard (mpx.KEY_DELAY, mpx.SPARE_SETTLE).
Each cell types one text 20 times, reads back what the app received and compares it exactly.
`--load` keeps every core busy for the whole run. Refuses to run on DISPLAY :0, like bench/pixel.py.
"""

import sys

# This file is called typing.py, and its folder is sys.path[0]: take it out so the standard library's
# typing module is found first.
sys.path[:] = [p for p in sys.path if p not in (__import__("os").path.dirname(__import__("os").path.abspath(__file__)),)]

import argparse
import json
import os
import shutil
import signal
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY3 = "/usr/bin/python3"
TEXTS = {
    "ascii30": "the quick brown fox jumps over!"[:30],
    "ascii200": ("pack my box with five dozen liquor jugs and sphinx of black quartz judge my vow while "
                 "the five boxing wizards jump quickly over lazy dogs 0123456789 and again how vexingly "
                 "quick daft zebras jump"),
    "shift": "Hola, Mundo! (2026) #1 @home",
    "nonascii": "ñandú café — 你好 😀",
    "repeat": "aaaa bbbb 1111 ....",
}
DEFAULT_SPARE = 0.03
GTK3_ENTRY = """
import sys, gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
win = Gtk.Window(title="ccdesk-entry3"); win.set_default_size(400, 60)
e = Gtk.Entry(); win.add(e)
def ch(_):
    open(sys.argv[1] + ".tmp", "w").write(e.get_text()); import os; os.replace(sys.argv[1] + ".tmp", sys.argv[1])
e.connect("changed", ch); win.connect("destroy", Gtk.main_quit); win.show_all(); ch(0); Gtk.main()
"""
GTK4_ENTRY = """
import sys, os, gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib
Gtk.init()
win = Gtk.Window(title="ccdesk-entry4"); win.set_default_size(400, 60)
e = Gtk.Entry(); win.set_child(e)
def ch(*_):
    open(sys.argv[1] + ".tmp", "w").write(e.get_text()); os.replace(sys.argv[1] + ".tmp", sys.argv[1])
e.connect("changed", ch); loop = GLib.MainLoop(); win.connect("close-request", lambda *_: loop.quit())
win.present(); ch(); loop.run()
"""
FF_PAGE = """<!doctype html><meta charset=utf-8><title>start</title>
<body><input id=i style="width:90%%;font-size:20px" %s><br>
<textarea id=t rows=6 style="width:90%%;font-size:20px" %s></textarea>
<script>
function m(){document.title='I['+i.value+']T['+t.value+']';}
i.addEventListener('input',m);t.addEventListener('input',m);m();
</script>"""
CLEAR = {"tk": ["ctrl+a", "ctrl+k"]}  # Tk: ctrl+a is "line start", ctrl+k kills to the end
CLEAR_DEFAULT = ["ctrl+a", "BackSpace"]


# -- worker: one target on its own display -----------------------------------------------------------

def start_xvfb():
    r, w = os.pipe()
    proc = subprocess.Popen([shutil.which("Xvfb"), "-displayfd", str(w), "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
                            pass_fds=(w,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    os.close(w)
    line = os.read(r, 32).decode().strip()
    os.close(r)
    if not line:
        raise SystemExit("Xvfb did not start")
    return proc, f":{line}"


def xdo(env, *args):
    return subprocess.run(["xdotool", *args], env=env, capture_output=True, text=True, timeout=10).stdout.strip()


class Target:
    """One app plus how to read back what it holds."""

    def __init__(self, name, env, tmp):
        self.name, self.env, self.tmp = name, env, Path(tmp)
        self.proc = None
        self.state = self.tmp / "state.txt"
        self.wid = None
        self.point = None  # where to click to focus the field

    def launch(self, argv, **kw):
        self.proc = subprocess.Popen(argv, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True, **kw)

    def window(self, name_pat, timeout=40):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            ids = xdo(self.env, "search", "--onlyvisible", "--name", name_pat).split()
            if ids:
                self.wid = int(ids[-1])
                return
            time.sleep(0.2)
        raise SystemExit(f"{self.name}: window {name_pat!r} never appeared")

    def center(self):
        g = dict(kv.split("=") for kv in xdo(self.env, "getwindowgeometry", "--shell", str(self.wid)).split())
        return int(g["X"]) + int(g["WIDTH"]) // 2, int(g["Y"]) + int(g["HEIGHT"]) // 2

    def read(self):
        raise NotImplementedError

    def stop(self):
        if self.proc:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except OSError:
                pass


class FileTarget(Target):
    def read(self):
        try:
            return self.state.read_text()
        except OSError:
            return None


class TkTarget(Target):
    def read(self):
        try:
            return json.loads(self.state.read_text())["entry"]
        except (OSError, ValueError, KeyError):
            return None

    def start(self):
        self.launch([sys.executable, str(ROOT / "tests/tk_app.py"), str(self.state)])
        self.window("^ccdesk-test$")
        end = time.monotonic() + 10
        while time.monotonic() < end and self.read() is None:
            time.sleep(0.1)
        sys.path.insert(0, str(ROOT))
        from tests.pngutil import read_png
        time.sleep(0.5)
        shot = self.tmp / "tk.png"
        subprocess.run(["scrot", "-o", str(shot)], env=self.env, check=True)
        e = read_png(str(shot)).find_color((255, 255, 0))
        self.point = ((e[0] + e[2]) // 2, (e[1] + e[3]) // 2)


class TextViewTarget(FileTarget):
    def start(self):
        self.launch([PY3, str(ROOT / "tests/gtk_text_app.py"), str(self.state)])
        self.window("^ccdesk-text$")
        time.sleep(1.0)
        self.point = self.center()

    def read(self):
        return super().read()


class GtkEntryTarget(FileTarget):
    def __init__(self, name, env, tmp, code, title):
        super().__init__(name, env, tmp)
        self.code, self.title = code, title

    def start(self):
        self.launch([PY3, "-c", self.code, str(self.state)])
        self.window(f"^{self.title}$")
        time.sleep(1.0)
        self.point = self.center()


class FirefoxTarget(Target):
    def __init__(self, name, env, tmp, field):
        super().__init__(name, env, tmp)
        self.field = field

    def read(self):
        title = xdo(self.env, "getwindowname", str(self.wid)) if self.wid else ""
        if not title.startswith("I["):
            return None
        i, _, t = title.partition("]T[")
        t = t.rsplit("]", 1)[0] if "]" in t else t
        return i[2:] if self.field == "input" else t

    def start(self):
        page = self.tmp / "page.html"
        page.write_text(FF_PAGE % (("autofocus", "") if self.field == "input" else ("", "autofocus")))
        prof = self.tmp / "profile"
        prof.mkdir()
        (prof / "user.js").write_text("\n".join(
            f'user_pref("{k}", {v});' for k, v in {
                "browser.shell.checkDefaultBrowser": "false", "datareporting.policy.dataSubmissionEnabled": "false",
                "browser.aboutwelcome.enabled": "false", "browser.startup.homepage_override.mstone": '"ignore"',
                "browser.tabs.warnOnClose": "false", "app.update.enabled": "false",
                "toolkit.telemetry.reportingpolicy.firstRun": "false", "browser.newtabpage.enabled": "false",
                "network.captive-portal-service.enabled": "false", "browser.search.suggest.enabled": "false",
                "extensions.autoDisableScopes": "15"}.items()))
        env = dict(self.env, MOZ_ENABLE_WAYLAND="0", GDK_BACKEND="x11", MOZ_DISABLE_CONTENT_SANDBOX="1")
        self.env = env
        self.launch(["firefox", "--no-remote", "--profile", str(prof), "--new-window", f"file://{page}"])
        end = time.monotonic() + 55
        while time.monotonic() < end:
            ids = xdo(env, "search", "--onlyvisible", "--name", r"^I\[").split()
            if ids:
                self.wid = int(ids[-1])
                break
            time.sleep(0.5)
        else:
            raise SystemExit("firefox did not show the page within 55 s")
        time.sleep(2.0)
        x, y = self.center()
        self.point = (x, y)  # no click for Firefox: the field has autofocus; a click would land anywhere


class XdotoolPath:
    name = "xdotool"

    def __init__(self, env):
        from ccdesk.backends import X11Backend
        self.backend = X11Backend(env=env)
        self.env = env

    def set(self, delay_ms, **_):
        from ccdesk.backends import x11
        x11.TYPE_DELAY_MS = delay_ms

    def focus(self, tg, click=True):
        xdo(self.env, "windowfocus", str(tg.wid))
        if click and not isinstance(tg, FirefoxTarget):
            self.backend.click(*tg.point, 1, 1)

    def type(self, text):
        self.backend.type_text(text)

    def key(self, name):
        from ccdesk.parsing import parse_key_combo
        self.backend.press_key(parse_key_combo(name))

    def close(self):
        pass


class MpxPath:
    name = "mpx"

    def __init__(self, env):
        from ccdesk.backends.mpx import Mpx
        self.mpx = Mpx("bench", env)
        self.mpx.create()

    def set(self, key_delay, spare=DEFAULT_SPARE, **_):
        from ccdesk.backends import mpx
        mpx.KEY_DELAY, mpx.SPARE_SETTLE = key_delay / 1000, spare

    def focus(self, tg, click=True):
        self.mpx.focus(tg.wid)
        if click and not isinstance(tg, FirefoxTarget):
            self.mpx.warp(*tg.point)
            self.mpx.button(1, True)
            self.mpx.button(1, False)

    def type(self, text):
        self.mpx.type_text(text)

    def key(self, name):
        from ccdesk.parsing import parse_key_combo
        self.mpx.press_key(parse_key_combo(name))

    def close(self):
        self.mpx.close()


def classify(exp, got):
    if got is None:
        return "no-read"
    if len(got) < len(exp):
        return "dropped"
    if len(got) > len(exp):
        return "duplicated"
    if sorted(got) == sorted(exp):
        return "reordered"
    return "wrong"


def settle(tg, exp, stable=0.5, timeout=6.0):
    """The value once it equals `exp`, or once it has not moved for `stable` seconds."""
    end = time.monotonic() + timeout
    last, since = tg.read(), time.monotonic()
    while time.monotonic() < end:
        cur = tg.read()
        if cur == exp:
            return cur
        if cur != last:
            last, since = cur, time.monotonic()
        elif time.monotonic() - since >= stable:
            break
        time.sleep(0.02)
    return last


def clear(path, tg):
    for attempt in range(3):
        for k in CLEAR.get(tg.name.split("-")[0], CLEAR_DEFAULT):
            path.key(k)
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            if tg.read() == "":
                return True
            time.sleep(0.02)
        path.focus(tg)
    return False


def run_cell(path, tg, text_name, reps):
    text, times, fails, examples, setup_fail = TEXTS[text_name], [], collections_counter(), [], 0
    for _ in range(reps):
        if not clear(path, tg):
            setup_fail += 1
            fails["no-clear"] += 1
            continue
        t = time.perf_counter()
        path.type(text)
        times.append((time.perf_counter() - t) * 1000)
        got = settle(tg, text)
        if got != text:
            kind = classify(text, got)
            fails[kind] += 1
            if len(examples) < 2:
                examples.append(got)
    return {"text": text_name, "reps": reps, "failures": sum(fails.values()), "kinds": dict(fails),
            "median_ms": round(statistics.median(times), 1) if times else None, "examples": examples}


def collections_counter():
    import collections
    return collections.Counter()


def worker(args):
    sys.path.insert(0, str(ROOT))
    xvfb, display = start_xvfb()
    tmp = tempfile.mkdtemp(prefix="ccdesk-typing-")
    env = dict(os.environ, DISPLAY=display, NO_AT_BRIDGE="1", GDK_BACKEND="x11")
    for k in ("WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS"):
        env.pop(k, None)
    name = args.worker
    tg = None
    paths = []
    out = {"target": name, "display": display, "cells": []}
    try:
        if name == "tk":
            tg = TkTarget("tk", env, tmp)
        elif name == "gtk3-entry":
            tg = GtkEntryTarget("gtk3-entry", env, tmp, GTK3_ENTRY, "ccdesk-entry3")
        elif name == "gtk3-textview":
            tg = TextViewTarget("gtk3-textview", env, tmp)
        elif name == "gtk4-entry":
            tg = GtkEntryTarget("gtk4-entry", env, tmp, GTK4_ENTRY, "ccdesk-entry4")
        elif name == "ff-input":
            tg = FirefoxTarget("ff-input", env, tmp, "input")
        elif name == "ff-textarea":
            tg = FirefoxTarget("ff-textarea", env, tmp, "textarea")
        tg.start()
        env = tg.env
        out["window"] = tg.wid
        settings = []
        if "xdotool" in args.paths:
            settings += [("xdotool", {"delay_ms": d}, TEXTS) for d in args.xdelays]
        if "mpx" in args.paths:
            for kd in args.kdelays:
                for sp in args.spares:
                    only = TEXTS if sp == DEFAULT_SPARE else ["nonascii"]
                    settings.append(("mpx", {"key_delay": kd, "spare": sp}, only))
        for pname, params, texts in settings:
            if not paths or paths[-1].name != pname:
                for p in paths:
                    p.close()
                paths = [XdotoolPath(env) if pname == "xdotool" else MpxPath(env)]
            path = paths[0]
            path.set(**({"delay_ms": params["delay_ms"]} if pname == "xdotool" else params))
            path.focus(tg)
            for tn in texts:
                cell = run_cell(path, tg, tn, args.reps)
                cell.update(path=pname, **params)
                out["cells"].append(cell)
                print(f"[{name}] {pname} {params} {tn}: {cell['failures']}/{args.reps} fail, {cell['median_ms']} ms",
                      flush=True)
    finally:
        for p in paths:
            try:
                p.close()
            except Exception:
                pass
        if tg:
            tg.stop()
        try:
            os.killpg(xvfb.pid, signal.SIGKILL)
        except OSError:
            pass
        shutil.rmtree(tmp, ignore_errors=True)
    Path(args.out).write_text(json.dumps(out, indent=1))


# -- orchestrator --------------------------------------------------------------------------------

def installed():
    ok, skipped = ["tk"], {}
    for name, mod in (("gtk3-entry", "3.0"), ("gtk3-textview", "3.0"), ("gtk4-entry", "4.0")):
        r = subprocess.run([PY3, "-c", f"import gi; gi.require_version('Gtk','{mod}')"], capture_output=True)
        (ok.append(name) if r.returncode == 0 else skipped.__setitem__(name, "no PyGObject Gtk " + mod))
    if shutil.which("firefox"):
        ok += ["ff-input", "ff-textarea"]
    else:
        skipped["firefox"] = "not installed"
    skipped["qt"] = "no PyQt5/PyQt6/PySide6 for /usr/bin/python3" if subprocess.run(
        [PY3, "-c", "import PyQt5"], capture_output=True).returncode and subprocess.run(
        [PY3, "-c", "import PyQt6"], capture_output=True).returncode else "present (not wired)"
    if not shutil.which("xterm"):
        skipped["xterm"] = "not installed"
    return ok, skipped


def summarize(results):
    cells = {}
    for r in results:
        for c in r["cells"]:
            key = (c["path"], c.get("delay_ms", c.get("key_delay")), c.get("spare"))
            cells.setdefault(key, {})[(r["target"], c["text"])] = c
    targets = sorted({r["target"] for r in results})
    texts = list(TEXTS)
    print("\nfailures (out of 20 per text) and median ms, per setting")
    print(f"{'setting':22} {'text':9} " + " ".join(f"{t:>13}" for t in targets) + "   median ms (all targets)")
    for key in sorted(cells, key=lambda k: (k[0], -k[1], -(k[2] or 0))):
        for tn in texts:
            row = [cells[key].get((t, tn)) for t in targets]
            if not any(row):
                continue
            ms = [c["median_ms"] for c in row if c and c["median_ms"] is not None]
            label = f"{key[0]} {key[1]}ms" + (f" spare={key[2]}" if key[0] == "mpx" else "")
            print(f"{label:22} {tn:9} " + " ".join(f"{(str(c['failures']) + '/' + str(c['reps'])) if c else '-':>13}" for c in row)
                  + f"   {statistics.median(ms):8.1f}" if ms else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--compare", nargs="+")
    ap.add_argument("--worker")
    ap.add_argument("--targets", default="all")
    ap.add_argument("--paths", default="xdotool,mpx")
    ap.add_argument("--xdelays", default="12,8,4,2,1,0")
    ap.add_argument("--kdelays", default="6,3,1,0")
    ap.add_argument("--spares", default="0.03,0.015")
    ap.add_argument("--reps", type=int, default=20)
    ap.add_argument("--load", action="store_true", help="busy-loop on every core for the whole run")
    args = ap.parse_args()
    if args.compare:
        return summarize([r for p in args.compare for r in json.loads(Path(p).read_text())["results"]])
    display = os.environ.get("DISPLAY", "")
    if display.split(".")[0] == ":0":
        print("refusing to run: DISPLAY is :0, the real desktop. This benchmark starts its own Xvfb displays and "
              "sends keys only to them; unset DISPLAY or point it at an Xvfb.", file=sys.stderr)
        return 2
    args.xdelays = [int(x) for x in args.xdelays.split(",")]
    args.kdelays = [int(x) for x in args.kdelays.split(",")]
    args.spares = [float(x) for x in args.spares.split(",")]
    args.paths = args.paths.split(",")
    if args.worker:
        return worker(args)
    ok, skipped = installed()
    targets = ok if args.targets == "all" else args.targets.split(",")
    print("targets:", targets, "skipped:", skipped, flush=True)
    tmp = Path(tempfile.mkdtemp(prefix="ccdesk-typing-run-"))
    load = []
    if args.load:
        for _ in range(os.cpu_count() or 4):
            load.append(subprocess.Popen([sys.executable, "-c", "while True: pass"], start_new_session=True))
    procs = {}
    try:
        for t in targets:
            cmd = [sys.executable, __file__, "--worker", t, "--out", str(tmp / f"{t}.json"), "--paths", ",".join(args.paths),
                   "--xdelays", ",".join(map(str, args.xdelays)), "--kdelays", ",".join(map(str, args.kdelays)),
                   "--spares", ",".join(map(str, args.spares)), "--reps", str(args.reps)]
            procs[t] = subprocess.Popen(cmd, env=dict(os.environ, DISPLAY=""), start_new_session=True)
        for t, p in procs.items():
            p.wait()
    finally:
        for p in list(procs.values()) + load:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
    results = [json.loads(f.read_text()) for f in sorted(tmp.glob("*.json"))]
    summarize(results)
    if args.out:
        Path(args.out).write_text(json.dumps({"skipped": skipped, "load": args.load, "results": results}, indent=1))
        print(f"wrote {args.out}")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
