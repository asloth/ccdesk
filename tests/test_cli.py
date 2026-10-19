"""CLI, install and the "no third-party imports" rule."""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import ccdesk
from ccdesk.cli import main

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "ccdesk"


def run_cli(*args, env=None):
    return subprocess.run([sys.executable, "-m", "ccdesk", *args], capture_output=True, text=True,
                          env=env, cwd=ROOT, timeout=60)


def test_version(capsys):
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == f"ccdesk {ccdesk.__version__}"
    assert run_cli("version").stdout.strip() == f"ccdesk {ccdesk.__version__}"


def test_help_lists_commands():
    out = run_cli("--help").stdout
    for cmd in ("doctor", "install", "mcp", "version"):
        assert cmd in out


def test_doctor_without_display(monkeypatch, capsys):
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "CCDESK_BACKEND"):
        monkeypatch.delenv(var, raising=False)
    assert main(["doctor", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["session"]["detected"] == "none"
    assert report["ready"] is False
    assert any("no graphical session" in w for w in report["warnings"])


def test_doctor_text_report(monkeypatch, capsys):
    monkeypatch.setenv("CCDESK_BACKEND", "x11")
    main(["doctor"])
    out = capsys.readouterr().out
    for heading in ("Session", "Backend: x11", "Tools", "screen:", "scale:", "cua-driver:", "audit log:"):
        assert heading in out


def test_doctor_detects_cua_driver(tmp_path, monkeypatch, capsys):
    fake = tmp_path / "cua-driver"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    main(["doctor", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["cua_driver"] == str(fake)
    assert any("does not use it" in w for w in report["warnings"])


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return h


def fake_claude(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "claude.log"
    script = bindir / "claude"
    script.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return log


def test_install_dry_run_writes_nothing(home, tmp_path, monkeypatch, capsys):
    log = fake_claude(tmp_path, monkeypatch)
    assert main(["install", "--dry-run", "--read-only"]) == 0
    out = capsys.readouterr().out
    target = home / ".claude" / "skills" / "computer-use" / "SKILL.md"
    assert f"would write {target}" in out
    assert "would run: claude mcp add --scope user ccdesk -- " in out
    assert out.rstrip().endswith("mcp --read-only")
    assert "claude mcp remove --scope user ccdesk" in out
    assert not target.exists()
    assert not (home / ".claude").exists()
    assert not log.exists()


def test_install_dry_run_without_claude(home, monkeypatch, capsys):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert main(["install", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "`claude` is not on PATH" in out
    assert "claude mcp add --scope user ccdesk --" in out


def test_install_for_real(home, tmp_path, monkeypatch, capsys):
    log = fake_claude(tmp_path, monkeypatch)
    assert main(["install", "--allow-launch"]) == 0
    target = home / ".claude" / "skills" / "computer-use" / "SKILL.md"
    assert target.read_bytes() == (ROOT / "skills" / "computer-use" / "SKILL.md").read_bytes()
    calls = log.read_text().splitlines()
    assert calls[0] == "mcp remove --scope user ccdesk"
    assert calls[1].startswith("mcp add --scope user ccdesk -- ")
    assert calls[1].endswith("mcp --allow-launch")


def test_install_respects_claude_config_dir(tmp_path, monkeypatch, capsys):
    fake_claude(tmp_path, monkeypatch)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    main(["install", "--dry-run"])
    assert str(tmp_path / "cfg" / "skills" / "computer-use" / "SKILL.md") in capsys.readouterr().out


def test_skill_frontmatter():
    text = (ROOT / "skills" / "computer-use" / "SKILL.md").read_text()
    assert text.startswith("---\nname: computer-use\ndescription: ")
    for must in ("screenshot", "verify", "Firefox", "password", "dialog", "confirm=true"):
        assert must.lower() in text.lower()


def _imported_modules(path: Path):
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0]


def test_only_stdlib_and_mcp_are_imported():
    allowed = set(sys.stdlib_module_names) | {"ccdesk", "__future__"}
    offenders = {}
    for path in PKG.rglob("*.py"):
        mods = set(_imported_modules(path)) - allowed
        if path.name == "server.py":
            mods -= {"mcp"}
        if mods:
            offenders[str(path.relative_to(ROOT))] = sorted(mods)
    assert offenders == {}


def test_core_imports_without_mcp():
    code = (
        "import sys; sys.modules['mcp'] = None\n"
        "import ccdesk.cli, ccdesk.desktop, ccdesk.doctor, ccdesk.install, ccdesk.backends\n"
        "print('ok')"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT)
    assert proc.stdout.strip() == "ok", proc.stderr
