#!/usr/bin/env python3
"""Hold an interactive Claude Code session in a terminal and drive it from a socket.

    supervisor.py [claude arguments...]

With tmux installed, Claude Code runs in tmux session CLAUDE_AGENT_TMUX_SESSION
(default claude-agent) on a tmux server of its own, socket name
CLAUDE_AGENT_TMUX_SOCKET (default claude-agent), which a person reaches with
`claude-agent attach` at their own terminal's size. Without it, Claude Code
runs in a pseudo-terminal of fixed size that the socket relays.
CLAUDE_AGENT_TERMINAL=tmux or pty chooses. Everything else talks to this
process over a unix socket (CLAUDE_AGENT_SOCKET, under
CLAUDE_AGENT_RUNTIME_DIR), one JSON object per line:

    {"op": "event", ...}            a hook event, from hooks.py event
    {"op": "submit", "text": ...}   a prompt; held until the session is idle
    {"op": "command", "line": ...}  an allowlisted built-in such as /compact
    {"op": "stop", "text": ...}     interrupt; then submit text, if any
    {"op": "status"}                state, held texts, session
    {"op": "subscribe"}             a stream of {"event": ...} lines
    {"op": "attach", "rw": false}   the raw terminal, both ways if rw (pty only)

The session's state comes from its hooks, never from the screen: idle after
SessionStart, Stop or StopFailure, busy from UserPromptSubmit. Texts that
arrive while it is busy are held and pasted together, as one prompt, when it
is idle again: a paste mid-turn would be folded into the running turn, and
one into an open dialog would answer it. The screen is read in two places
only, both narrow: the startup warnings, and the confirmation a built-in
command may open.

Exits with Claude Code's status. With CLAUDE_AGENT_RESPAWN=N it instead starts
Claude Code again N seconds after it exits, in the same terminal, and keeps
the socket: the bridge and anyone attached stay where they are.
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
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path
from typing import Callable

# The terminal's size; in tmux, only until a person attaches and tmux sizes it
# to their terminal.
ROWS = 50
COLUMNS = 160
SESSION = "claude-agent"
TMUX_SOCKET = "claude-agent"
# Bytes per send-keys, each one an argument of three bytes: tmux 3.8 refuses a
# command of about 3 KB ("command too long"), older tmux more.
KEYS_PER_COMMAND = 512
TMUX_TIMEOUT_SECONDS = 10
PANE_POLL_SECONDS = 0.5
SCREEN_POLL_SECONDS = 0.25
SCREEN_CHARS = 20000
READ_CHUNK = 65536
PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"
ENTER, ESCAPE, DOWN = b"\r", b"\x1b", b"\x1b[B"
SETTLE_SECONDS = 1.0
SUBMIT_DELAY_SECONDS = 0.3
CONFIRM_WINDOW_SECONDS = 3.0
STOP_SETTLE_SECONDS = 1.0
# The shortest CLAUDE_AGENT_RESPAWN. A key timed for a Claude Code -- a paste's
# Enter, a stop's Escape, up to about STOP_SETTLE_SECONDS after it was asked
# for -- must not outlive it into the next, which in tmux has the same pane.
RESPAWN_FLOOR_SECONDS = 2.0
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
    "CLAUDECODE",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED",
    # And the tmux it runs in: with these, a bare `tmux` in Claude Code's shell
    # reaches that server -- ours, where kill-server would end Claude Code.
    "TMUX",
    "TMUX_PANE",
)
# What a pane reports: itself, dead or not, how it ended, and the pid it runs.
# tmux answers for a pane that is gone with empty fields and status 0, so the
# id is how to tell.
PANE_FORMAT = "#{pane_id}:#{pane_dead}:#{pane_dead_status}:#{pane_dead_signal}:#{pane_pid}"
# The session's supervisor, as identity() gives it: a session whose supervisor
# is gone was left by one that was killed, and the next may end it.
OWNER_OPTION = "@claude-agent-supervisor"
# starttime, the 22nd field of /proc/PID/stat, counted from after "(comm)".
STAT_STARTTIME = 19
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


def tmux_session() -> str:
    """The tmux session Claude Code runs in: CLAUDE_AGENT_TMUX_SESSION, else claude-agent."""
    return os.environ.get("CLAUDE_AGENT_TMUX_SESSION") or SESSION


def tmux_socket() -> str:
    """The tmux server's socket name: CLAUDE_AGENT_TMUX_SOCKET, else claude-agent."""
    return os.environ.get("CLAUDE_AGENT_TMUX_SOCKET") or TMUX_SOCKET


