#!/usr/bin/env python3
"""Hold an interactive Claude Code session in a pty and drive it from a socket.

    supervisor.py [claude arguments...]

Claude Code runs in a pseudo-terminal nobody has to watch. Everything else
talks to this process over a unix socket (CLAUDE_AGENT_SOCKET, under
CLAUDE_AGENT_RUNTIME_DIR), one JSON object per line:

    {"op": "event", ...}            a hook event, from hooks.py event
    {"op": "submit", "text": ...}   a prompt; held until the session is idle
    {"op": "command", "line": ...}  an allowlisted built-in such as /compact
    {"op": "stop", "text": ...}     interrupt; then submit text, if any
    {"op": "status"}                state, held texts, session
    {"op": "subscribe"}             a stream of {"event": ...} lines
    {"op": "attach", "rw": false}   the raw terminal, both ways if rw

The session's state comes from its hooks, never from the screen: idle after
SessionStart, Stop or StopFailure, busy from UserPromptSubmit. Texts that
arrive while it is busy are held and pasted together, as one prompt, when it
is idle again: a paste mid-turn would be folded into the running turn, and
one into an open dialog would answer it. The screen is read in two places
only, both narrow: the startup warnings, and the confirmation a built-in
command may open.

Exits with Claude Code's status.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import re
import selectors
import signal
import socket
import struct
import sys
import tempfile
import termios
import time
from pathlib import Path
from typing import Callable

ROWS = 50
COLUMNS = 160
READ_CHUNK = 65536
PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"
ENTER, ESCAPE, DOWN = b"\r", b"\x1b", b"\x1b[B"
SETTLE_SECONDS = 1.0
SUBMIT_DELAY_SECONDS = 0.3
CONFIRM_WINDOW_SECONDS = 3.0
STOP_SETTLE_SECONDS = 1.0
RESULT_TIMEOUT_SECONDS = 900
OUTPUT_CHARS = 1000
SUMMARY_CHARS = 3000
SCREEN_CHARS = 4000
MAX_TEXT_CHARS = 20000
# What may wait for a turn to end, in texts and in characters.
MAX_HELD_TEXTS = 20
MAX_HELD_CHARS = 40000
MAX_COMMAND_CHARS = 520
# sun_path is 108 bytes on Linux, its terminating NUL included.
MAX_SOCKET_PATH = 107
# Built-ins that may be typed: name -> arguments pattern. The bridge checks the
# same table and answers usage errors; this is the last check before the keys.
# /compact runs for as long as the summary takes; the others are done at once.
COMPACT_WAIT_SECONDS = 600
COMMANDS = {
    "clear": r".{0,500}",
    "compact": r".{0,500}",
    "model": r"[\w.\[\]-]{1,100}",
    "effort": r"low|medium|high|xhigh|max|auto",
    "fast": r"on|off",
    "goal": r".{1,500}",
    "mcp": r"reconnect [\w.-]{1,100}",
    "reload-skills": r"",
    "reload-plugins": r"",
}
# Tools that open a dialog for the person. A text that arrives while one is
# open is its answer: Escape closes the dialog and the text follows.
DIALOG_TOOLS = {"AskUserQuestion", "ExitPlanMode"}
# Variables a parent Claude Code session sets for its children. Inherited, they
# turn off the transcript and confuse the session about what it is.
PARENT_MARKERS = (
    "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED",
)
ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b[=>]")
CONFIRM = re.compile(r"Enter to confirm|❯\s*1\.")
OUTPUT = re.compile(r"<local-command-(stdout|stderr)>(.*?)</local-command-\1>", re.S)


def log(message: str) -> None:
    print(f"claude-agent: {message}", file=sys.stderr, flush=True)


def socket_path() -> Path:
    """CLAUDE_AGENT_SOCKET, else control.sock in the runtime directory."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    default = (
        Path(runtime) / "claude-agent"
        if runtime
        else Path(tempfile.gettempdir()) / f"claude-agent-{os.getuid()}"
    )
    directory = Path(os.environ.get("CLAUDE_AGENT_RUNTIME_DIR") or default)
    return Path(os.environ.get("CLAUDE_AGENT_SOCKET") or directory / "control.sock")


def screen_text(raw: bytes) -> str:
    """Output with the escape sequences taken out; the TUI draws gaps as cursor moves."""
    return ANSI.sub(b" ", raw).decode(errors="replace")


def allowed_command(line: str) -> bool:
    if len(line) > MAX_COMMAND_CHARS or not line.startswith("/") or not line.isprintable():
        return False
    name, _, args = line[1:].partition(" ")
    return name in COMMANDS and re.fullmatch(COMMANDS[name], args) is not None


