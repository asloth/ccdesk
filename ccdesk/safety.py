"""Safety policy and the JSONL audit log."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ccdesk.errors import PolicyError
from ccdesk.parsing import KeyCombo, is_destructive

# Tools that change desktop state. --read-only disables all of them.
INPUT_TOOLS = frozenset(
    {"click", "click_element", "move", "drag", "scroll", "type_text", "press_key", "activate_window", "manage_window", "launch", "clipboard_set"}
)

MAX_LOGGED_TEXT = 200


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "ccdesk"


def default_audit_path() -> Path:
    override = os.environ.get("CCDESK_AUDIT_LOG")
    return Path(override).expanduser() if override else cache_dir() / "audit.log"


@dataclass
class Policy:
    read_only: bool = False
    allow_launch: bool = False
    confirm_keys: tuple[KeyCombo, ...] = field(default_factory=tuple)
    yield_ms: int = 0  # hold input until the user has been idle this long (0: never wait)

    def check(self, tool: str) -> None:
        if self.read_only and tool in INPUT_TOOLS:
            raise PolicyError(f"{tool} is disabled: ccdesk was started with --read-only")
        if tool == "launch" and not self.allow_launch:
            raise PolicyError("launch is disabled: restart the server with `ccdesk mcp --allow-launch`")

    def check_key(self, combo: KeyCombo, confirm: bool) -> None:
        if is_destructive(combo, self.confirm_keys) and not confirm:
            raise PolicyError(
                f"{combo} can close windows, quit applications or end the session; "
                "take a screenshot to verify the target, then call again with confirm=true"
            )


def _redact(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        if k == "text" and isinstance(v, str):
            # Clipboard and typed text can carry secrets; keep only a short preview.
            out[k] = v if len(v) <= MAX_LOGGED_TEXT else v[:MAX_LOGGED_TEXT] + "…"
            out["text_len"] = len(v)
        else:
            out[k] = v
    return out


class AuditLog:
    """Append-only JSONL log. Failures to write never break the action itself."""

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path).expanduser() if path else default_audit_path()
        self._lock = threading.Lock()

    def record(self, tool: str, args: dict, status: str, result=None, error: str | None = None,
               duration_ms: float | None = None) -> dict:
        entry = {
            "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "pid": os.getpid(),
            "tool": tool,
            "args": _redact(args),
            "status": status,
        }
        if result is not None:
            entry["result"] = result
        if error:
            entry["error"] = error
        if duration_ms is not None:
            entry["duration_ms"] = round(duration_ms, 1)
        line = json.dumps(entry, ensure_ascii=False, default=str)
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError:
            pass
        return entry