def literal(text: str) -> str:
    """text as a tmux format that expands to itself: no #(...) runs, no #{...} is read."""
    return text.replace("#", "##")


def tmux(*args: str) -> subprocess.CompletedProcess[bytes]:
    """One tmux command, never left to hang the loop; a failure is a nonzero status.

    Always the server on our own socket. Without TMUX in its environment, and
    with -L, no command reaches a tmux server the supervisor itself runs in:
    there an empty target would be the caller's own pane.
    """
    argv = ["tmux", "-L", tmux_socket(), *args]
    try:
        return subprocess.run(
            argv, env=tmux_env(), capture_output=True, timeout=TMUX_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 1, b"", str(exc).encode())


def identity(pid: int) -> str:
    """host:pid:start time, which a pid used again by another process does not match."""
    try:
        # comm, in parentheses, may hold spaces.
        started = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[STAT_STARTTIME]
    except (OSError, IndexError):
        started = ""
    return f"{socket.gethostname()}:{pid}:{started}"


def tmux_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}


def quoted(arg: str) -> str:
    """arg as one word of a tmux command line: tmux's parser quotes as a shell does."""
    return "'" + arg.replace("'", "'\\''") + "'"


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
        texts = [
            f"Compacted {compacted.get('preTokens', 0):,} → {compacted.get('postTokens', 0):,} tokens"
            f" in {round(compacted.get('durationMs', 0) / 1000)}s."
        ]
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
        self.pending.append(
            {
                "name": name,
                "deadline": time.monotonic() + self.timeout,
                "seen": False,
                "compacted": None,
                "summary": None,
            }
        )

    def records(self) -> list[dict]:
        if not self.path:
            return []
        try:
            with self.path.open("rb") as transcript:
                transcript.seek(self.offset)
                appended = transcript.read()
        except OSError:
            return []
        appended = appended[: appended.rfind(b"\n") + 1]
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
            self.emit(
                {
                    "event": "result",
                    "command": item["name"],
                    "texts": [f"No result from /{item['name']} within {minutes} min."],
                }
            )

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
            if not item["seen"] and (
                run.get("command") == item["name"] or f"<command-name>/{item['name']}</command-name>" in text
            ):
                item["seen"] = True
                break
        output = OUTPUT.search(text)
        seen = next((p for p in self.pending if p["seen"]), None)
        if output and seen:
            item = seen
            self.pending.remove(item)
            texts = answer(item["name"], output[2], item["compacted"], item["summary"])
            self.emit({"event": "result", "command": item["name"], "texts": texts})