def answer(name: str, output: str, compacted: dict | None, summary: str | None) -> list[str]:
    """The texts that report a built-in command's output."""
    output = screen_text(output.encode()).strip()
    if name == "compact" and compacted is not None:
        texts = [f"Compacted {compacted.get('preTokens', 0):,} → {compacted.get('postTokens', 0):,} tokens"
                 f" in {round(compacted.get('durationMs', 0) / 1000)}s."]
        body = (summary or "").partition("Summary:")[2].strip() or (summary or "")
        if len(body) > SUMMARY_CHARS:
            body = body[:SUMMARY_CHARS].rstrip() + f"… (trimmed, {len(body):,} chars)"
        return texts + [body] if body.strip() else texts
    if len(output) > OUTPUT_CHARS:
        output = output[:OUTPUT_CHARS].rstrip() + "…"
    return [f"/{name}: {output or 'done'}"]


class Results:
    """Finds each typed command's output in the session transcript.

    Claude Code records every built-in and what it printed in the transcript
    it names in SessionStart; /compact also leaves its token counts and
    summary. /clear starts a new session, whose transcript is read from its
    start.
    """

    def __init__(self, emit: Callable[[dict], None], timeout: float = RESULT_TIMEOUT_SECONDS) -> None:
        self.emit = emit
        self.timeout = timeout
        self.path: Path | None = None
        self.offset = 0
        self.pending: list[dict] = []

    def transcript(self, path: str) -> None:
        if path and Path(path) != self.path:
            self.path = Path(path)
            self.offset = 0 if self.pending else self.size()

    def size(self) -> int:
        try:
            return self.path.stat().st_size if self.path else 0
        except OSError:
            return 0

    def expect(self, name: str) -> None:
        if not self.pending:
            self.offset = self.size()
        self.pending.append({"name": name, "deadline": time.monotonic() + self.timeout,
                             "seen": False, "compacted": None, "summary": None})

    def records(self) -> list[dict]:
        if not self.path:
            return []
        try:
            with self.path.open("rb") as transcript:
                transcript.seek(self.offset)
                appended = transcript.read()
        except OSError:
            return []
        appended = appended[:appended.rfind(b"\n") + 1]
        self.offset += len(appended)
        found = []
        for line in appended.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                found.append(record)
        return found

    def poll(self) -> None:
        if not self.pending:
            return
        for record in self.records():
            self.consider(record)
        now = time.monotonic()
        for item in [p for p in self.pending if p["deadline"] < now]:
            self.pending.remove(item)
            minutes = max(1, round(self.timeout / 60))
            self.emit({"event": "result", "command": item["name"],
                       "texts": [f"No result from /{item['name']} within {minutes} min."]})

    def consider(self, record: dict) -> None:
        message = record.get("message")
        text = message.get("content") if isinstance(message, dict) else record.get("content")
        text = text if isinstance(text, str) else ""
        compact = next((p for p in self.pending if p["name"] == "compact"), None)
        if record.get("isCompactSummary") and compact:
            compact["summary"] = text
            return
        if record.get("subtype") == "compact_boundary" and compact:
            compact["compacted"] = record.get("compactMetadata") or {}
            return
        run = record.get("commandRun") or {}
        for item in self.pending:
            if not item["seen"] and (run.get("command") == item["name"]
                                     or f"<command-name>/{item['name']}</command-name>" in text):
                item["seen"] = True
                break
        output = OUTPUT.search(text)
        item = next((p for p in self.pending if p["seen"]), None)
        if output and item:
            self.pending.remove(item)
            texts = answer(item["name"], output[2], item["compacted"], item["summary"])
            self.emit({"event": "result", "command": item["name"], "texts": texts})


