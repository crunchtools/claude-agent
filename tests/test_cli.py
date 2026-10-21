"""The claude-agent command line: version, status, usage and exit codes."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

CLI = Path(__file__).resolve().parent.parent / "root/usr/bin/claude-agent"
FAKE = Path(__file__).resolve().parent / "fake_claude.py"


def run(*args: str, path: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": path if path is not None else os.environ["PATH"], **(env or {})}
    return subprocess.run(
        [sys.executable, str(CLI), *args], capture_output=True, text=True, env=env, check=False
    )


class CliTest(unittest.TestCase):
    def test_version(self) -> None:
        result = run("--version")
        self.assertEqual(result.returncode, 0)
        self.assertRegex(result.stdout, r"^claude-agent \d+\.\d+\.\d+\n$")

    def test_usage_error_exits_2(self) -> None:
        result = run("frobnicate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("claude-agent status", result.stderr)

    def test_requests_without_a_session_exit_1(self) -> None:
        with tempfile.TemporaryDirectory() as runtime:
            env = {"CLAUDE_AGENT_RUNTIME_DIR": runtime}
            for args in (["send", "hi"], ["stop"], ["attach"]):
                result = run(*args, env=env)
                self.assertEqual(result.returncode, 1, args)
                self.assertIn("no session is running", result.stderr)

    def test_run_holds_a_session_that_send_reaches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CLAUDE_AGENT_RUNTIME_DIR": tmp, "CLAUDE_AGENT_CLAUDE": str(FAKE), "FAKE_CLAUDE_DIR": tmp}
            agent = subprocess.Popen([sys.executable, str(CLI), "run"], env={**os.environ, **env},
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.time() + 10
                while time.time() < deadline and "idle" not in run("status", env=env).stdout:
                    time.sleep(0.2)
                result = run("send", "--wait", "hello", env=env)
                self.assertEqual((result.returncode, result.stdout), (0, "echo: hello\n"))
                self.assertEqual(run("send", "exit", env=env).returncode, 0)
                self.assertEqual(agent.wait(10), 3)
            finally:
                if agent.poll() is None:
                    agent.kill()

    def test_status_without_claude_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            result = run("status", path=empty)
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stdout, r"claude: +not found")


if __name__ == "__main__":
    unittest.main()
