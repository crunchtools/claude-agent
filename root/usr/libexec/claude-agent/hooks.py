#!/usr/bin/env python3
"""Claude Code hooks for an agent: the audit record and the circuit breakers.

    hooks.py audit     PostToolUse. One JSON line per tool call.
    hooks.py breaker   PreToolUse. Denies a conversation that has run away.
    hooks.py event     The turn's events, handed to the supervisor that
                       started this session (CLAUDE_AGENT_SOCKET), if any.

Both read the hook event on stdin, and both do nothing unless CLAUDE_AGENT=1
is set: managed settings reach every Claude Code session on the machine, and
only an agent's sessions are meant to be counted and logged. The supervisor
sets it for the sessions it starts; an agent image sets it for everything.

Where they write: CLAUDE_AGENT_AUDIT_DIR (default
$XDG_STATE_HOME/claude-agent/audit), kept CLAUDE_AGENT_AUDIT_RETENTION_DAYS
(default 120), and CLAUDE_AGENT_BREAKER_DIR (default
$XDG_RUNTIME_DIR/claude-agent/breaker, or a per-user directory in /tmp),
whose files go after a week without a call.

They fail in opposite directions on purpose. An audit hook that cannot write
exits non-zero, which Claude Code reports and carries on from: losing a log
line must not stop the work. A breaker that cannot count denies the call: a
breaker that fails open is not a breaker.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import socket
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_VALUE_CHARS = 200
DEFAULT_MAX_TOOL_CALLS = 500
DEFAULT_MAX_REPEAT = 5
DEFAULT_AUDIT_RETENTION_DAYS = 120
BREAKER_STATE_DAYS = 7

SECRET_KEY = re.compile(r"token|secret|passw|api[_-]?key|authorization|credential|cookie", re.I)
SECRET_VALUE = re.compile(
    r"(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}"
    r"|\b(gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})\b"
)
# A secret inside a string: a header (X-API-Key: v) or an assignment
# (TOKEN=v, --password v) whose name says what it is.
SECRET_FIELD = re.compile(
    r"((?:[\w-]*(?:token|secret|passw|api[_-]?key|credential|cookie)[\w-]*)\s*[:=]\s*['\"]?|"
    r"--?[\w-]*(?:token|secret|passw|api[_-]?key)[\w-]*[\s=]+['\"]?)[^\s'\"]+",
    re.I,
)


def enabled() -> bool:
    return os.environ.get("CLAUDE_AGENT") == "1"


def audit_dir() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(os.environ.get("CLAUDE_AGENT_AUDIT_DIR") or Path(state) / "claude-agent/audit")


def breaker_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    default = (
        Path(runtime) / "claude-agent"
        if runtime
        else Path(tempfile.gettempdir()) / f"claude-agent-{os.getuid()}"
    )
    return Path(os.environ.get("CLAUDE_AGENT_BREAKER_DIR") or default / "breaker")


def redact(value: Any, key: str = "") -> Any:
    """Return ``value`` safe to write down: secrets replaced, long text cut.

    The log outlives the session and is read by people who should not learn
    a token from it.
    """
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if not isinstance(value, str):
        return value
    if SECRET_KEY.search(key):
        return "[REDACTED]"
    text = SECRET_VALUE.sub(lambda m: (m.group(1) or "") + "[REDACTED]", value)
    text = SECRET_FIELD.sub(lambda m: m.group(1) + "[REDACTED]", text)
    if len(text) > MAX_VALUE_CHARS:
        return f"{text[:MAX_VALUE_CHARS]}... [{len(text)} chars]"
    return text


def split_tool(name: str) -> tuple[str, str]:
    """Split ``mcp__server__tool`` into (server, tool); built-ins have no server."""
    if not name.startswith("mcp__"):
        return "claude-code", name
    server, _, tool = name[len("mcp__"):].partition("__")
    return server, tool or name


def failed(response: Any) -> bool:
    return isinstance(response, dict) and bool(
        response.get("is_error") or response.get("isError") or "error" in response
    )


def build_record(event: dict[str, Any], outcome: str) -> dict[str, Any]:
    """The record the Autonomous Agent profile asks for (IV, audit logging)."""
    server, tool = split_tool(str(event.get("tool_name", "")))
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": tool,
        "server": server,
        "args": redact(event.get("tool_input", {})),
        "result_summary": outcome,
        "token_cost": None,
        "conversation_id": event.get("session_id", ""),
    }


def prune(directory: Path, pattern: str, days: float) -> None:
    """Delete files matching ``pattern`` not written for ``days``."""
    cutoff = time.time() - days * 86400
    for path in directory.glob(pattern):
        try:
            stale = path.stat().st_mtime < cutoff
        except FileNotFoundError:
            stale = False  # another session's prune got there first
        if stale:
            path.unlink(missing_ok=True)


def append(record: dict[str, Any]) -> None:
    directory = audit_dir()
    directory.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
    path = directory / f"audit-{record['timestamp'][:10]}.jsonl"
    if not path.exists():
        # Once a day, when the day's file starts, the oldest ones go.
        days = limit("CLAUDE_AGENT_AUDIT_RETENTION_DAYS", DEFAULT_AUDIT_RETENTION_DAYS)
        prune(directory, "audit-*.jsonl", days)
    # O_APPEND keeps concurrent sessions' lines whole.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


def audit(event: dict[str, Any]) -> None:
    outcome = "error" if failed(event.get("tool_response")) else "success"
    append(build_record(event, outcome))


def limit(name: str, default: int) -> int:
    return max(1, int(os.environ.get(name, default)))


def fingerprint(event: dict[str, Any]) -> str:
    body = json.dumps([event.get("tool_name"), event.get("tool_input")], sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()


def count(state: dict[str, Any], call: str, max_calls: int, max_repeat: int) -> str | None:
    """Count one call into ``state``; return why it is refused, or None.

    Once tripped, a conversation stays tripped: the reason is stored and
    returned for every later call.
    """
    if state.get("tripped"):
        return str(state["tripped"])
    state["calls"] = state.get("calls", 0) + 1
    state["repeat"] = state.get("repeat", 0) + 1 if state.get("last") == call else 1
    state["last"] = call
    if state["calls"] > max_calls:
        state["tripped"] = f"more than {max_calls} tool calls in one conversation"
    elif state["repeat"] > max_repeat:
        state["tripped"] = f"the same call {state['repeat']} times in a row"
    return state.get("tripped")


def breaker(event: dict[str, Any]) -> str | None:
    """Apply the breakers to one call. State is a small file per conversation."""
    session = "".join(c for c in str(event.get("session_id", "")) if c.isalnum() or c in "-_") or "unknown"
    state_dir = breaker_dir()
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_file = state_dir / f"{session}.json"
    if not state_file.exists():
        prune(state_dir, "*.json", BREAKER_STATE_DAYS)
    with open(state_file, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        raw = handle.read()
        state = json.loads(raw) if raw else {}
        already = bool(state.get("tripped"))
        reason = count(
            state,
            fingerprint(event),
            limit("CLAUDE_AGENT_MAX_TOOL_CALLS", DEFAULT_MAX_TOOL_CALLS),
            limit("CLAUDE_AGENT_MAX_REPEAT", DEFAULT_MAX_REPEAT),
        )
        handle.seek(0)
        handle.truncate()
        json.dump(state, handle)
    if reason and not already:
        append(build_record(event, f"breaker_tripped: {reason}"))
    return reason


# What the supervisor needs from each event; a tool's input only for the tools
# that open a dialog, whose question is texted to the operator.
EVENT_FIELDS = ("hook_event_name", "session_id", "transcript_path", "tool_name", "source",
                "last_assistant_message", "error", "message", "notification_type")
DIALOG_TOOLS = ("AskUserQuestion", "ExitPlanMode")


def forward(event: dict[str, Any]) -> None:
    """Hand one hook event to the supervisor. A session it did not start has no socket."""
    path = os.environ.get("CLAUDE_AGENT_SOCKET")
    if not path:
        return
    message = {key: event[key] for key in EVENT_FIELDS if key in event}
    if event.get("tool_name") in DIALOG_TOOLS:
        message["tool_input"] = event.get("tool_input", {})
    message["op"] = "event"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(2)
        conn.connect(path)
        conn.sendall(json.dumps(message, default=str).encode() + b"\n")


def deny(reason: str) -> None:
    decision = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"Circuit breaker: {reason}. This conversation is halted. "
            "Tell the operator what you were doing; do not retry."
        ),
    }
    json.dump({"hookSpecificOutput": decision}, sys.stdout)


def main(argv: list[str]) -> int:
    mode = argv[1:]
    if mode not in (["audit"], ["breaker"], ["event"]):
        print("usage: hooks.py audit|breaker|event  (hook event on stdin)", file=sys.stderr)
        return 1
    if not enabled():
        return 0
    if mode == ["audit"]:
        audit(json.load(sys.stdin))
        return 0
    if mode == ["event"]:
        try:
            forward(json.load(sys.stdin))
        except (OSError, ValueError) as exc:
            # Exit 1 is reported and the turn goes on; the supervisor missed one event.
            print(f"hooks.py event: {exc!r}", file=sys.stderr)
            return 1
        return 0
    try:
        reason = breaker(json.load(sys.stdin))
    except Exception as exc:
        print(f"hooks.py breaker: state unavailable: {exc!r}", file=sys.stderr)
        reason = f"breaker state unavailable ({type(exc).__name__})"
    if reason:
        deny(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
