"""The supervisor, driving a stand-in Claude Code (fake_claude.py) through its socket."""

from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest import mock

TOP = Path(__file__).resolve().parent.parent
SUPERVISOR = TOP / "root/usr/libexec/claude-agent/supervisor.py"
FAKE = Path(__file__).resolve().parent / "fake_claude.py"
# The tmux cases need tmux; CI runs them in an image that has it
# (pyproject.toml, [tool.sdlc.ci]), and everywhere else they are skipped.
NEEDS_TMUX = unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
# Run from inside tmux, as on a seat, every tmux these tests start would
# otherwise default to the server holding the person's own session.
os.environ.pop("TMUX", None)
os.environ.pop("TMUX_PANE", None)


def tmux_env(directory: Path) -> dict[str, str]:
    """A tmux server of the test's own, under directory: never a shared one."""
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env["TMUX_TMPDIR"] = str(directory)
    return env


def tmux(directory: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-L", "claude-agent", *args],
        env=tmux_env(directory),
        capture_output=True,
        text=True,
        check=False,
    )


class Events:
    """A subscription to the supervisor's events."""

    def __init__(self, path: Path) -> None:
        self.conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.conn.connect(str(path))
        self.conn.sendall(b'{"op": "subscribe"}\n')
        self.lines = self.conn.makefile()
        self.lines.readline()

    def until(self, kind: str, timeout: float = 15) -> list[dict]:
        """Every event up to and including the first of ``kind``."""
        seen: list[dict] = []
        self.conn.settimeout(timeout)
        while True:
            event = json.loads(self.lines.readline())
            seen.append(event)
            if event.get("event") == kind:
                return seen


