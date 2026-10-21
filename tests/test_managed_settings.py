"""The managed-settings fragments the packages ship, checked for what must and must not be there."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "root"
FRAGMENTS = ROOT / "etc/claude-code/managed-settings.d"
LIBEXEC = "/usr/libexec/claude-agent/"


def load(name: str) -> dict:
    return json.loads((FRAGMENTS / name).read_text())


def commands(settings: dict, event: str) -> dict[str, str]:
    """Each hook command of an event, mapped to its matcher."""
    return {
        hook["command"]: entry.get("matcher", "")
        for entry in settings.get("hooks", {}).get(event, [])
        for hook in entry["hooks"]
    }


class CoreFragmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load("10-claude-agent.json")

    def test_breaker_and_audit_run_on_every_tool_call(self) -> None:
        self.assertEqual(commands(self.settings, "PreToolUse")[LIBEXEC + "hooks.py breaker"], "")
        self.assertEqual(commands(self.settings, "PostToolUse")[LIBEXEC + "hooks.py audit"], "")

    def test_allow_hook_answers_the_prompt_itself(self) -> None:
        # A PreToolUse allow does not answer a prompt the session's mode
        # still calls for; PermissionRequest is that prompt.
        self.assertEqual(commands(self.settings, "PreToolUse")[LIBEXEC + "allow-hook.sh"], "")
        self.assertEqual(commands(self.settings, "PermissionRequest")[LIBEXEC + "allow-hook.sh"], "")

    def test_the_supervisor_hears_every_turn_event(self) -> None:
        for event in ("SessionStart", "UserPromptSubmit", "Stop", "StopFailure", "Notification",
                      "PreToolUse", "PostToolUse"):
            self.assertIn(LIBEXEC + "hooks.py event", commands(self.settings, event), event)

    def test_core_sets_no_machine_wide_policy(self) -> None:
        # Installed on a workstation, the core package must leave the
        # person's own Claude Code alone: its hooks are gated on
        # CLAUDE_AGENT=1, and it carries no policy keys at all.
        self.assertEqual(set(self.settings), {"hooks"})


class TrentinaFragmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load("20-claude-agent-trentina.json")

    def test_the_gateway_is_the_only_mcp_server(self) -> None:
        self.assertEqual(self.settings["allowedMcpServers"], [{"serverName": "trentina"}])
        self.assertTrue(self.settings["disableClaudeAiConnectors"])

    def test_only_managed_hooks_run(self) -> None:
        # A hook in the home volume or in a checkout's .claude/settings.json
        # would otherwise run with prompts off.
        self.assertIs(self.settings["allowManagedHooksOnly"], True)

    def test_memory_guard_is_registered_on_writes(self) -> None:
        matcher = commands(self.settings, "PreToolUse")[LIBEXEC + "deny-local-memory.sh"].split("|")
        self.assertTrue({"Bash", "Write", "Edit"} <= set(matcher))

    def test_web_tools_denied_and_auto_memory_off(self) -> None:
        self.assertEqual(sorted(self.settings["permissions"]["deny"]), ["WebFetch", "WebSearch"])
        self.assertIs(self.settings["autoMemoryEnabled"], False)

    def test_remote_control_can_fetch_its_feature_flags(self) -> None:
        # Remote Control exits at start when this is set: it disables the
        # feature-flag fetch along with telemetry.
        self.assertNotIn("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", self.settings["env"])


class EveryHookIsShippedTest(unittest.TestCase):
    def test_every_command_names_a_packaged_script(self) -> None:
        for fragment in sorted(FRAGMENTS.glob("*.json")):
            settings = json.loads(fragment.read_text())
            for event in settings.get("hooks", {}):
                for command in commands(settings, event):
                    self.assertTrue(command.startswith(LIBEXEC), command)
                    self.assertTrue((ROOT / command.split()[0].lstrip("/")).is_file(), command)


if __name__ == "__main__":
    unittest.main()
