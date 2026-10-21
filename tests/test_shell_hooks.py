"""The two shell hooks the packages ship, run as Claude Code runs them."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

LIBEXEC = Path(__file__).resolve().parent.parent / "root/usr/libexec/claude-agent"


def run(script: str, event: dict[str, object], **env: str) -> dict[str, object] | None:
    """Run a hook on one event; return its decision, or None if it gave none."""
    result = subprocess.run(
        ["bash", str(LIBEXEC / script)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], **env},
        check=True,
    )
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)["hookSpecificOutput"]


# An agent session with prompts off.
ON = {"CLAUDE_AGENT": "1", "CLAUDE_AGENT_ANSWER_PERMISSIONS": "1"}


class AllowHookTest(unittest.TestCase):
    def test_answers_nothing_unless_turned_on(self) -> None:
        self.assertIsNone(run("allow-hook.sh", {"tool_name": "Bash", "permission_mode": "default"}))

    def test_allows_any_tool_in_any_mode(self) -> None:
        for mode in ("plan", "acceptEdits", "default"):
            for tool in ("Bash", "Write", "mcp__trentina__merge_pull_request"):
                event = {"tool_name": tool, "permission_mode": mode}
                decision = run("allow-hook.sh", event, **ON)
                self.assertIsNotNone(decision, (mode, tool))
                self.assertEqual(decision["permissionDecision"], "allow")

    def test_answers_the_permission_request_in_any_mode(self) -> None:
        for mode in ("plan", "acceptEdits", "default"):
            for tool in ("Bash", "Write", "mcp__trentina__merge_pull_request"):
                event = {"hook_event_name": "PermissionRequest", "tool_name": tool, "permission_mode": mode}
                decision = run("allow-hook.sh", event, **ON)
                self.assertIsNotNone(decision, (mode, tool))
                self.assertEqual(decision["hookEventName"], "PermissionRequest")
                self.assertEqual(decision["decision"], {"behavior": "allow"})

    def test_answers_nothing_outside_an_agent(self) -> None:
        event = {"tool_name": "Bash", "permission_mode": "default"}
        self.assertIsNone(run("allow-hook.sh", event, CLAUDE_AGENT_ANSWER_PERMISSIONS="1"))

    def test_permission_request_answered_only_when_turned_on(self) -> None:
        event = {"hook_event_name": "PermissionRequest", "tool_name": "Write", "permission_mode": "default"}
        self.assertIsNone(run("allow-hook.sh", event))

    def test_questions_and_plan_approval_reach_the_person_and_are_logged(self) -> None:
        with tempfile.TemporaryDirectory() as audit:
            for hook_event in ("PreToolUse", "PermissionRequest"):
                for tool in ("AskUserQuestion", "ExitPlanMode"):
                    event = {
                        "hook_event_name": hook_event,
                        "tool_name": tool,
                        "permission_mode": "plan",
                        "session_id": "s1",
                    }
                    self.assertIsNone(
                        run("allow-hook.sh", event, **ON, CLAUDE_AGENT_AUDIT_DIR=audit)
                    )
            lines = (Path(audit) / "allow-hook.jsonl").read_text().splitlines()
            self.assertEqual(
                [(json.loads(line)["event"], json.loads(line)["left_to_person"]) for line in lines],
                [
                    ("PreToolUse", "AskUserQuestion"),
                    ("PreToolUse", "ExitPlanMode"),
                    ("PermissionRequest", "AskUserQuestion"),
                    ("PermissionRequest", "ExitPlanMode"),
                ],
            )


class MemoryGuardTest(unittest.TestCase):
    MEMORY = "/home/agent/.claude/projects/-home-agent-work/memory/note.md"

    def decision(self, tool: str, **tool_input: str) -> str | None:
        answer = run("deny-local-memory.sh", {"tool_name": tool, "tool_input": tool_input})
        return None if answer is None else str(answer["permissionDecision"])

    def test_writes_into_local_memory_are_denied(self) -> None:
        self.assertEqual(self.decision("Write", file_path=self.MEMORY), "deny")
        self.assertEqual(self.decision("Bash", command=f"echo x > {self.MEMORY}"), "deny")
        self.assertEqual(self.decision("Bash", command=f"tee -a {self.MEMORY} < /dev/null"), "deny")

    def test_reads_and_other_paths_pass(self) -> None:
        self.assertIsNone(self.decision("Bash", command=f"cat {self.MEMORY}"))
        self.assertIsNone(self.decision("Write", file_path="/home/agent/work/notes.md"))


if __name__ == "__main__":
    unittest.main()
