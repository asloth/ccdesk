#!/usr/bin/env python3
"""Record the README demo GIF: a real ccdesk session on a private screen.

Starts a headless Xvfb screen, launches the GTK signup form from `bench/form_app.py`,
records the screen with ffmpeg while clicking and typing through `ccdesk.desktop.Desktop`,
and writes `docs/demo.gif` cropped to the window.

    .venv/bin/python bench/record_demo.py

Needs: Xvfb, ffmpeg, ImageMagick, a system Python with PyGObject for the form app
(`/usr/bin/python3`, package `python3-gi`), and `gir1.2-gtk-3.0`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ccdesk.desktop import Desktop  # noqa: E402
from ccdesk.headless import Headless  # noqa: E402

SIZE = "1280x800"
CROP = "640:580:380:120"          # the form window plus its shadow
OUT = ROOT / "docs" / "demo.gif"
CAPTIONS = [
    (0.0, 2.6, "ccdesk drives a real GTK app on a private screen"),
    (2.6, 5.4, "clicks and keystrokes, no pixel math per step"),
    (5.4, 9.0, "form filled and submitted in one turn"),
]
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def esc(text: str) -> str:
    return text.replace(":", r"\:").replace("'", r"\'")


def main() -> int:
    if not shutil.which("ffmpeg"):
        print("ffmpeg missing", file=sys.stderr)
        return 2
    system_python = os.environ.get("CCDESK_DEMO_PYTHON", "/usr/bin/python3")

    headless = Headless(size=SIZE)
    env = headless.start()
    print(f"private display {headless.display}")
    os.environ.update({k: v for k, v in env.items() if isinstance(v, str)})

    state = Path(tempfile.mkstemp(suffix=".json")[1])
    app = subprocess.Popen([system_python, "bench/form_app.py", str(state)],
                           cwd=ROOT, env=env)
    time.sleep(4)

    desktop = Desktop(headless=headless)
    shot = desktop.screenshot()
    print(f"form up: {shot['width']}x{shot['height']}, {shot['approx_tokens']} tokens per look")

    raw = Path(tempfile.mkstemp(suffix=".mkv")[1])
    ffmpeg = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "x11grab", "-framerate", "12",
         "-video_size", SIZE, "-i", f"{headless.display}.0", "-t", "16", str(raw)],
        env=dict(os.environ, DISPLAY=headless.display or ""),
    )
    time.sleep(1.2)

    # Coordinates come from a screenshot of this form (see README).
    try:
        desktop.click(727, 234)
        desktop.type_text("Sara Benel", replace=True)
        desktop.click(727, 279)
        desktop.type_text("sara@example.com", replace=True)
        desktop.click(593, 447)          # Pro
        desktop.click(525, 508)          # accept the terms
        time.sleep(0.4)
        desktop.click(904, 542)          # Submit
        time.sleep(1.8)
        submitted = json.loads(state.read_text()) if state.exists() else {}
        print("submitted:", submitted)
    finally:
        ffmpeg.terminate()
        ffmpeg.wait(timeout=10)
        app.terminate()
        headless.stop()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    filters = [f"crop={CROP}", "scale=720:-1:flags=lanczos", "fps=12"]
    if Path(FONT).exists():
        for start, end, text in CAPTIONS:
            filters.append(
                f"drawtext=fontfile={FONT}:text='{esc(text)}':fontcolor=white:fontsize=17:"
                f"box=1:boxcolor=black@0.72:boxborderw=9:x=18:y=h-58:"
                f"enable='between(t,{start},{end})'"
            )
    graph = ",".join(filters) + ",split[a][b];[a]palettegen=max_colors=160[p];[b][p]paletteuse=dither=bayer:bayer_scale=3"
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
                        "-vf", graph, "-loop", "0", str(OUT)], capture_output=True, text=True)
    if r.returncode != 0:
        print("gif failed:", r.stderr[-600:], file=sys.stderr)
        return 1
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
