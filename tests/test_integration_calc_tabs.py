"""A real LibreOffice Calc sheet tab, on a private Xvfb and accessibility bus.

Calc's sheet tabs have no AT-SPI action: click_element selects them through the Selection of
their tab list, with no pointer. Run with `xvfb-run -a uv run pytest tests/test_integration_calc_tabs.py`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from ccdesk.a11y import A11y
from ccdesk.backends import X11Backend
from ccdesk.desktop import Desktop
from ccdesk.errors import CcdeskError
from ccdesk.headless import Headless, find_libexec
from ccdesk.safety import AuditLog, Policy

TWO_SHEETS = """<?xml version="1.0" encoding="UTF-8"?>
<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.2"
 office:mimetype="application/vnd.oasis.opendocument.spreadsheet">
 <office:body><office:spreadsheet>
  <table:table table:name="Ledger"><table:table-row><table:table-cell><text:p>one</text:p></table:table-cell></table:table-row></table:table>
  <table:table table:name="Controls"><table:table-row><table:table-cell><text:p>two</text:p></table:table-cell></table:table-row></table:table>
 </office:spreadsheet></office:body>
</office:document>
"""


def skip_reason():
    for tool in ("soffice", "Xvfb", "dbus-daemon", "xdotool"):
        if not shutil.which(tool):
            return f"{tool} absent"
    if not find_libexec("at-spi2-registryd"):
        return "AT-SPI registry absent"
    if subprocess.run(["/usr/bin/python3", "-c", "import gi; gi.require_version('Atspi', '2.0')"],
                      capture_output=True).returncode:
        return "system PyGObject with Atspi absent"
    return None


pytestmark = [pytest.mark.display, pytest.mark.skipif(skip_reason() is not None, reason=skip_reason() or "")]


def test_click_element_selects_a_calc_sheet_tab_without_the_pointer(tmp_path):
    doc = tmp_path / "two.fods"
    doc.write_text(TWO_SHEETS)
    screen = Headless(size="1400x900", wm="auto")
    env = screen.start()
    app = subprocess.Popen(["soffice", "--calc", "--norestore", "--nologo", "--nofirststartwizard",
                            f"-env:UserInstallation=file://{tmp_path}/lo", str(doc)],
                           env=dict(env, SAL_USE_VCLPLUGIN="gtk3"),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        a11y = A11y()
        a11y.env = env
        desk = Desktop(backend=X11Backend(env=env), policy=Policy(), audit=AuditLog(tmp_path / "audit.log"),
                       shots_dir=tmp_path / "shots", a11y=a11y, screen_diff=False)
        pointer = []
        desk.backend.click = lambda *a: pointer.append(a)  # any pointer click fails the test

        def tabs():
            try:
                # Offscreen controls share the node cap. Search for the tabs instead of
                # assuming they fit after every menu and toolbar in the default tree.
                tree = "\n".join(desk.ui_tree(window="two.fods", query=name)["tree"]
                                 for name in ("Ledger", "Controls"))
            except CcdeskError:
                return {}
            out = {}
            for line in tree.splitlines():
                if " page tab " in f" {line.strip()} ":
                    ref, _, rest = line.strip().partition(" ")
                    out[rest.split('"')[1]] = (ref, "[selected" in rest)
            return out

        deadline = time.monotonic() + 90
        found = tabs()
        while "Controls" not in found and time.monotonic() < deadline:
            time.sleep(1)
            found = tabs()
        assert "Controls" in found, "Calc's sheet tabs never reached the accessibility bus"
        assert found["Ledger"][1] and not found["Controls"][1]

        r = desk.click_element(found["Controls"][0])
        assert r["selected"] == "page tab 'Controls'" and r["via"] == "accessibility"
        assert pointer == []
        after = tabs()
        assert after["Controls"][1] and not after["Ledger"][1]
        assert "Sheet Controls" in desk.ui_tree(window="two.fods", query="Sheet")["tree"]
    finally:
        app.terminate()
        try:
            app.wait(timeout=15)
        except subprocess.TimeoutExpired:
            app.kill()
        screen.stop()
