"""The audit and circuit-breaker hooks, run the way Claude Code runs them."""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "root/usr/libexec/claude-agent/hooks.py"

# root/ mirrors the install tree; keep the import from writing bytecode into it.
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("agent_hooks", HOOKS)
hooks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hooks)


def run_hook(mode: str, event: object, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    payload = event if isinstance(event, str) else json.dumps(event)
    return subprocess.run(
        [sys.executable, str(HOOKS), mode],
        input=payload,
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_AGENT": "1", **env},
        check=False,
    )


def read_records(directory: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for path in sorted(directory.glob("audit-*.jsonl"))
        for line in path.read_text().splitlines()
    ]


class AuditTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.env = {"CLAUDE_AGENT_AUDIT_DIR": tmp.name}

    def test_record_has_the_profile_fields(self) -> None:
        event = {
            "session_id": "abc",
            "tool_name": "mcp__trentina__memory_search",
            "tool_input": {"query": "rotv"},
            "tool_response": {"content": "..."},
        }
        self.assertEqual(run_hook("audit", event, self.env).returncode, 0)
        (record,) = read_records(self.dir)
        self.assertEqual(
            set(record),
            {"timestamp", "tool", "server", "args", "result_summary", "token_cost", "conversation_id"},
        )
        self.assertEqual((record["server"], record["tool"]), ("trentina", "memory_search"))
        self.assertEqual(record["result_summary"], "success")
        self.assertEqual(record["conversation_id"], "abc")

    def test_builtin_tool_and_error_result(self) -> None:
        event = {
            "session_id": "s",
            "tool_name": "Bash",
            "tool_input": {"command": "false"},
            "tool_response": {"is_error": True},
        }
        run_hook("audit", event, self.env)
        (record,) = read_records(self.dir)
        self.assertEqual(
            (record["server"], record["tool"], record["result_summary"]), ("claude-code", "Bash", "error")
        )

    def test_credentials_never_reach_the_log(self) -> None:
        github_token = "ghp_" + "a" * 36
        event = {
            "session_id": "s",
            "tool_name": "Bash",
            "tool_input": {
                "command": "curl -H 'Authorization: Bearer abcdefghijklmnop123456' https://example.com",
                "api_key": "plain-value",
                "nested": {"password": "hunter2", "note": github_token},
            },
        }
        run_hook("audit", event, self.env)
        text = "".join(path.read_text() for path in self.dir.glob("*.jsonl"))
        for secret in ("abcdefghijklmnop123456", "plain-value", "hunter2", github_token):
            self.assertNotIn(secret, text)
        self.assertIn("Bearer [REDACTED]", text)

    def test_secrets_inside_commands_are_redacted(self) -> None:
        command = "curl -H 'X-API-Key: k3y-value' --password hunter2 https://example.com && TOKEN=abc123 run"
        text = hooks.redact(command)
        for secret in ("k3y-value", "hunter2", "abc123"):
            self.assertNotIn(secret, text)
        self.assertIn("https://example.com", text)

    def test_old_audit_files_are_pruned_when_a_day_starts(self) -> None:
        old = self.dir / "audit-2000-01-01.jsonl"
        old.write_text("{}\n")
        os.utime(old, (0, 0))
        run_hook("audit", {"session_id": "s", "tool_name": "Bash", "tool_input": {}}, self.env)
        self.assertFalse(old.exists())
        self.assertEqual(len(read_records(self.dir)), 1)

    def test_long_values_are_cut(self) -> None:
        self.assertEqual(hooks.redact("x" * 5000), "x" * hooks.MAX_VALUE_CHARS + "... [5000 chars]")

    def test_bad_input_is_reported_without_blocking(self) -> None:
        result = run_hook("audit", "not json", self.env)
        # Exit 2 is the one code Claude Code treats as blocking.
        self.assertNotIn(result.returncode, (0, 2))
        self.assertIn("JSONDecodeError", result.stderr)
        self.assertEqual(read_records(self.dir), [])

    def test_unknown_mode_is_an_error(self) -> None:
        self.assertEqual(run_hook("nonsense", {}, self.env).returncode, 1)


class BreakerTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "breaker"
        self.audit = Path(tmp.name) / "audit"
        self.env = {
            "CLAUDE_AGENT_BREAKER_DIR": str(self.state),
            "CLAUDE_AGENT_AUDIT_DIR": str(self.audit),
            "CLAUDE_AGENT_MAX_TOOL_CALLS": "6",
            "CLAUDE_AGENT_MAX_REPEAT": "3",
        }

    def call(self, command: str, session: str = "s1") -> str | None:
        """Run one call through the breaker; return the denial reason, if any."""
        event = {"session_id": session, "tool_name": "Bash", "tool_input": {"command": command}}
        result = run_hook("breaker", event, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout:
            return None
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        return decision["permissionDecisionReason"]

    def test_different_calls_pass_until_the_total(self) -> None:
        for n in range(6):
            self.assertIsNone(self.call(f"echo {n}"))
        self.assertIn("more than 6 tool calls", self.call("echo 6"))

    def test_same_call_in_a_row_trips(self) -> None:
        for _ in range(3):
            self.assertIsNone(self.call("ls"))
        self.assertIn("4 times in a row", self.call("ls"))

    def test_a_different_call_resets_the_run(self) -> None:
        for command in ("ls", "ls", "ls", "pwd", "ls", "ls"):
            self.assertIsNone(self.call(command))

    def test_tripped_stays_tripped_and_is_audited_once(self) -> None:
        for _ in range(4):
            self.call("ls")
        self.assertIn("4 times in a row", self.call("something else"))
        (record,) = read_records(self.audit)
        self.assertTrue(record["result_summary"].startswith("breaker_tripped"))

    def test_conversations_are_counted_apart(self) -> None:
        for _ in range(4):
            self.call("ls", session="a")
        self.assertIsNone(self.call("ls", session="b"))

    def test_unreadable_state_denies(self) -> None:
        self.state.mkdir(parents=True)
        (self.state / "s1.json").write_text("{not json")
        self.assertIn("breaker state unavailable", self.call("ls"))

    def test_unwritable_state_directory_denies(self) -> None:
        blocker = Path(self.state.parent) / "file"
        blocker.write_text("")
        self.env["CLAUDE_AGENT_BREAKER_DIR"] = str(blocker / "breaker")
        self.assertIn("breaker state unavailable", self.call("ls"))

    def test_default_state_directory_is_per_user_runtime(self) -> None:
        env = {k: v for k, v in self.env.items() if k != "CLAUDE_AGENT_BREAKER_DIR"}
        env["XDG_RUNTIME_DIR"] = str(self.state.parent / "run")
        self.env = env
        self.assertIsNone(self.call("ls"))
        self.assertTrue((self.state.parent / "run/claude-agent/breaker/s1.json").is_file())

    def test_stale_breaker_state_is_pruned(self) -> None:
        self.state.mkdir(parents=True)
        stale = self.state / "gone.json"
        stale.write_text("{}")
        os.utime(stale, (0, 0))
        self.call("ls", session="new")
        self.assertFalse(stale.exists())

    def test_session_id_cannot_leave_the_state_directory(self) -> None:
        self.call("ls", session="../../escape")
        self.assertEqual([p.name for p in self.state.iterdir()], ["escape.json"])

    def test_count_is_pure(self) -> None:
        state: dict[str, object] = {}
        self.assertIsNone(hooks.count(state, "a", 10, 2))
        self.assertIsNone(hooks.count(state, "a", 10, 2))
        self.assertIsNotNone(hooks.count(state, "a", 10, 2))


class EventTest(unittest.TestCase):
    """hooks.py event hands the turn's events to the supervisor's socket."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "control.sock"
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.server.close)
        self.server.bind(str(self.path))
        self.server.listen(1)
        self.server.settimeout(5)

    def received(self, event: dict) -> dict:
        result = run_hook("event", event, {"CLAUDE_AGENT_SOCKET": str(self.path)})
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        conn, _ = self.server.accept()
        with conn:
            return json.loads(conn.makefile().readline())

    def test_stop_carries_the_final_answer_and_nothing_else(self) -> None:
        message = self.received({
            "hook_event_name": "Stop", "session_id": "s", "last_assistant_message": "Done.",
            "transcript_path": "/t.jsonl", "cwd": "/home/a", "permission_mode": "default",
        })
        self.assertEqual(message, {"op": "event", "hook_event_name": "Stop", "session_id": "s",
                                   "transcript_path": "/t.jsonl", "last_assistant_message": "Done."})

    def test_only_a_dialog_tool_sends_its_input(self) -> None:
        question = {"questions": [{"question": "Which?"}]}
        asked = self.received({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                               "tool_input": question})
        self.assertEqual(asked["tool_input"], question)
        bash = self.received({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                              "tool_input": {"command": "cat secrets"}})
        self.assertNotIn("tool_input", bash)

    def test_no_socket_no_supervisor_nothing_sent(self) -> None:
        result = run_hook("event", {"hook_event_name": "Stop"}, {"CLAUDE_AGENT_SOCKET": ""})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_a_supervisor_that_is_gone_does_not_stop_the_turn(self) -> None:
        gone = {"CLAUDE_AGENT_SOCKET": str(self.path) + ".gone"}
        result = run_hook("event", {"hook_event_name": "Stop"}, gone)
        # 1 is reported and the turn goes on; 2 is the one code that would block it.
        self.assertEqual(result.returncode, 1)
        self.assertIn("hooks.py event", result.stderr)


class GateTest(unittest.TestCase):
    """Managed settings reach every session on the machine; only an agent's count."""

    def test_hooks_do_nothing_without_claude_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if k != "CLAUDE_AGENT"}
            env.update(CLAUDE_AGENT_AUDIT_DIR=tmp, CLAUDE_AGENT_BREAKER_DIR=tmp, CLAUDE_AGENT_MAX_REPEAT="1")
            event = json.dumps({"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "ls"}})
            for mode in ("breaker", "breaker", "audit"):
                result = subprocess.run([sys.executable, str(HOOKS), mode], input=event,
                                        capture_output=True, text=True, env=env, check=False)
                self.assertEqual((result.returncode, result.stdout), (0, ""))
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