class Control:
    """A tmux control-mode client (tmux -C) on one pane's session, read through the selector.

    The pane is polled and typed into while the loop also serves the socket,
    so nothing here waits on tmux: a command goes in as a line, and its reply
    comes back, in order, between %begin and %end (or %error), to the callback
    it was sent with. A tmux that stops answering costs the replies, not the
    loop. A client of its own for each Claude Code: one that is closed takes
    its unanswered callbacks with it.
    """

    def __init__(self, selector: selectors.BaseSelector, pane: str) -> None:
        self.selector = selector
        self.waiting: list[tuple[float, Callable[[bool, str], None]]] = []
        self.pending = b""
        self.received = b""
        self.block: list[str] | None = None
        self.guard = ""
        # Attached: its own attach-session has answered.
        self.attached = False
        self.ended = False
        # Not sized by: the window follows whoever attaches to look.
        flags = "no-output,ignore-size"
        argv = ["tmux", "-L", tmux_socket(), "-C", "attach-session", "-f", flags, "-t", pane]
        its_stdin, self.to_tmux = os.pipe()
        self.from_tmux, its_stdout = os.pipe()
        try:
            self.proc: subprocess.Popen[bytes] | None = subprocess.Popen(
                argv, env=tmux_env(), stdin=its_stdin, stdout=its_stdout, stderr=subprocess.DEVNULL
            )
        except OSError as exc:
            log(f"could not reach tmux: {exc}")
            self.proc = None
        finally:
            os.close(its_stdin)
            os.close(its_stdout)
        if not self.proc:
            self.close()
            return
        os.set_blocking(self.to_tmux, False)
        os.set_blocking(self.from_tmux, False)
        selector.register(self.from_tmux, selectors.EVENT_READ, self.on_readable)

    def send(self, args: list[str], reply: Callable[[bool, str], None]) -> None:
        if self.ended:
            return
        self.waiting.append((time.monotonic(), reply))
        self.pending += (" ".join(map(quoted, args)) + "\n").encode()
        self.flush()

    def overdue(self) -> bool:
        """A reply that has kept us waiting longer than a tmux command may take."""
        return bool(self.waiting) and time.monotonic() - self.waiting[0][0] > TMUX_TIMEOUT_SECONDS

    def flush(self) -> None:
        if self.ended:
            # Ready in the select that closed it; its fd may be another's now.
            return
        try:
            written = os.write(self.to_tmux, self.pending)
        except BlockingIOError:
            written = 0
        except OSError:
            self.close()
            return
        self.pending = self.pending[written:]
        registered = self.registered(self.to_tmux)
        if self.pending and not registered:
            self.selector.register(self.to_tmux, selectors.EVENT_WRITE, self.flush)
        elif not self.pending and registered:
            self.selector.unregister(self.to_tmux)

    def registered(self, fd: int) -> bool:
        try:
            self.selector.get_key(fd)
        except KeyError:
            return False
        return True

    def on_readable(self) -> None:
        if self.ended:
            return
        try:
            output = os.read(self.from_tmux, READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            output = b""
        if not output:
            self.close()
            return
        self.received += output
        while b"\n" in self.received and not self.ended:
            line, self.received = self.received.split(b"\n", 1)
            self.on_line(line.decode(errors="replace"))

    def on_line(self, line: str) -> None:
        if self.block is None:
            # Outside a reply, a notification: none of them is ours to act on.
            if line.startswith("%begin "):
                self.block, self.guard = [], line.partition(" ")[2]
            return
        kind, _, guard = line.partition(" ")
        # A line of a reply -- a screen's, say -- may itself read "%end"; the
        # one that ends it repeats its %begin's time, number and flags.
        if kind not in ("%end", "%error") or guard != self.guard:
            self.block.append(line)
            return
        text, self.block = "\n".join(self.block), None
        if not guard.endswith(" 1"):
            # The attach-session it was started with, not a command we sent;
            # %error when the pane is gone.
            self.attached = kind == "%end"
            return
        if self.waiting:
            _, reply = self.waiting.pop(0)
            reply(kind == "%end", text)

    def close(self) -> None:
        """Done with it; whatever it was still to answer goes unanswered."""
        if self.ended:
            return
        self.ended = True
        self.waiting.clear()
        for fd in (self.to_tmux, self.from_tmux):
            if self.proc and self.registered(fd):
                self.selector.unregister(fd)
            os.close(fd)
        if self.proc:
            self.proc.kill()
            self.proc.wait()


class Tmux:
    """Claude Code in a pane of a tmux server of our own, which people attach to.

    Polling and typing go through a control-mode client (Control), so tmux
    never holds up the loop; starting Claude Code, and signalling it, are
    rare and are tmux commands run to the end.
    """

    def __init__(self, selector: selectors.BaseSelector) -> None:
        self.selector = selector
        self.session = tmux_session()
        self.pane = ""
        self.pid = 0
        self.ended: int | None = None
        self.marked = ""
        self.next_poll = 0.0
        self.control: Control | None = None
        # A pane poll, a screen capture, still to be answered.
        self.polling = False
        self.capturing = False

    def taken(self) -> str:
        """Why it cannot start, or nothing.

        A session whose supervisor was killed (tmux daemonizes, so nothing
        else ends it) is ended here. One with no supervisor named, or one on
        another host, or one whose supervisor still runs, is left alone.
        """
        target = f"={self.session}:"
        if tmux("has-session", "-t", target).returncode != 0:
            return ""
        shown = tmux("show-options", "-v", "-t", target, OWNER_OPTION)
        # Unset, or unreadable: no supervisor named, so not ours to end.
        owner = shown.stdout.decode().strip() if shown.returncode == 0 else ""
        host, _, pid = owner.rpartition(":")[0].partition(":")
        if host == socket.gethostname() and pid.isdigit() and identity(int(pid)) != owner:
            log(f"ending tmux session {self.session}, left by supervisor {pid}, which is gone")
            if tmux("kill-session", "-t", target).returncode == 0:
                return ""
        return (
            f"tmux session {self.session} already exists; "
            "end it or set CLAUDE_AGENT_TMUX_SESSION to another name"
        )

    def start(self, argv: list[str], env: dict[str, str]) -> bool:
        """Start Claude Code: in its own pane again, else a new window, else a new session.

        tmux gives the pane the environment its server started with. The
        server is ours alone and ends with its last session, so the supervisor
        is what starts it, and Claude Code gets the supervisor's environment.
        remain-on-exit keeps a pane that ended long enough to read how it ended.
        """
        self.ended, self.marked, self.next_poll, self.pid = None, "", 0.0, 0
        self.disconnect()
        unset = [arg for name in PARENT_MARKERS for arg in ("-u", name)]
        command = ["env", *unset, *(f"{k}={v}" for k, v in env.items()), *argv]
        # tmux runs these as an argv, no shell, but takes an argument that
        # ends in ";" for its own command separator unless it is escaped.
        start = self.start_command([arg[:-1] + "\\;" if arg.endswith(";") else arg for arg in command])
        # Chained, so the option is set before tmux can notice an early exit;
        # with no -t it applies to the window just made.
        started = tmux(*start, "remain-on-exit", "on")
        pane = self.pane if start[0] == "respawn-pane" else started.stdout.decode().strip()
        if started.returncode != 0 or not pane.startswith("%"):
            # The old pane is kept: the next attempt starts in it again, where
            # a new session would find its name taken.
            log(f"could not start claude in tmux: {started.stderr.decode().strip()}")
            return False
        self.pane = pane
        tmux("set-option", "-t", f"={self.session}:", OWNER_OPTION, identity(os.getpid()))
        return True

    def start_command(self, command: list[str]) -> list[str]:
        """The tmux command that runs command: the old pane, a new window, or a new session."""
        target = f"={self.session}:"
        # -c and -s are formats, where #(...) runs a shell.
        cwd = literal(os.getcwd())
        shown = tmux("display-message", "-p", "-t", self.pane, "#{pane_id}") if self.pane else None
        if shown and shown.stdout.decode().strip() == self.pane:
            start = ["respawn-pane", "-k", "-t", self.pane, "-c", cwd, *command, ";"]
            return start + ["set-option", "-w", "-t", self.pane]
        if self.pane and tmux("has-session", "-t", target).returncode == 0:
            start = ["new-window", "-t", target, "-c", cwd, "-P", "-F", "#{pane_id}", *command]
            return start + [";", "set-option", "-w"]
        start = ["new-session", "-d", "-s", literal(self.session), "-x", str(COLUMNS), "-y", str(ROWS)]
        return start + ["-c", cwd, "-P", "-F", "#{pane_id}", *command, ";", "set-option", "-w"]

    def connected(self) -> Control | None:
        """The control client for this Claude Code's pane, started if it is not running."""
        if self.ended is not None or not self.pane:
            return None
        if self.control is None or self.control.ended:
            self.control, self.polling, self.capturing = Control(self.selector, self.pane), False, False
        return self.control

    def disconnect(self) -> None:
        if self.control:
            self.control.close()
        self.control = None

    def write(self, keys: bytes) -> None:
        """Bytes into the pane as they are, as a pty write would put them.

        Not paste-buffer: tmux 3.6 and later replace its control characters,
        a paste's brackets among them, unless given -S, which older tmux lacks.
        """

        def typed(ok: bool, error: str) -> None:
            if not ok:
                log(f"could not type into tmux: {error.strip()}")

        if control := self.connected():
            for start in range(0, len(keys), KEYS_PER_COMMAND):
                chunk = keys[start : start + KEYS_PER_COMMAND]
                control.send(["send-keys", "-t", self.pane, "-H", *(f"{key:02x}" for key in chunk)], typed)

    def capture(self, then: Callable[[str], None]) -> None:
        control = self.connected()
        if control:
            control.send(["capture-pane", "-p", "-t", self.pane], lambda ok, text: then(text if ok else ""))

    def screen(self, then: Callable[[str], None]) -> None:
        """The lines on screen that were not there at the last mark, to then, once tmux answers."""

        def captured(text: str) -> None:
            self.capturing = False
            old = set(self.marked.splitlines())
            then("\n".join(line for line in text.splitlines() if line not in old))

        # One at a time: a tmux slow to answer is not asked again meanwhile.
        if not self.capturing:
            self.capturing = True
            self.capture(captured)

    def mark(self) -> None:
        # Answered before any screen asked for after it: tmux replies in order.
        self.capture(lambda text: setattr(self, "marked", text))

    def fields(self, shown: str) -> list[str] | None:
        """The pane's dead, status, signal and pid fields from PANE_FORMAT; None when it is gone."""
        fields = shown.strip().split(":")
        if len(fields) != PANE_FORMAT.count(":") + 1 or fields[0] != self.pane:
            return None
        return fields[1:]

    def shown(self) -> list[str] | None:
        shown = tmux("display-message", "-p", "-t", self.pane, PANE_FORMAT)
        return self.fields(shown.stdout.decode()) if shown.returncode == 0 else None

    def poll(self) -> int | None:
        """Claude Code's exit status once it has ended.

        Asked of tmux every PANE_POLL_SECONDS; the answer is taken when it
        comes. A tmux that leaves a question unanswered too long gets a new
        client; one that cannot attach one at all has lost the pane.
        """
        if self.control and self.control.ended:
            self.gone(self.control.attached)
            return self.ended
        control = self.connected()
        if control and control.overdue():
            log("tmux has not answered for a while; reconnecting")
            self.disconnect()
        elif control and not self.polling and time.monotonic() >= self.next_poll:
            self.next_poll = time.monotonic() + PANE_POLL_SECONDS
            self.polling = True
            control.send(["display-message", "-p", "-t", self.pane, PANE_FORMAT], self.on_shown)
        return self.ended

    def on_shown(self, ok: bool, shown: str) -> None:
        self.polling = False
        fields = self.fields(shown) if ok else None
        if not fields:
            self.gone(False)
            return
        dead, status, signum, pid = fields
        self.pid = int(pid) if pid.isdigit() and dead == "0" else 0
        if dead == "1":
            self.end(int(status) if status.lstrip("-").isdigit() else -int(signum or 1))

    def gone(self, attached: bool) -> None:
        """The control client ended, or the pane was not there to answer.

        One that had attached may have ended for any reason: the next poll
        starts another. One that never could, or a pane tmux no longer knows,
        is a pane gone -- killed, or tmux with it -- that took its status.
        """
        if attached:
            self.control = None
        else:
            self.end(1)

    def end(self, status: int) -> None:
        self.ended, self.pid = status, 0
        self.disconnect()

    def kill(self, signum: int) -> None:
        """Signal Claude Code, and only while its pane says it still runs that pid.

        A pid read earlier may belong to a pane that has since died, and then to
        any process at all; so it is asked for again, and a pane that cannot
        vouch for a live pid is killed through tmux instead.
        """
        dead, _, _, pid = (self.shown() if self.pane else None) or ["1", "", "", ""]
        if dead == "0" and pid.isdigit() and int(pid) > 1:
            os.kill(int(pid), signum)
        elif self.pane:
            tmux("kill-pane", "-t", self.pane)

    def close(self) -> None:
        self.disconnect()
        if self.pane:
            tmux("kill-pane", "-t", self.pane)

    def describe(self) -> dict:
        return {"terminal": "tmux", "tmux_socket": tmux_socket(), "tmux_session": self.session}


class Pty:
    """Claude Code in a pseudo-terminal of its own, for a host without tmux.

    Its output goes to on_output as it comes, for whoever attached through the
    socket.
    """

    def __init__(self, selector: selectors.BaseSelector, on_output: Callable[[bytes], None]) -> None:
        self.selector = selector
        self.on_output = on_output
        self.pid = 0
        self.master = -1
        self.raw = b""
        self.ended: int | None = None

    def taken(self) -> str:
        return ""

    def start(self, argv: list[str], env: dict[str, str]) -> bool:
        self.close()
        self.raw, self.ended = b"", None
        inherited = {k: v for k, v in os.environ.items() if k not in PARENT_MARKERS}
        env = {"TERM": "xterm-256color"} | inherited | env
        self.pid, self.master = pty.fork()
        if self.pid == 0:
            os.execvpe(argv[0], argv, env)
        self.resize(ROWS, COLUMNS)
        os.set_blocking(self.master, False)
        self.selector.register(self.master, selectors.EVENT_READ, self.on_readable)
        return True

    def resize(self, rows: int, columns: int) -> None:
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

    def redraw(self, later: Callable[[float, Callable[[], None]], None]) -> None:
        """A resize makes the TUI draw the whole screen, for someone who just attached."""
        self.resize(ROWS - 1, COLUMNS)
        later(0.2, lambda: self.resize(ROWS, COLUMNS))

    def write(self, keys: bytes) -> None:
        if self.ended is None:
            os.write(self.master, keys)

    def on_readable(self) -> None:
        try:
            output = os.read(self.master, READ_CHUNK)
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            output = b""  # EIO: the session closed its terminal
        if not output:
            self.selector.unregister(self.master)
            _, status = os.waitpid(self.pid, 0)
            self.ended = os.waitstatus_to_exitcode(status)
            return
        self.raw = (self.raw + output)[-SCREEN_CHARS:]
        self.on_output(output)

    def screen(self, then: Callable[[str], None]) -> None:
        """The output since the last mark, escape sequences taken out, to then."""
        then(ANSI.sub(b" ", self.raw).decode(errors="replace"))

    def mark(self) -> None:
        self.raw = b""

    def poll(self) -> int | None:
        return self.ended

    def kill(self, signum: int) -> None:
        if self.pid and self.ended is None:
            os.kill(self.pid, signum)

    def close(self) -> None:
        """The terminal of a Claude Code that has ended; a new one gets its own."""
        if self.master >= 0:
            os.close(self.master)
            self.master = -1

    def describe(self) -> dict:
        return {"terminal": "pty"}


def terminal(selector: selectors.BaseSelector, on_output: Callable[[bytes], None]) -> Tmux | Pty:
    """CLAUDE_AGENT_TERMINAL, else tmux when it is installed, else a pty."""
    choice = os.environ.get("CLAUDE_AGENT_TERMINAL") or ("tmux" if shutil.which("tmux") else "pty")
    return Pty(selector, on_output) if choice == "pty" else Tmux(selector)


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
        self.confirm_until = 0.0
        self.answered: set[str] = set()
        # Startup notices, replayed to each subscriber: one may connect later.
        self.notices: list[dict] = []
        self.buffers: dict[socket.socket, bytes] = {}
        timeout = float(os.environ.get("CLAUDE_AGENT_RESULT_TIMEOUT", RESULT_TIMEOUT_SECONDS))
        self.results = Results(self.on_result, timeout)
        self.enter_due = 0.0
        self.term = terminal(self.selector, self.on_output)
        self.alive = False
        self.stopping = False
        respawn = os.environ.get("CLAUDE_AGENT_RESPAWN", "")
        self.respawn = float(respawn) if respawn else None
        if self.respawn is not None and self.respawn < RESPAWN_FLOOR_SECONDS:
            log(f"CLAUDE_AGENT_RESPAWN={respawn} is below the floor; waiting {RESPAWN_FLOOR_SECONDS:g}s")
            self.respawn = RESPAWN_FLOOR_SECONDS
        self.next_screen_poll = 0.0
        self.status: int | None = None
        # Which Claude Code a timer was set for; it moves on when one ends.
        self.starts = 0

    # The session ----------------------------------------------------------

    def spawn(self) -> bool:
        self.starts += 1
        env = {"CLAUDE_AGENT": "1", "CLAUDE_AGENT_SOCKET": str(self.sock)}
        self.alive = self.term.start(self.argv, env)
        return self.alive

    def write(self, keys: bytes) -> None:
        self.term.write(keys)

    def paste(self, text: str) -> None:
        """One bracketed paste, then Enter: a multi-line text is one prompt."""
        self.write(PASTE_START + text.encode() + PASTE_END)
        self.enter_due = time.monotonic() + SUBMIT_DELAY_SECONDS
        self.later(SUBMIT_DELAY_SECONDS, self.this_start(lambda: self.write(ENTER)))

    def on_output(self, output: bytes) -> None:
        for client in list(self.attached):
            self.send_raw(client, output)

    def watch(self) -> None:
        """Has Claude Code ended, and is there a screen to read."""
        if not self.alive:
            return
        status = self.term.poll()
        if status is not None:
            self.reap(status)
            return
        now = time.monotonic()
        reading = self.state == "starting" or self.confirm_until > now
        if reading and now >= self.next_screen_poll:
            self.next_screen_poll = now + SCREEN_POLL_SECONDS
            self.term.screen(self.read_screen)

    def read_screen(self, text: str) -> None:
        if self.state == "starting":
            self.startup_dialogs(text)
        elif self.confirm_until > time.monotonic() and CONFIRM.search(text):
            # A built-in asked to confirm (/effort mid-conversation does). The
            # operator asked for it; its default is yes.
            self.confirm_until = 0.0
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

            def accept_bypass() -> None:
                self.write(DOWN)
                self.later(0.5, self.this_start(lambda: self.write(ENTER)))

            self.later(1.0, self.this_start(accept_bypass))
            log("accepted bypass permissions mode")

    def reap(self, status: int) -> None:
        self.alive = False
        # Whatever was timed for this Claude Code is for nobody now.
        self.starts += 1
        log(f"claude exited with status {status}")
        self.emit({"event": "exited", "status": status})
        if self.respawn is None or self.stopping:
            self.term.close()
            self.status = status
            return
        # A new Claude Code asks its startup questions again.
        self.state = "starting"
        self.dialog = None
        self.answered.clear()
        self.notices.clear()
        log(f"starting claude again in {self.respawn:g}s")
        self.later(self.respawn, self.restart)

    def restart(self) -> None:
        if self.stopping:
            self.status = self.status if self.status is not None else 0
            return
        if not self.spawn():
            self.later(self.respawn or 1.0, self.restart)
            return
        self.startup_timeout()

    def startup_timeout(self) -> None:
        quiet = float(os.environ.get("CLAUDE_AGENT_STARTUP_QUIET", 60))
        self.later(quiet, self.this_start(lambda: self.idle() if self.state == "starting" else None))

    def this_start(
        self, action: Callable[[], None], otherwise: Callable[[], None] | None = None
    ) -> Callable[[], None]:
        """action, done only if the Claude Code it was set for still runs; else otherwise.

        reap() and spawn() both move starts on, so a timer outliving its Claude
        Code -- across a respawn delay, or into the next one's startup -- does
        nothing, or only otherwise.
        """
        start = self.starts

        def fenced() -> None:
            if self.starts == start:
                action()
            elif otherwise:
                otherwise()

        return fenced

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
            self.later(SETTLE_SECONDS, self.this_start(self.idle))

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
            self.emit(
                {
                    "event": "notice",
                    "text": str(event.get("message", "")),
                    "type": event.get("notification_type"),
                }
            )

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
        self.term.mark()
        self.confirm_until = time.monotonic() + CONFIRM_WINDOW_SECONDS
        self.paste(line)
        log(f"typed /{name}")
        # A built-in fires no hook. The session is free again when its result
        # is in the transcript, or after a wait sized to the command.
        wait = COMPACT_WAIT_SECONDS if name == "compact" else CONFIRM_WINDOW_SECONDS
        self.later(wait, self.this_start(self.command_done))

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
            # Ended meanwhile, it leaves the text for the next Claude Code.
            self.later(
                STOP_SETTLE_SECONDS, self.this_start(lambda: self.paste(text), lambda: self.hold(text))
            )
            return {"ok": True, "answered": True}
        if len(self.held) >= MAX_HELD_TEXTS or sum(map(len, self.held)) + len(text) > MAX_HELD_CHARS:
            return {"ok": False, "error": "too much is already waiting for this turn; /stop or wait"}
        self.hold(text)
        return {"ok": True, "held": bool(self.held)}

    def hold(self, text: str) -> None:
        self.held.append(text)
        self.drain()

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
        # Only into the Claude Code that was asked, and once it is past its
        # startup screens, where Escape would answer one.
        self.later(wait, self.this_start(lambda: self.write(ESCAPE) if self.state != "starting" else None))
        # Stop does not fire on an interrupt; the session is idle once it settles.
        self.later(wait + STOP_SETTLE_SECONDS, lambda: self.after_stop(text))
        return {"ok": True, "dropped": dropped}

    def after_stop(self, text: str) -> None:
        self.emit({"event": "stopped"})
        # With no Claude Code ready -- ended and not yet started again, or
        # still starting -- there is nothing to be idle: the text is held for
        # the next one, whose startup is still watched.
        if self.alive and self.state != "starting":
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
        return {
            "ok": True,
            "state": self.state,
            "held": len(self.held),
            "dialog": self.dialog,
            "session": self.session,
            "pid": self.term.pid,
            **self.term.describe(),
        }

    def subscribe(self, client: socket.socket) -> None:
        self.subscribers.append(client)
        self.send_line(client, {"ok": True})
        for notice in self.notices:
            self.send_line(client, notice)

    def attach(self, client: socket.socket, rw: bool) -> dict | None:
        if not isinstance(self.term, Pty):
            return {"ok": False, "error": "the session is in tmux; attach to it with tmux"}
        self.attached[client] = rw
        self.send_line(client, {"ok": True})
        self.send_raw(client, self.term.raw)
        self.term.redraw(self.later)
        return None

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
        # Registered with the selector exactly when it has a buffer; a closed
        # socket's fileno is -1, which the selector cannot look up.
        registered = client in self.buffers
        self.attached.pop(client, None)
        self.buffers.pop(client, None)
        if client in self.subscribers:
            self.subscribers.remove(client)
        if registered:
            self.selector.unregister(client)
        client.close()

    def emit(self, event: dict) -> None:
        for client in list(self.subscribers):
            self.send_line(client, event)

    def later(self, delay: float, action: Callable[[], None]) -> None:
        self.timers.append((time.monotonic() + delay, action))

    def run(self) -> int:
        taken = self.term.taken()
        if taken:
            log(taken)
            return 1
        # Listening first: Claude Code's first hook connects at once.
        self.listen()
        if not self.spawn():
            self.sock.unlink(missing_ok=True)
            return 1
        where = self.term.describe()
        log(f"session started in {where.get('tmux_session', 'a pty')}; socket {self.sock}")
        self.startup_timeout()
        while self.status is None:
            for key, _ in self.selector.select(timeout=0.2):
                key.data()
            now = time.monotonic()
            due = [t for t in self.timers if t[0] <= now]
            self.timers = [t for t in self.timers if t[0] > now]
            for _, action in due:
                action()
            self.results.poll()
            self.watch()
        self.sock.unlink(missing_ok=True)
        return self.status


def main(argv: list[str]) -> int:
    claude = os.environ.get("CLAUDE_AGENT_CLAUDE", "claude")
    sock = socket_path()
    if len(os.fsencode(sock)) > MAX_SOCKET_PATH:
        log(
            f"the socket path {sock} is longer than a unix socket allows ({MAX_SOCKET_PATH} bytes); "
            "set CLAUDE_AGENT_RUNTIME_DIR to a shorter directory"
        )
        return 1
    supervisor = Supervisor([claude, *argv[1:]], sock)

    def forward(signum: int, _frame: object) -> None:
        supervisor.stopping = True
        supervisor.term.kill(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
