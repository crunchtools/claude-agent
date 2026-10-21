"""The supervisor, driving a stand-in Claude Code (fake_claude.py) through its socket."""

from __future__ import annotations

import json
import os
import runpy
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TOP = Path(__file__).resolve().parent.parent
SUPERVISOR = TOP / "root/usr/libexec/claude-agent/supervisor.py"
FAKE = Path(__file__).resolve().parent / "fake_claude.py"


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
    def setUp(self) -> None:
        self.launch()

    def launch(self, *args: str, **extra: str) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.sock = self.dir / "run/control.sock"
        env = {**os.environ, "CLAUDE_AGENT_CLAUDE": str(FAKE),
               "CLAUDE_AGENT_RUNTIME_DIR": str(self.dir / "run"),
               "FAKE_CLAUDE_DIR": str(self.dir), "CLAUDE_AGENT_STARTUP_QUIET": "20",
               "CLAUDE_AGENT_RESULT_TIMEOUT": "5", "CLAUDECODE": "1", **extra}
        env.pop("CLAUDE_AGENT_SOCKET", None)
        self.proc = subprocess.Popen([sys.executable, str(SUPERVISOR), *args], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
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

    def test_exits_with_claudes_status(self) -> None:
        self.request(op="submit", text="exit")
        self.assertEqual(self.events.until("exited")[-1]["status"], 3)
        self.assertEqual(self.proc.wait(10), 3)
        self.assertFalse(self.sock.exists())


class StartupTest(unittest.TestCase):
    """The two startup screens the supervisor reads."""

    def start(self, *args: str, **extra: str) -> SupervisorTest:
        case = SupervisorTest("test_exits_with_claudes_status")
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
        boundary = {"type": "system", "subtype": "compact_boundary",
                    "compactMetadata": {"preTokens": 50160, "postTokens": 6588, "durationMs": 13176}}
        summary = {"type": "user", "isCompactSummary": True,
                   "message": {"content": "Continued.\n\nSummary:\n1. Intent: " + "x" * 3100}}
        self.write(*([summary, boundary] if summary_first else [boundary, summary]),
                   {"type": "user", "message": {"content": "<command-name>/compact</command-name>"}},
                   {"type": "user", "message": {
                       "content": "<local-command-stdout>Compacted</local-command-stdout>"}})
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
        self.assertEqual(self.emitted, [{"event": "result", "command": "fast",
                                         "texts": ["No result from /fast within 1 min."]}])


if __name__ == "__main__":
    unittest.main()