class SupervisorTest(unittest.TestCase):
    """The session in a pty; TmuxSupervisorTest runs every case again in tmux."""

    TERMINAL = "pty"

    def setUp(self) -> None:
        self.launch()

    def launch(self, *args: str, cwd: str | None = None, **extra: str) -> None:
        """A supervisor in a directory of its own, with a tmux server of its own."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.sock = self.dir / "run/control.sock"
        (self.dir / "tmux").mkdir()
        if self.TERMINAL == "tmux":
            self.addCleanup(tmux, self.dir / "tmux", "kill-server")
        self.relaunch(*args, cwd=cwd, **extra)

    def relaunch(self, *args: str, cwd: str | None = None, **extra: str) -> None:
        """A supervisor in the last one's directory, on its tmux server."""
        env = {
            **tmux_env(self.dir / "tmux"),
            "CLAUDE_AGENT_TERMINAL": self.TERMINAL,
            "CLAUDE_AGENT_CLAUDE": str(FAKE),
            "CLAUDE_AGENT_RUNTIME_DIR": str(self.dir / "run"),
            "FAKE_CLAUDE_DIR": str(self.dir),
            "CLAUDE_AGENT_STARTUP_QUIET": "20",
            "CLAUDE_AGENT_RESULT_TIMEOUT": "5",
            "CLAUDECODE": "1",
            **extra,
        }
        env.pop("CLAUDE_AGENT_SOCKET", None)
        self.proc = subprocess.Popen(
            [sys.executable, str(SUPERVISOR), *args],
            env=env,
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.stop)
        deadline = time.time() + 10
        while not self.sock.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.events = Events(self.sock)
        if not extra.get("FAKE_CLAUDE_UNTRUSTED"):
            self.events.until("idle")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(10)
        assert self.proc.stderr is not None  # stderr=PIPE
        self.proc.stderr.close()

    def request(self, **message: object) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(10)
            conn.connect(str(self.sock))
            conn.sendall(json.dumps(message).encode() + b"\n")
            return json.loads(conn.makefile().readline())

    def test_a_prompt_gets_busy_then_its_reply(self) -> None:
        self.assertTrue(self.request(op="submit", text="hello")["ok"])
        seen = self.events.until("reply")
        self.assertEqual(seen[0], {"event": "busy"})
        self.assertEqual(seen[-1], {"event": "reply", "text": "echo: hello"})
        self.events.until("idle")

    def test_multi_line_text_is_one_prompt(self) -> None:
        self.request(op="submit", text="line one\nline two")
        self.assertEqual(self.events.until("reply")[-1]["text"], "echo: line one\nline two")

    def test_texts_sent_mid_turn_are_held_and_sent_together(self) -> None:
        self.request(op="submit", text="slow 2")
        self.events.until("busy")
        self.request(op="submit", text="first")
        self.request(op="submit", text="second")
        self.assertEqual(self.request(op="status")["held"], 2)
        self.assertEqual(self.events.until("reply")[-1]["text"], "done")
        self.assertEqual(self.events.until("reply")[-1]["text"], "echo: first\n\nsecond")

    def test_tool_calls_are_heartbeats(self) -> None:
        self.request(op="submit", text="slow 2")
        kinds = [event["event"] for event in self.events.until("reply")]
        self.assertGreaterEqual(kinds.count("heartbeat"), 2)

    def test_shell_escapes_and_slashes_are_refused(self) -> None:
        for text in ("!rm -rf /tmp/x", "  !ls", "/exit", "/config"):
            reply = self.request(op="submit", text=text)
            self.assertFalse(reply["ok"], text)

    def test_only_allowlisted_commands_are_typed(self) -> None:
        for line in ("/exit", "/config", "/model ; rm", "/effort huge", "/fast on\n/exit"):
            self.assertFalse(self.request(op="command", line=line)["ok"], line)

    def test_command_result_comes_from_the_transcript(self) -> None:
        self.assertTrue(self.request(op="command", line="/fast on")["ok"])
        seen = self.events.until("result")
        self.assertEqual(seen[-1]["texts"], ["/fast: Fast mode ON"])

    def test_command_confirmation_is_answered(self) -> None:
        self.request(op="command", line="/effort high")
        self.assertEqual(self.events.until("result")[-1]["texts"], ["/effort: Set effort level to high"])

    def test_clear_reports_from_the_new_transcript(self) -> None:
        self.request(op="command", line="/clear")
        self.assertEqual(self.events.until("result")[-1]["texts"], ["/clear: done"])
        self.request(op="submit", text="after")
        self.assertEqual(self.events.until("reply")[-1]["text"], "echo: after")

    def test_stop_interrupts_drops_held_and_sends_its_text(self) -> None:
        self.request(op="submit", text="slow 10")
        self.events.until("heartbeat")
        self.request(op="submit", text="held")
        self.assertEqual(self.request(op="stop", text="instead")["dropped"], 1)
        seen = self.events.until("reply")
        self.assertIn({"event": "stopped"}, seen)
        self.assertEqual(seen[-1]["text"], "echo: instead")

    def test_stop_right_after_a_paste_still_interrupts_it(self) -> None:
        self.request(op="submit", text="slow 10")
        self.request(op="stop")
        seen = self.events.until("stopped", timeout=8)
        self.assertNotIn("done", [event.get("text") for event in seen])

    def test_a_text_answers_an_open_question(self) -> None:
        self.request(op="submit", text="ask")
        question = self.events.until("question")[-1]
        self.assertEqual(question["tool"], "AskUserQuestion")
        # Claude Code's own "needs your permission" for the open question.
        time.sleep(1)
        self.assertTrue(self.request(op="submit", text="A please")["answered"])
        seen = self.events.until("reply")
        self.assertEqual(seen[-1]["text"], "echo: A please")
        self.assertNotIn("notice", [event["event"] for event in seen])

    def tmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        return tmux(self.dir / "tmux", *args)

    def test_attach_sees_the_screen(self) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(10)
            conn.connect(str(self.sock))
            conn.sendall(b'{"op": "attach", "rw": false}\n')
            data = b""
            while b"Welcome to Fake Claude" not in data:
                data += conn.recv(65536)
            # Read-only: keys from an attached viewer go nowhere.
            conn.sendall(b"exit\r")
            time.sleep(1)
            self.assertIsNone(self.proc.poll())

    def test_a_long_prompt_arrives_whole(self) -> None:
        # Longer than several send-keys in tmux.
        text = "".join(chr(ord("a") + n % 26) for n in range(5000))
        self.request(op="submit", text=text)
        self.assertEqual(self.events.until("reply")[-1]["text"], f"echo: {text}")

    def test_claude_gets_its_arguments_as_given(self) -> None:
        # A semicolon ends a tmux command, and a shell would expand $(...).
        args = ["a b", ";", "x;", "$(touch pwned)", "it's"]
        self.stop()
        self.launch(*args)
        self.assertEqual(json.loads((self.dir / "argv.json").read_text()), args)
        self.assertFalse((self.dir / "pwned").exists())

    def test_claude_gets_no_tmux_to_reach(self) -> None:
        # The tmux the supervisor runs in, or the one it runs Claude Code in.
        self.stop()
        self.launch(TMUX="/tmp/elsewhere,1,0", TMUX_PANE="%9")
        seen = json.loads((self.dir / "env.json").read_text())
        self.assertEqual((seen["TMUX"], seen["TMUX_PANE"]), (None, None))

    def test_held_texts_are_bounded(self) -> None:
        self.request(op="submit", text="slow 5")
        self.events.until("busy")
        replies = [self.request(op="submit", text=f"text {n}") for n in range(25)]
        self.assertTrue(all(reply["ok"] for reply in replies[:20]))
        self.assertFalse(replies[20]["ok"])
        self.assertEqual(self.request(op="status")["held"], 20)

    def test_held_characters_are_bounded(self) -> None:
        self.request(op="submit", text="slow 5")
        self.events.until("busy")
        self.assertTrue(self.request(op="submit", text="x" * 20000)["ok"])
        self.assertTrue(self.request(op="submit", text="y" * 20000)["ok"])
        self.assertFalse(self.request(op="submit", text="z")["ok"])

    def test_waiting_commands_are_bounded(self) -> None:
        self.request(op="submit", text="slow 5")
        self.events.until("busy")
        replies = [self.request(op="command", line="/fast on") for _ in range(21)]
        self.assertTrue(all(reply["ok"] for reply in replies[:20]))
        self.assertFalse(replies[20]["ok"])

    def test_exits_with_claudes_status(self) -> None:
        self.request(op="submit", text="exit")
        self.assertEqual(self.events.until("exited")[-1]["status"], 3)
        self.assertEqual(self.proc.wait(10), 3)
        self.assertFalse(self.sock.exists())


