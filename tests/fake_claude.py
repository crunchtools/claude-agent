#!/usr/bin/env python3
"""A stand-in for interactive Claude Code, for the supervisor's tests.

It reads raw keys from its terminal the way the TUI does, and runs the real
``hooks.py event`` for each hook Claude Code would fire. What it does with a
prompt is chosen by the prompt's text:

    /effort X      asks to confirm, then records the command in its transcript
    /clear         starts a new session and transcript
    /<other> ...   records the command and its output
    slow N         a turn of N seconds with tool calls (heartbeats)
    ask            opens a question dialog and waits for Escape
    exit           exits 3
    anything else  replies "echo: <prompt>"

FAKE_CLAUDE_DIR is where its transcripts go. FAKE_CLAUDE_UNTRUSTED=1 makes it
stop at the working directory's trust question, and --permission-mode
bypassPermissions shows the warning whose default is "No, exit".
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
import tty
import uuid
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "root/usr/libexec/claude-agent/hooks.py"
PASTE_START, PASTE_END = "\x1b[200~", "\x1b[201~"
DIR = Path(os.environ["FAKE_CLAUDE_DIR"])


class Fake:
    def __init__(self) -> None:
        self.session = ""
        self.transcript = Path()

    def hook(self, name: str, **fields: object) -> None:
        event = {"hook_event_name": name, "session_id": self.session,
                 "transcript_path": str(self.transcript), **fields}
        subprocess.run([sys.executable, str(HOOKS), "event"], input=json.dumps(event), text=True, check=False)

    def new_session(self, source: str) -> None:
        self.session = uuid.uuid4().hex
        self.transcript = DIR / f"{self.session}.jsonl"
        self.transcript.touch()
        self.hook("SessionStart", source=source)

    def record(self, *records: dict) -> None:
        with self.transcript.open("a") as out:
            for record in records:
                out.write(json.dumps(record) + "\n")

    def command(self, line: str) -> None:
        name, _, args = line[1:].partition(" ")
        if name == "clear":
            self.new_session("clear")
        if name == "effort":
            self.say("Change effort level? ❯ 1. Yes, switch   2. No   Enter to confirm")
            while read_key() != "\r":
                pass
        output = {"effort": f"Set effort level to {args}", "fast": f"Fast mode {args.upper()}"}.get(name, "")
        self.record({"type": "user", "message": {"content": f"<command-name>/{name}</command-name>"}},
                    {"type": "user", "message": {"content":
                        f"<local-command-stdout>{output}</local-command-stdout>"}})

    def prompt(self, text: str) -> None:
        self.hook("UserPromptSubmit", prompt=text)
        if text.startswith("slow "):
            for _ in range(int(text.split()[1])):
                self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "sleep 1"})
                time.sleep(1)
                self.hook("PostToolUse", tool_name="Bash", tool_input={"command": "sleep 1"})
                if key_waiting() == "\x1b":
                    self.say("Interrupted")
                    return
            self.hook("Stop", last_assistant_message="done")
        elif text == "ask":
            self.hook("PreToolUse", tool_name="AskUserQuestion",
                      tool_input={"questions": [{"question": "Which?", "options": [{"label": "A"}]}]})
            self.hook("Notification", notification_type="permission_prompt",
                      message="Claude needs your permission")
            while read_key() != "\x1b":
                pass
            self.say("Interrupted")
        else:
            self.hook("Stop", last_assistant_message=f"echo: {text}")

    def say(self, text: str) -> None:
        sys.stdout.write(text + "\r\n")
        sys.stdout.flush()

    def startup(self) -> bool:
        """The startup screens; False when the session should exit."""
        if os.environ.get("FAKE_CLAUDE_UNTRUSTED"):
            self.say("Do you trust the files in this folder?  1. No, exit  2. Yes, proceed")
            while True:
                read_key()
        if "bypassPermissions" in sys.argv or os.environ.get("FAKE_CLAUDE_BYPASS_WARNING"):
            self.say("WARNING: Claude Code running in Bypass Permissions mode  1. No, exit  2. Yes, I accept")
            keys = ""
            while not keys.endswith("\r"):
                keys += read_key()
            return "\x1b[B" in keys
        return True

    def run(self) -> int:
        return self.serve() if self.startup() else 1

    def serve(self) -> int:
        self.new_session("startup")
        self.say("Welcome to Fake Claude")
        buffer = ""
        while True:
            key = read_key()
            if key == "\r":
                # Paste markers and a stray Escape (an interrupt while idle) are not text.
                text = buffer.replace(PASTE_START, "").replace(PASTE_END, "")
                text = text.replace("\x1b", "").strip()
                buffer = ""
                if text == "exit":
                    return 3
                if text.startswith("/"):
                    self.command(text)
                elif text:
                    self.prompt(text)
            else:
                buffer += key


# Keys read ahead while checking for an interrupt, handed back in order.
PENDING: list[str] = []
READ_AHEAD = 4096


def read_key() -> str:
    return PENDING.pop(0) if PENDING else os.read(0, 1).decode(errors="replace")


def key_waiting() -> str:
    """An Escape pressed on its own, as the TUI tells it from a paste's \\x1b[."""
    if select.select([0], [], [], 0)[0]:
        PENDING.extend(os.read(0, READ_AHEAD).decode(errors="replace"))
    if PENDING[:1] == ["\x1b"] and PENDING[1:2] != ["["]:
        return PENDING.pop(0)
    return ""


if __name__ == "__main__":
    tty.setraw(0)
    sys.exit(Fake().run())
