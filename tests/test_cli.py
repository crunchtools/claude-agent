"""The claude-agent command line: version, status, usage and exit codes."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

CLI = Path(__file__).resolve().parent.parent / "root/usr/bin/claude-agent"
FAKE = Path(__file__).resolve().parent / "fake_claude.py"
# Run from inside tmux, as on a seat, every tmux these tests start would
# otherwise default to the server holding the person's own session.
os.environ.pop("TMUX", None)
os.environ.pop("TMUX_PANE", None)


def run(*args: str, path: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **(env or {})}
    env["PATH"] = path if path is not None else os.environ["PATH"]
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
            env = {"CLAUDE_AGENT_RUNTIME_DIR": runtime, "TMUX_TMPDIR": runtime}
            for args in (["send", "hi"], ["stop"], ["attach"]):
                result = run(*args, env=env)
                self.assertEqual(result.returncode, 1, args)
                self.assertIn("no session is running", result.stderr)

    def test_run_holds_a_session_that_send_reaches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CLAUDE_AGENT_RUNTIME_DIR": tmp,
                "CLAUDE_AGENT_CLAUDE": str(FAKE),
                "FAKE_CLAUDE_DIR": tmp,
                "TMUX_TMPDIR": tmp,
            }
            agent = subprocess.Popen(
                [sys.executable, str(CLI), "run"],
                env={**os.environ, **env},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.time() + 10
                while time.time() < deadline and "idle" not in run("status", env=env).stdout:
                    time.sleep(0.2)
                result = run("send", "--wait", "hello", env=env)
                self.assertEqual((result.returncode, result.stdout), (0, "echo: hello\n"))
                if shutil.which("tmux"):
                    self.check_tmux_attach(Path(tmp), env)
                self.assertEqual(run("send", "exit", env=env).returncode, 0)
                self.assertEqual(agent.wait(10), 3)
            finally:
                if agent.poll() is None:
                    agent.terminate()
                    agent.wait(10)
                if shutil.which("tmux"):
                    server = ["tmux", "-L", "claude-agent", "kill-server"]
                    subprocess.run(server, env=env | {"PATH": os.environ["PATH"]}, check=False)

    def check_tmux_attach(self, tmp: Path, env: dict) -> None:
        """attach runs tmux on the session's own server; a stand-in tmux shows how."""
        stub = tmp / "bin"
        stub.mkdir()
        (stub / "tmux").write_text('#!/bin/sh\necho "$@"\n')
        (stub / "tmux").chmod(0o755)
        path = f"{stub}:{os.environ['PATH']}"
        target = "-L claude-agent attach-session -t =claude-agent:"
        self.assertEqual(run("attach", env=env, path=path).stdout, f"{target} -r\n")
        self.assertEqual(run("attach", "--rw", env=env, path=path).stdout, f"{target}\n")

    def start(self, tmp: str, **extra: str) -> subprocess.Popen[str]:
        # TMUX_TMPDIR: a tmux server under tmp, not the shared one in
        # /tmp/tmux-UID that a real claude-agent on this host uses.
        env = {
            "CLAUDE_AGENT_RUNTIME_DIR": tmp,
            "CLAUDE_AGENT_CLAUDE": str(FAKE),
            "FAKE_CLAUDE_DIR": tmp,
            "TMUX_TMPDIR": tmp,
            **extra,
        }
        agent = subprocess.Popen(
            [sys.executable, str(CLI), "run"],
            env={**os.environ, **env},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        def reap() -> None:
            # SIGTERM, which the CLI passes on: SIGKILL would leave its
            # supervisor running with nobody to stop it.
            if agent.poll() is None:
                agent.terminate()
                try:
                    agent.wait(10)
                except subprocess.TimeoutExpired:
                    agent.kill()
            if shutil.which("tmux"):
                server = ["tmux", "-L", "claude-agent", "kill-server"]
                subprocess.run(
                    server, env={**os.environ, "TMUX_TMPDIR": tmp}, capture_output=True, check=False
                )

        self.addCleanup(reap)
        deadline = time.time() + 10
        while time.time() < deadline and "idle" not in run("status", env=env).stdout:
            time.sleep(0.2)
        return agent

    def test_send_wait_waits_for_its_own_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CLAUDE_AGENT_RUNTIME_DIR": tmp}
            agent = self.start(tmp)
            self.assertEqual(run("send", "slow 2", env=env).returncode, 0)
            time.sleep(0.5)
            result = run("send", "--wait", "mine", env=env)
            self.assertEqual(result.stdout, "echo: mine\n")
            run("send", "exit", env=env)
            agent.wait(10)
            assert agent.stderr is not None  # stderr=PIPE
            agent.stderr.close()

    def test_run_restarts_a_bridge_that_dies_and_stops_it_at_the_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # No token file: the bridge exits at once, every time.
            agent = self.start(
                tmp,
                CLAUDE_AGENT_MATRIX_HOMESERVER="http://127.0.0.1:9",
                CLAUDE_AGENT_MATRIX_TOKEN_FILE=str(Path(tmp) / "missing"),
            )
            time.sleep(6.5)
            run("send", "exit", env={"CLAUDE_AGENT_RUNTIME_DIR": tmp})
            self.assertEqual(agent.wait(10), 3)
            assert agent.stderr is not None  # stderr=PIPE
            log = agent.stderr.read()
            agent.stderr.close()
            self.assertGreaterEqual(log.count("the Matrix bridge exited (1), restarting"), 1)

    def test_status_without_claude_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            result = run("status", path=empty)
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stdout, r"claude: +not found")


if __name__ == "__main__":
    unittest.main()