@NEEDS_TMUX
class TmuxSupervisorTest(SupervisorTest):
    """The same cases, with the session in tmux, and what only tmux does."""

    TERMINAL = "tmux"

    def test_attach_sees_the_screen(self) -> None:
        self.assertIn(
            "Welcome to Fake Claude", self.tmux("capture-pane", "-p", "-t", "=claude-agent:").stdout
        )
        # A client attaching at 90x20 -- a control-mode one, which needs no
        # terminal -- and the window, with Claude Code in it, follows it rather
        # than a size fixed at start.
        client = subprocess.Popen(
            ["tmux", "-L", "claude-agent", "-C", "attach-session", "-t", "=claude-agent:"],
            env=tmux_env(self.dir / "tmux"),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
        )
        self.addCleanup(client.wait, 10)
        self.addCleanup(client.kill)
        assert client.stdin is not None  # stdin=PIPE
        client.stdin.write("refresh-client -C 90x20\n")
        client.stdin.flush()
        size, deadline = "", time.time() + 10
        while size != "90x20" and time.time() < deadline:
            time.sleep(0.1)
            shown = self.tmux("display-message", "-p", "-t", "=claude-agent:", "#{pane_width}x#{pane_height}")
            size = shown.stdout.strip()
        self.assertEqual(size, "90x20")
        self.request(op="submit", text="after the resize")
        self.assertEqual(self.events.until("reply")[-1]["text"], "echo: after the resize")

    def test_the_tmux_it_runs_in_is_left_alone(self) -> None:
        # A supervisor started from inside a person's tmux, as a seat's agent
        # might: nothing it does, its exit included, reaches that server.
        outer = tempfile.TemporaryDirectory()
        self.addCleanup(outer.cleanup)
        server = ["tmux", "-S", f"{outer.name}/outer"]
        env = tmux_env(Path(outer.name))
        subprocess.run([*server, "new-session", "-d", "-s", "outer", "sleep 600"], env=env, check=True)
        self.addCleanup(subprocess.run, [*server, "kill-server"], env=env, check=False)
        pane = subprocess.run(
            [*server, "display-message", "-p", "-t", "outer", "#{pane_id}"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.launch(TMUX=f"{outer.name}/outer,1,0", TMUX_PANE=pane.stdout.strip())
        self.request(op="submit", text="exit")
        self.assertEqual(self.proc.wait(10), 3)
        listed = subprocess.run(
            [*server, "list-sessions", "-F", "#{session_name}"], env=env, capture_output=True, text=True
        )
        self.assertEqual(listed.stdout.split(), ["outer"])

    def test_a_session_name_and_directory_are_taken_literally(self) -> None:
        # Both are tmux formats, where #(...) runs a shell and #{...} expands.
        self.stop()
        work = self.dir / "d#(touch C1)#{pane_id}"
        work.mkdir()
        name = "c#(touch S1)#{session_id}"
        self.launch(cwd=str(work), CLAUDE_AGENT_TMUX_SESSION=name)
        self.assertEqual(json.loads((self.dir / "env.json").read_text())["cwd"], str(work))
        listed = self.tmux("list-sessions", "-F", "#{session_name}")
        self.assertEqual(listed.stdout.split("\n")[0], name)
        self.assertEqual(self.request(op="status")["tmux_session"], name)
        self.assertEqual([p.name for p in self.dir.rglob("*1") if p.name in ("C1", "S1")], [])

    def test_an_existing_session_is_not_taken_over(self) -> None:
        env = {**tmux_env(self.dir / "tmux"), "CLAUDE_AGENT_RUNTIME_DIR": str(self.dir / "run2")}
        second = subprocess.run(
            [sys.executable, str(SUPERVISOR)], env=env, capture_output=True, text=True, timeout=30
        )
        self.assertEqual(second.returncode, 1)
        self.assertIn("already exists", second.stderr)

    def test_a_session_left_by_a_killed_supervisor_is_ended(self) -> None:
        # SIGKILL, with no systemd to take the tmux server along.
        self.proc.kill()
        self.stop()
        self.assertEqual(self.tmux("has-session", "-t", "=claude-agent:").returncode, 0)
        # It left its socket too.
        self.sock.unlink()
        self.relaunch()
        self.request(op="submit", text="hello")
        self.assertEqual(self.events.until("reply")[-1]["text"], "echo: hello")
        windows = self.tmux("list-windows", "-t", "=claude-agent:", "-F", "#{window_index}")
        self.assertEqual(len(windows.stdout.split()), 1)

    def test_a_session_with_no_supervisor_named_is_not_taken_over(self) -> None:
        # Someone else's, or one a 1.1 supervisor left: it may be anything.
        self.stop()
        self.tmux("new-session", "-d", "-s", "claude-agent", "sleep 600")
        env = {**tmux_env(self.dir / "tmux"), "CLAUDE_AGENT_RUNTIME_DIR": str(self.dir / "run2")}
        second = subprocess.run(
            [sys.executable, str(SUPERVISOR)], env=env, capture_output=True, text=True, timeout=30
        )
        self.assertEqual(second.returncode, 1)
        self.assertIn("already exists", second.stderr)

    def test_a_tmux_that_stops_answering_does_not_stall_the_socket(self) -> None:
        server = int(self.tmux("display-message", "-p", "#{pid}").stdout)
        os.kill(server, signal.SIGSTOP)
        try:
            # Across several pane polls, each of which would wait on tmux.
            for _ in range(4):
                started = time.monotonic()
                self.assertEqual(self.request(op="status")["state"], "idle")
                self.assertLess(time.monotonic() - started, 1.0)
                time.sleep(0.5)
        finally:
            os.kill(server, signal.SIGCONT)
        self.request(op="submit", text="after")
        self.assertEqual(self.events.until("reply")[-1]["text"], "echo: after")

    def test_exiting_ends_the_tmux_session(self) -> None:
        self.request(op="submit", text="exit")
        self.assertEqual(self.proc.wait(10), 3)
        self.assertNotEqual(self.tmux("has-session", "-t", "=claude-agent:").returncode, 0)

    def test_status_says_where_to_attach(self) -> None:
        status = self.request(op="status")
        self.assertEqual(
            (status["terminal"], status["tmux_socket"], status["tmux_session"]),
            ("tmux", "claude-agent", "claude-agent"),
        )
        self.assertFalse(self.request(op="attach")["ok"])


class RespawnTest(unittest.TestCase):
    """CLAUDE_AGENT_RESPAWN: Claude Code is started again and the socket stays."""

    CASE = SupervisorTest

    def setUp(self) -> None:
        self.case = self.CASE("test_exits_with_claudes_status")
        self.addCleanup(self.case.doCleanups)
        self.case.launch("--permission-mode", "bypassPermissions", CLAUDE_AGENT_RESPAWN="2")

    def again(self) -> None:
        seen = self.case.events.until("idle")
        self.assertIsNone(self.case.proc.poll())
        self.assertNotIn("exited", [event["event"] for event in seen])
        # The bypass warning is answered again, by the new Claude Code.
        self.case.request(op="submit", text="hello again")
        self.assertEqual(self.case.events.until("reply")[-1]["text"], "echo: hello again")

    def test_sigterm_ends_it_for_good(self) -> None:
        self.case.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.case.events.until("exited")[-1]["event"], "exited")
        self.case.proc.wait(10)
        self.assertFalse(self.case.sock.exists())

    def test_an_exit_starts_it_again(self) -> None:
        self.case.request(op="submit", text="exit")
        self.assertEqual(self.case.events.until("exited")[-1]["status"], 3)
        self.again()

    def test_a_killed_claude_starts_again(self) -> None:
        pid = self.case.request(op="status")["pid"]
        # 0 or 1 would signal this process group, or init.
        self.assertGreater(pid, 1)
        os.kill(pid, signal.SIGKILL)
        self.assertEqual(self.case.events.until("exited")[-1]["status"], -signal.SIGKILL)
        self.again()


@NEEDS_TMUX
class TmuxRespawnTest(RespawnTest):
    CASE = TmuxSupervisorTest

    def test_a_killed_pane_starts_again(self) -> None:
        self.case.tmux("kill-pane", "-t", "=claude-agent:")
        # Gone with its pane, it leaves no status to read.
        self.assertEqual(self.case.events.until("exited")[-1]["status"], 1)
        self.again()

    def test_a_killed_pane_beside_another_window_starts_in_a_new_one(self) -> None:
        self.case.tmux("new-window", "-d", "-t", "=claude-agent:", "sleep 600")
        self.case.tmux("kill-pane", "-t", "=claude-agent:0")
        self.case.events.until("exited")
        self.again()
        windows = self.case.tmux("list-windows", "-t", "=claude-agent:", "-F", "#{window_index}")
        self.assertEqual(len(windows.stdout.split()), 2)

    def test_a_failed_restart_is_tried_again(self) -> None:
        # tmux on PATH is a stand-in that, once armed, refuses every call up to
        # and including the first that would start Claude Code.
        self.case.doCleanups()
        stub = tempfile.TemporaryDirectory()
        self.addCleanup(stub.cleanup)
        down = Path(stub.name) / "down"
        (Path(stub.name) / "tmux").write_text(
            "#!/bin/sh\n"
            f'if [ -e "{down}" ]; then\n'
            f'  case " $* " in *" respawn-pane "*|*" new-window "*|*" new-session "*) rm -f "{down}";; esac\n'
            "  exit 1\n"
            "fi\n"
            f'exec {shutil.which("tmux")} "$@"\n'
        )
        (Path(stub.name) / "tmux").chmod(0o755)
        path = f"{stub.name}:{os.environ['PATH']}"
        self.case.launch("--permission-mode", "bypassPermissions", CLAUDE_AGENT_RESPAWN="2", PATH=path)
        self.case.request(op="submit", text="exit")
        self.case.events.until("exited")
        down.touch()
        self.again()
        self.assertFalse(down.exists())

    def test_sigterm_ends_the_tmux_session(self) -> None:
        self.case.proc.send_signal(signal.SIGTERM)
        self.case.proc.wait(10)
        self.assertNotEqual(self.case.tmux("has-session", "-t", "=claude-agent:").returncode, 0)


class StartupTest(unittest.TestCase):
    """The two startup screens the supervisor reads."""

    CASE = SupervisorTest

    def start(self, *args: str, **extra: str) -> SupervisorTest:
        case = self.CASE("test_exits_with_claudes_status")
        self.addCleanup(case.doCleanups)
        case.launch(*args, **extra)
        return case

    def test_an_untrusted_folder_is_reported_once_and_never_answered(self) -> None:
        case = self.start(FAKE_CLAUDE_UNTRUSTED="1")
        notice = case.events.until("notice")[-1]
        self.assertIn("not trusted", notice["text"])
        # A subscriber that connects afterwards still hears it.
        late = Events(case.sock)
        self.assertIn("not trusted", late.until("notice")[-1]["text"])
        time.sleep(1)
        self.assertEqual(case.request(op="status")["state"], "starting")

    def test_bypass_mode_is_accepted_when_asked_for(self) -> None:
        case = self.start("--permission-mode", "bypassPermissions")
        case.request(op="submit", text="hello")
        self.assertEqual(case.events.until("reply")[-1]["text"], "echo: hello")

    def test_the_bypass_warning_is_left_alone_when_not_asked_for(self) -> None:
        case = self.start(FAKE_CLAUDE_BYPASS_WARNING="1", CLAUDE_AGENT_STARTUP_QUIET="2")
        # The first Enter typed takes the warning's default, "No, exit".
        case.request(op="submit", text="hello")
        self.assertEqual(case.events.until("exited")[-1]["status"], 1)


@NEEDS_TMUX
class TmuxStartupTest(StartupTest):
    CASE = TmuxSupervisorTest


class TerminalTest(unittest.TestCase):
    """tmux when it is installed, a pty when it is not, either when asked for."""

    def choose(self, path: str, asked: str = "") -> str:
        with mock.patch.dict(os.environ, {"PATH": path, "CLAUDE_AGENT_TERMINAL": asked}):
            module = runpy.run_path(str(SUPERVISOR))
            return type(module["terminal"](None, print)).__name__

    def test_no_tmux_is_a_pty(self) -> None:
        self.assertEqual(self.choose("/nonexistent"), "Pty")
        self.assertEqual(self.choose("/nonexistent", asked="tmux"), "Tmux")

    @NEEDS_TMUX
    def test_tmux_installed_is_tmux(self) -> None:
        path = os.environ["PATH"]
        self.assertEqual(self.choose(path), "Tmux")
        self.assertEqual(self.choose(path, asked="pty"), "Pty")

    def test_a_tmux_that_fails_to_run_is_a_failed_command(self) -> None:
        module = runpy.run_path(str(SUPERVISOR))
        hung = subprocess.TimeoutExpired(["tmux"], 10)
        for error in (FileNotFoundError("tmux"), hung):
            with mock.patch("subprocess.run", side_effect=error):
                self.assertEqual(module["tmux"]("has-session").returncode, 1)

    def test_a_signal_reaches_only_a_pid_the_pane_still_runs(self) -> None:
        term = runpy.run_path(str(SUPERVISOR))["Tmux"](None)
        term.pane = "%1"
        for shown, signalled in (
            ("%1:0:::4242", 4242),  # running: its pid gets the signal
            ("%1:1:3::4242", None),  # dead: the pid may be anyone's now
            ("%1:0:::1", None),  # init, or worse
            ("%1:0:::", None),  # no pid at all
            ("%2:0:::4242", None),  # another pane answered
            ("", None),  # gone
        ):
            calls: list[tuple[str, ...]] = []

            def tmux(*args: str, out: str = shown, calls: list = calls) -> subprocess.CompletedProcess[bytes]:
                calls.append(args)
                return subprocess.CompletedProcess(args, 0 if out else 1, out.encode(), b"")

            with mock.patch.dict(term.kill.__globals__, {"tmux": tmux}), mock.patch("os.kill") as kill:
                term.kill(signal.SIGTERM)
            if signalled:
                kill.assert_called_once_with(signalled, signal.SIGTERM)
            else:
                kill.assert_not_called()
                self.assertEqual(calls[-1], ("kill-pane", "-t", "%1"), shown)

    def test_a_reply_ends_only_where_tmux_ends_it(self) -> None:
        module = runpy.run_path(str(SUPERVISOR))
        with (
            mock.patch("subprocess.Popen", side_effect=OSError("no tmux")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            control = module["Control"](None, "%1")
        replies: list[tuple[bool, str]] = []
        control.waiting = [(0.0, lambda ok, text: replies.append((ok, text)))] * 2
        for line in (
            "%begin 1700000000 10 0",  # its own attach
            "%end 1700000000 10 0",
            "%session-changed $0 claude-agent",
            "%begin 1700000000 12 1",
            "%end 1700000000 99 1",  # on the screen, not tmux's
            "%end 1700000000 12",
            "line two",
            "%end 1700000000 12 1",
            "%begin 1700000000 13 1",
            "no such pane",
            "%error 1700000000 13 1",
        ):
            control.on_line(line)
        self.assertTrue(control.attached)
        self.assertEqual(
            replies, [(True, "%end 1700000000 99 1\n%end 1700000000 12\nline two"), (False, "no such pane")]
        )
        # An attach that failed: its pane is gone.
        for line in ("%begin 1700000001 1 0", "can't find pane: %1", "%error 1700000001 1 0"):
            control.on_line(line)
        self.assertFalse(control.attached)

    def supervisor(self, **env: str) -> Any:
        with mock.patch.dict(os.environ, {"CLAUDE_AGENT_TERMINAL": "pty", **env}):
            return runpy.run_path(str(SUPERVISOR))["Supervisor"](["claude"], Path("/nonexistent"))

    def test_a_startup_timer_that_outlives_its_claude_does_nothing(self) -> None:
        # Claude Code exits early in its startup and the respawn delay outlasts
        # the rest of its timers: they must not call a dead terminal idle, where
        # a held prompt would be pasted and lost.
        supervisor = self.supervisor(CLAUDE_AGENT_RESPAWN="60", CLAUDE_AGENT_STARTUP_QUIET="60")
        supervisor.alive = True
        supervisor.startup_timeout()
        supervisor.on_session_start({})
        timers = [action for _, action in supervisor.timers]
        supervisor.held.append("a held text")
        with mock.patch.object(supervisor, "emit"), mock.patch.object(supervisor, "paste") as paste:
            supervisor.reap(1)
            self.assertEqual(supervisor.state, "starting")
            for action in timers:
                action()
        paste.assert_not_called()
        self.assertEqual((supervisor.state, supervisor.held), ("starting", ["a held text"]))

    def test_a_startup_timer_of_a_running_claude_still_ends_the_wait(self) -> None:
        supervisor = self.supervisor()
        supervisor.alive = True
        supervisor.startup_timeout()
        with mock.patch.object(supervisor, "emit"):
            supervisor.timers[-1][1]()
        self.assertEqual(supervisor.state, "idle")

    def test_a_stop_with_no_claude_ready_holds_its_text_for_the_next(self) -> None:
        # Sent in the respawn gap, or while a Claude Code is still starting.
        for exits in (True, False):
            supervisor = self.supervisor(CLAUDE_AGENT_RESPAWN="30")
            supervisor.alive = True
            supervisor.term = mock.Mock()
            with mock.patch.object(supervisor, "emit"):
                if exits:
                    supervisor.reap(1)
                supervisor.stop("the next prompt")
                for _, action in [t for t in supervisor.timers if t[1] != supervisor.restart]:
                    action()
                self.assertEqual((supervisor.state, supervisor.held), ("starting", ["the next prompt"]))
                # The next Claude Code's startup screen is still read.
                supervisor.alive = True
                supervisor.term.poll.return_value = None
                supervisor.term.screen.side_effect = lambda then: then("")
                supervisor.next_screen_poll = 0.0
                with mock.patch.object(supervisor, "startup_dialogs") as dialogs:
                    supervisor.watch()
                dialogs.assert_called_once()

    def test_the_bypass_answer_goes_only_to_the_claude_that_asked(self) -> None:
        for exits in (False, True):
            supervisor = self.supervisor(CLAUDE_AGENT_RESPAWN="60")
            supervisor.argv.extend(["--permission-mode", "bypassPermissions"])
            supervisor.alive = True
            with mock.patch.object(supervisor, "emit"), mock.patch.object(supervisor, "write") as write:
                supervisor.startup_dialogs("Bypass Permissions mode")
                supervisor.timers.pop()[1]()  # DOWN, and ENTER set for later
                if exits:
                    supervisor.reap(1)
                supervisor.timers.pop(0)[1]()  # ENTER, set before any reap's restart
            keys = [call.args[0] for call in write.call_args_list]
            self.assertEqual(keys, [b"\x1b[B"] if exits else [b"\x1b[B", b"\r"])

    def test_respawn_has_a_floor(self) -> None:
        # Below it, a key timed for the Claude Code that ended could reach the
        # next one: respawn-pane reuses the pane.
        with contextlib.redirect_stderr(io.StringIO()) as err:
            supervisor = self.supervisor(CLAUDE_AGENT_RESPAWN="0.5")
        self.assertEqual(supervisor.respawn, 2.0)
        self.assertIn("CLAUDE_AGENT_RESPAWN", err.getvalue())
        self.assertEqual(self.supervisor(CLAUDE_AGENT_RESPAWN="7").respawn, 7.0)

    def keys_after(
        self, act: Callable[[Any], None], ends: bool, state: str = "idle"
    ) -> tuple[Any, list[bytes]]:
        """The keys typed by act and every timer it set, Claude Code ending (or not) between."""
        supervisor = self.supervisor(CLAUDE_AGENT_RESPAWN="60")
        supervisor.alive, supervisor.state = True, state
        supervisor.term = mock.Mock()
        with mock.patch.object(supervisor, "emit"):
            act(supervisor)
            if ends:
                supervisor.reap(1)
            while timers := [t for t in supervisor.timers if t[1] != supervisor.restart]:
                supervisor.timers = [t for t in supervisor.timers if t not in timers]
                for _, action in timers:
                    action()
        return supervisor, [call.args[0] for call in supervisor.term.write.call_args_list]

    def test_a_pastes_enter_goes_only_to_the_claude_it_was_pasted_into(self) -> None:
        paste = b"\x1b[200~hello\x1b[201~"
        for ends, keys in ((False, [paste, b"\r"]), (True, [paste])):
            _, typed = self.keys_after(lambda s: s.submit("hello"), ends)
            self.assertEqual(typed, keys, ends)

    def test_a_stops_escape_goes_only_to_a_claude_that_is_running_a_turn(self) -> None:
        for ends, state, keys in (
            (False, "busy", [b"\x1b"]),
            (True, "busy", []),
            # Escape on a startup screen answers it, or exits.
            (False, "starting", []),
        ):
            _, typed = self.keys_after(lambda s: s.stop(""), ends, state)
            self.assertEqual(typed, keys, (ends, state))

    def test_an_answer_whose_claude_ended_is_held_for_the_next(self) -> None:
        def answer(supervisor: Any) -> None:
            supervisor.dialog = "AskUserQuestion"
            supervisor.submit("A please")

        supervisor, typed = self.keys_after(answer, True, "busy")
        self.assertEqual((typed, supervisor.held), ([b"\x1b"], ["A please"]))
        supervisor, typed = self.keys_after(answer, False, "busy")
        self.assertEqual((typed[-1], supervisor.held), (b"\r", []))

    def test_a_tmux_that_cannot_start_claude_ends_the_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **tmux_env(Path(tmp)),
                "PATH": "/nonexistent",
                "CLAUDE_AGENT_TERMINAL": "tmux",
                "CLAUDE_AGENT_CLAUDE": str(FAKE),
                "CLAUDE_AGENT_RUNTIME_DIR": f"{tmp}/run",
            }
            ended = subprocess.run(
                [sys.executable, str(SUPERVISOR)], env=env, capture_output=True, text=True, timeout=30
            )
            self.assertEqual(ended.returncode, 1)
            self.assertIn("could not start claude in tmux", ended.stderr)
            self.assertFalse(Path(f"{tmp}/run/control.sock").exists())


class ResultsTest(unittest.TestCase):
    """Command results read from a transcript, in the orders Claude Code writes them."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "t.jsonl"
        self.path.write_text("")
        self.emitted: list[dict] = []
        module = runpy.run_path(str(SUPERVISOR))
        self.results = module["Results"](self.emitted.append, timeout=60)
        self.results.transcript(str(self.path))

    def write(self, *records: dict) -> None:
        with self.path.open("a") as out:
            for record in records:
                out.write(json.dumps(record) + "\n")

    def compact(self, summary_first: bool) -> list[str]:
        self.results.expect("compact")
        boundary: dict[str, object] = {
            "type": "system",
            "subtype": "compact_boundary",
            "compactMetadata": {"preTokens": 50160, "postTokens": 6588, "durationMs": 13176},
        }
        summary: dict[str, object] = {
            "type": "user",
            "isCompactSummary": True,
            "message": {"content": "Continued.\n\nSummary:\n1. Intent: " + "x" * 3100},
        }
        self.write(
            *([summary, boundary] if summary_first else [boundary, summary]),
            {"type": "user", "message": {"content": "<command-name>/compact</command-name>"}},
            {
                "type": "user",
                "message": {"content": "<local-command-stdout>Compacted</local-command-stdout>"},
            },
        )
        self.results.poll()
        return self.emitted[-1]["texts"]

    def test_compact_reports_tokens_and_trimmed_summary_either_order(self) -> None:
        for summary_first in (False, True):
            texts = self.compact(summary_first)
            self.assertEqual(texts[0], "Compacted 50,160 → 6,588 tokens in 13s.")
            self.assertTrue(texts[1].startswith("1. Intent: xxx"))
            self.assertTrue(texts[1].endswith("… (trimmed, 3,111 chars)"))

    def test_a_command_with_no_result_times_out(self) -> None:
        self.results.timeout = 0
        self.results.expect("fast")
        self.results.poll()
        self.assertEqual(
            self.emitted,
            [{"event": "result", "command": "fast", "texts": ["No result from /fast within 1 min."]}],
        )


class SendTest(unittest.TestCase):
    """A client that cannot take a whole message is dropped, never waited on."""

    class Client:
        def __init__(self, takes: int | None) -> None:
            self.takes = takes  # bytes accepted per send; None raises
            self.closed = False

        def send(self, payload: bytes) -> int:
            if self.takes is None:
                raise BlockingIOError
            return min(self.takes, len(payload))

        def fileno(self) -> int:
            return -1

        def close(self) -> None:
            self.closed = True

    def setUp(self) -> None:
        module = runpy.run_path(str(SUPERVISOR))
        self.supervisor = module["Supervisor"](["claude"], Path("/nonexistent/control.sock"))

    def check(self, takes: int | None, dropped: bool) -> None:
        client = self.Client(takes)
        self.supervisor.subscribers.append(client)
        self.supervisor.emit({"event": "busy"})
        self.assertEqual(client.closed, dropped)
        self.assertEqual(client in self.supervisor.subscribers, not dropped)

    def test_a_whole_write_keeps_the_client(self) -> None:
        self.check(takes=1 << 20, dropped=False)

    def test_a_short_write_drops_it(self) -> None:
        self.check(takes=3, dropped=True)

    def test_a_full_buffer_drops_it(self) -> None:
        self.check(takes=None, dropped=True)


if __name__ == "__main__":
    unittest.main()
