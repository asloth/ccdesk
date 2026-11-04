"""Per-action latency of the accessibility tools, against tests/gtk_app.py on an X server.

    xvfb-run -a python bench/actions.py

Times the calls an agent makes in the look-act-verify loop, so a change to the helper can be
compared with an older checkout:

    git worktree add /tmp/ccdesk-old <old-ref>
    CCDESK_SRC=/tmp/ccdesk-old PYTHONPATH=/tmp/ccdesk-old xvfb-run -a python bench/actions.py

CCDESK_SRC is the checkout whose tests/gtk_app.py is used (default: this one). PYTHONPATH picks
the ccdesk under test. Numbers are medians of five runs, printed in milliseconds.
"""

import json
import os
import re
import statistics
import subprocess
import time
from pathlib import Path

ROOT = Path(os.environ.get("CCDESK_SRC") or Path(__file__).resolve().parent.parent)
STATE = Path(os.environ.get("CCDESK_BENCH_STATE") or "/tmp/ccdesk-bench-state.json")
PY = os.environ.get("CCDESK_A11Y_PYTHON", "/usr/bin/python3")


def main():
    from ccdesk.backends import X11Backend
    from ccdesk.desktop import Desktop
    from ccdesk.safety import AuditLog, Policy

    STATE.unlink(missing_ok=True)
    env = dict(os.environ, GNOME_ACCESSIBILITY="1", GDK_BACKEND="x11")
    app = subprocess.Popen([PY, str(ROOT / "tests/gtk_app.py"), str(STATE)], env=env)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if json.loads(STATE.read_text()).get("ready"):
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        desk = Desktop(backend=X11Backend(env=env), policy=Policy(),
                       audit=AuditLog(STATE.with_name("audit.log")),
                       shots_dir=STATE.with_name("shots"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any("ccdesk-gtk" in w.title for w in desk.backend.list_windows()):
                break
            time.sleep(0.05)

        tree = desk.ui_tree(window="ccdesk-gtk")
        print(f"tree: {len(tree['tree'].splitlines())} lines, ~{tree['approx_tokens']} tokens")
        button = re.search(r'^\s*(e\d+) (?:push )?button "Apply settings"', tree["tree"], re.M).group(1)
        field = re.search(r'^\s*(e\d+) (?:text|entry) "Display name"', tree["tree"], re.M).group(1)
        desk.click_element(field)  # focus the field for type_text

        def timed(label, fn, n=5):
            times = []
            for _ in range(n):
                start = time.perf_counter()
                fn()
                times.append((time.perf_counter() - start) * 1000)
            print(f"  {label:22} median {statistics.median(times):7.1f} ms   {[round(t) for t in times]}")

        timed("ui_tree", lambda: desk.ui_tree(window="ccdesk-gtk"))
        timed("click_element", lambda: desk.click_element(button))
        timed("type_text", lambda: desk.type_text("abc"))
        timed("set_value", lambda: desk.set_value(field, "Sara"))
        if hasattr(desk.a11y, "close"):  # older checkouts have no persistent helper
            desk.a11y.close()
    finally:
        app.terminate()
        app.wait(5)


if __name__ == "__main__":
    main()