class Supervisor:
    def __init__(self, argv: list[str], sock: Path) -> None:
        self.argv = argv
        self.sock = sock
        self.selector = selectors.DefaultSelector()
        self.state = "starting"
        self.held: list[str] = []
        self.commands: list[str] = []
        self.dialog: str | None = None
        self.session = ""
        self.subscribers: list[socket.socket] = []
        self.attached: dict[socket.socket, bool] = {}
        self.timers: list[tuple[float, Callable[[], None]]] = []
        self.screen = b""
        self.confirm_until = 0.0
        self.answered: set[str] = set()
        # Startup notices, replayed to each subscriber: one may connect later.
        self.notices: list[dict] = []
        self.buffers: dict[socket.socket, bytes] = {}
        timeout = float(os.environ.get("CLAUDE_AGENT_RESULT_TIMEOUT", RESULT_TIMEOUT_SECONDS))
        self.results = Results(self.on_result, timeout)
        self.enter_due = 0.0
        self.pid = 0
        self.master = -1
        self.status: int | None = None

    # The session ----------------------------------------------------------

    def spawn(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in PARENT_MARKERS}
        env.update(CLAUDE_AGENT="1", CLAUDE_AGENT_SOCKET=str(self.sock))
        env.setdefault("TERM", "xterm-256color")
        self.pid, self.master = pty.fork()
        if self.pid == 0:
            os.execvpe(self.argv[0], self.argv, env)
        self.resize(ROWS, COLUMNS)
        os.set_blocking(self.master, False)
        self.selector.register(self.master, selectors.EVENT_READ, self.on_output)

    def resize(self, rows: int, columns: int) -> None:
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

    def write(self, keys: bytes) -> None:
        os.write(self.master, keys)

    def paste(self, text: str) -> None:
        """One bracketed paste, then Enter: a multi-line text is one prompt."""
        self.write(PASTE_START + text.encode() + PASTE_END)
        self.enter_due = time.monotonic() + SUBMIT_DELAY_SECONDS
        self.later(SUBMIT_DELAY_SECONDS, lambda: self.write(ENTER))

    def on_output(self) -> None:
        try:
            output = os.read(self.master, READ_CHUNK)
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            output = b""  # EIO: the session closed its terminal
        if not output:
            self.selector.unregister(self.master)
            self.reap()
            return
        self.screen = (self.screen + output)[-SCREEN_CHARS:]
        for client in list(self.attached):
            self.send_raw(client, output)
        self.read_screen(screen_text(self.screen))

    def read_screen(self, text: str) -> None:
        if self.state == "starting":
            self.startup_dialogs(text)
        elif self.confirm_until > time.monotonic() and CONFIRM.search(text):
            # A built-in asked to confirm (/effort mid-conversation does). The
            # operator asked for it; its default is yes.
            self.confirm_until = 0.0
            self.screen = b""
            self.write(ENTER)
            log("confirmed the command's dialog")

    def startup_dialogs(self, text: str) -> None:
        if "trust" in text and "folder" in text and "trust" not in self.answered:
            self.answered.add("trust")
            log("the working directory is not trusted yet; answer once with: claude-agent attach --rw")
            notice = {"event": "notice", "text": "The working directory is not trusted yet."}
            self.notices.append(notice)
            self.emit(notice)
        if "Bypass" in text and "bypass" not in self.answered and "bypassPermissions" in self.argv:
            # Its default is "No, exit"; the mode was asked for on the command line.
            self.answered.add("bypass")
            self.screen = b""
            self.later(1.0, lambda: (self.write(DOWN), self.later(0.5, lambda: self.write(ENTER))))
            log("accepted bypass permissions mode")

    def reap(self) -> None:
        _, status = os.waitpid(self.pid, 0)
        self.status = os.waitstatus_to_exitcode(status)
        log(f"claude exited with status {self.status}")
        self.emit({"event": "exited", "status": self.status})

    # State, from hooks -------------------------------------------------------

    def on_event(self, event: dict) -> None:
        handlers = {
            "SessionStart": self.on_session_start,
            "UserPromptSubmit": lambda _: self.busy() if self.state != "starting" else None,
            "Stop": self.on_turn_end,
            "StopFailure": self.on_turn_end,
            "PreToolUse": self.on_tool,
            "PostToolUse": self.on_tool,
            "Notification": self.on_notification,
        }
        handler = handlers.get(str(event.get("hook_event_name")))
        if handler:
            handler(event)

    def on_session_start(self, event: dict) -> None:
        self.session = str(event.get("session_id", ""))
        self.results.transcript(str(event.get("transcript_path", "")))
        if self.state == "starting":
            self.later(SETTLE_SECONDS, self.idle)

    def on_turn_end(self, event: dict) -> None:
        self.dialog = None
        kind = "reply" if event.get("hook_event_name") == "Stop" else "error"
        text = event.get("last_assistant_message") or event.get("error") or ""
        self.emit({"event": kind, "text": str(text)})
        self.idle()

    def on_tool(self, event: dict) -> None:
        tool = event.get("tool_name")
        if event.get("hook_event_name") == "PreToolUse" and tool in DIALOG_TOOLS:
            self.dialog = tool
            self.emit({"event": "question", "tool": tool, "input": event.get("tool_input", {})})
            return
        if event.get("hook_event_name") == "PostToolUse" and tool == self.dialog:
            self.dialog = None
        self.emit({"event": "heartbeat"})

    def on_notification(self, event: dict) -> None:
        # A question already reported needs no "Claude needs your permission".
        if not self.dialog:
            self.emit({"event": "notice", "text": str(event.get("message", "")),
                       "type": event.get("notification_type")})

    def busy(self) -> None:
        if self.state != "busy":
            self.state = "busy"
            self.emit({"event": "busy"})

    def on_result(self, event: dict) -> None:
        self.emit(event)
        self.command_done()

    def command_done(self) -> None:
        if self.state == "command":
            self.idle()

    def idle(self) -> None:
        self.state = "idle"
        self.emit({"event": "idle"})
        self.drain()

    def drain(self) -> None:
        """Type what was held, commands first, now that nobody is mid-turn."""
        if self.state != "idle":
            return
        if self.commands:
            self.type_command(self.commands.pop(0))
        elif self.held:
            text = "\n\n".join(self.held)
            self.held.clear()
            self.busy()
            self.paste(text)

    def type_command(self, line: str) -> None:
        name = line[1:].split(" ")[0]
        self.state = "command"
        self.results.expect(name)
        self.screen = b""
        self.confirm_until = time.monotonic() + CONFIRM_WINDOW_SECONDS
        self.paste(line)
        log(f"typed /{name}")
        # A built-in fires no hook. The session is free again when its result
        # is in the transcript, or after a wait sized to the command.
        wait = COMPACT_WAIT_SECONDS if name == "compact" else CONFIRM_WINDOW_SECONDS
        self.later(wait, self.command_done)

    # Requests ---------------------------------------------------------------

    def submit(self, text: str) -> dict:
        text = text.strip()
        if not text or len(text) > MAX_TEXT_CHARS:
            return {"ok": False, "error": "empty or too long"}
        if text.startswith("!"):
            # A typed ! runs a shell command with no hook at all.
            return {"ok": False, "error": "shell escapes are refused"}
        if text.startswith("/"):
            # A typed / runs a built-in: /exit, /config. Built-ins come only
            # through "command", which allows a fixed list.
            return {"ok": False, "error": "slash commands go through the command request"}
        if self.dialog and self.state == "busy":
            # The text answers the open question: close the dialog, send it.
            self.dialog = None
            self.write(ESCAPE)
            self.later(STOP_SETTLE_SECONDS, lambda: self.paste(text))
            return {"ok": True, "answered": True}
        if len(self.held) >= MAX_HELD_TEXTS or sum(map(len, self.held)) + len(text) > MAX_HELD_CHARS:
            return {"ok": False, "error": "too much is already waiting for this turn; /stop or wait"}
        self.held.append(text)
        self.drain()
        return {"ok": True, "held": bool(self.held)}

    def command(self, line: str) -> dict:
        line = " ".join(line.split())
        if not allowed_command(line):
            return {"ok": False, "error": "not an allowed command"}
        if len(self.commands) >= MAX_HELD_TEXTS:
            return {"ok": False, "error": "too many commands are already waiting; /stop or wait"}
        self.commands.append(line)
        self.drain()
        return {"ok": True}

    def stop(self, text: str) -> dict:
        dropped = len(self.held)
        self.held.clear()
        self.commands.clear()
        self.dialog = None
        # A paste whose Enter is still due would take the Escape as an edit
        # and then run anyway; interrupt once it has been submitted.
        wait = max(0.0, self.enter_due - time.monotonic()) + (0.2 if self.enter_due else 0.0)
        self.later(wait, lambda: self.write(ESCAPE))
        # Stop does not fire on an interrupt; the session is idle once it settles.
        self.later(wait + STOP_SETTLE_SECONDS, lambda: self.after_stop(text))
        return {"ok": True, "dropped": dropped}

    def after_stop(self, text: str) -> None:
        self.emit({"event": "stopped"})
        self.state = "idle"
        if text.strip():
            self.submit(text)

    def handle(self, client: socket.socket, request: dict) -> dict | None:
        """Answer one request; None when the reply is not a JSON line."""
        ops: dict[str, Callable[[], dict | None]] = {
            "event": lambda: self.on_event(request),
            "submit": lambda: self.submit(str(request.get("text", ""))),
            "command": lambda: self.command(str(request.get("line", ""))),
            "stop": lambda: self.stop(str(request.get("text", ""))),
            "status": self.describe,
            "subscribe": lambda: self.subscribe(client),
            "attach": lambda: self.attach(client, bool(request.get("rw"))),
        }
        op = ops.get(str(request.get("op")))
        return op() if op else {"ok": False, "error": f"unknown op {request.get('op')!r}"}

    def describe(self) -> dict:
        return {"ok": True, "state": self.state, "held": len(self.held), "dialog": self.dialog,
                "session": self.session, "pid": self.pid}

    def subscribe(self, client: socket.socket) -> None:
        self.subscribers.append(client)
        self.send_line(client, {"ok": True})
        for notice in self.notices:
            self.send_line(client, notice)

    def attach(self, client: socket.socket, rw: bool) -> None:
        self.attached[client] = rw
        self.send_line(client, {"ok": True})
        self.send_raw(client, self.screen)
        # A resize makes the TUI draw the whole screen for the newcomer.
        self.resize(ROWS - 1, COLUMNS)
        self.later(0.2, lambda: self.resize(ROWS, COLUMNS))

    # Socket plumbing ----------------------------------------------------------

    def listen(self) -> None:
        self.sock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.sock.exists():
            self.sock.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            server.bind(str(self.sock))
        finally:
            os.umask(old)
        server.listen(16)
        server.setblocking(False)
        self.selector.register(server, selectors.EVENT_READ, lambda: self.on_accept(server))

    def on_accept(self, server: socket.socket) -> None:
        client, _ = server.accept()
        client.setblocking(False)
        self.selector.register(client, selectors.EVENT_READ, lambda: self.on_client(client))
        self.buffers[client] = b""

    def on_client(self, client: socket.socket) -> None:
        try:
            received = client.recv(READ_CHUNK)
        except OSError:
            received = b""  # a reset is a hang-up
        if not received:
            self.drop(client)
            return
        if client in self.attached:
            if self.attached[client]:
                self.write(received)
            return
        self.buffers[client] += received
        while b"\n" in self.buffers.get(client, b"") and client not in self.attached:
            line, self.buffers[client] = self.buffers[client].split(b"\n", 1)
            try:
                request = json.loads(line)
            except ValueError:
                request = {}
            reply = self.handle(client, request) if isinstance(request, dict) else {"ok": False}
            if reply is not None:
                self.send_line(client, reply)

    def send_line(self, client: socket.socket, message: dict) -> None:
        self.send_raw(client, json.dumps(message).encode() + b"\n")

    def send_raw(self, client: socket.socket, payload: bytes) -> None:
        """Send without blocking the loop. A client too slow to take it is dropped;
        the bridge reconnects, and a viewer attaches again."""
        try:
            sent = client.send(payload)
        except OSError:
            sent = -1
        if sent != len(payload):
            self.drop(client)

    def drop(self, client: socket.socket) -> None:
        self.attached.pop(client, None)
        self.buffers.pop(client, None)
        if client in self.subscribers:
            self.subscribers.remove(client)
        if client.fileno() in self.selector.get_map():
            self.selector.unregister(client)
        client.close()

    def emit(self, event: dict) -> None:
        for client in list(self.subscribers):
            self.send_line(client, event)

    def later(self, delay: float, action: Callable[[], None]) -> None:
        self.timers.append((time.monotonic() + delay, action))

    def run(self) -> int:
        self.listen()
        self.spawn()
        log(f"session started; socket {self.sock}")
        quiet = float(os.environ.get("CLAUDE_AGENT_STARTUP_QUIET", 60))
        self.later(quiet, lambda: self.idle() if self.state == "starting" else None)
        while self.status is None:
            for key, _ in self.selector.select(timeout=0.2):
                key.data()
            now = time.monotonic()
            due = [t for t in self.timers if t[0] <= now]
            self.timers = [t for t in self.timers if t[0] > now]
            for _, action in due:
                action()
            self.results.poll()
        self.sock.unlink(missing_ok=True)
        return self.status


def main(argv: list[str]) -> int:
    claude = os.environ.get("CLAUDE_AGENT_CLAUDE", "claude")
    sock = socket_path()
    if len(os.fsencode(sock)) > MAX_SOCKET_PATH:
        log(f"the socket path {sock} is longer than a unix socket allows ({MAX_SOCKET_PATH} bytes); "
            "set CLAUDE_AGENT_RUNTIME_DIR to a shorter directory")
        return 1
    supervisor = Supervisor([claude, *argv[1:]], sock)

    def forward(signum: int, _frame: object) -> None:
        if supervisor.pid:
            os.kill(supervisor.pid, signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
