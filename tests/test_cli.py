"""The claude-agent command line: version, status, usage and exit codes."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CLI = Path(__file__).resolve().parent.parent / "root/usr/bin/claude-agent"


def run(*args: str, path: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": path if path is not None else os.environ["PATH"]}
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

    def test_status_without_claude_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            result = run("status", path=empty)
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stdout, r"claude: +not found")


if __name__ == "__main__":
    unittest.main()
