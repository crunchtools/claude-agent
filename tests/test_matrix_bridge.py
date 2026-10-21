"""The Matrix bridge, run against a stand-in homeserver and a stand-in supervisor."""

from __future__ import annotations

import json
import os
import queue
import runpy
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent.parent / "root/usr/libexec/claude-agent/matrix-bridge.py"
ME = "@agent1:example.org"
OWNER = "@alice:example.org"
STRANGER = "@mallory:example.org"
ROOM = "!dm:example.org"
GROUP = "!group:example.org"


def message(sender: str, body: str, room: str = ROOM) -> dict:
    return {"rooms": {"join": {room: {"timeline": {"events": [
        {"type": "m.room.message", "sender": sender, "content": {"msgtype": "m.text", "body": body}},
    ]}}}}}


def invite(sender: str, room: str) -> dict:
    return {"rooms": {"invite": {room: {"invite_state": {"events": [
        {"type": "m.room.member", "state_key": ME, "sender": sender, "content": {"membership": "invite"}},
    ]}}}}}


class FakeHomeserver:
    """Serves queued /sync batches, one per request, and records writes."""

    def __init__(self) -> None:
        self.batches: queue.Queue[dict] = queue.Queue()
        self.requests: list[tuple[str, str, dict]] = []
        self.count = 0
        self.members = {ROOM: [ME, OWNER], GROUP: [ME, OWNER, STRANGER]}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def reply(self, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if self.path.endswith("/account/whoami"):
                    self.reply({"user_id": ME})
                    return
                if self.path.endswith("/joined_members"):
                    room = urllib.parse.unquote(self.path.split("/rooms/")[1].split("/")[0])
                    if room not in fake.members:
                        self.send_error(500)
                        return
                    self.reply({"joined": {member: {} for member in fake.members[room]}})
                    return
                try:
                    batch = fake.batches.get(timeout=0.5)
                except queue.Empty:
                    batch = {}
                fake.count += 1
                self.reply({"next_batch": f"s{fake.count}", **batch})

            def write(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                fake.requests.append((self.command, self.path, body))
                self.reply({"event_id": "$sent"} if self.command == "PUT" else {"room_id": "x"})

            do_POST = write
            do_PUT = write

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # The bridge is killed mid long-poll at the end of each test.
        self.server.handle_error = lambda *args: None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def texts(self) -> list[str]:
        return [body["body"] for method, path, body in self.requests if "/send/m.room.message/" in path]

    def typing(self) -> list[bool]:
        return [body["typing"] for method, path, body in self.requests if "/typing/" in path]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class FakeSupervisor:
    """Records requests, answers them, and pushes events to subscribers."""

    def __init__(self, path: Path) -> None:
        self.requests: list[dict] = []
        self.answer: dict = {"ok": True}
        self.subscribers: list[socket.socket] = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(8)
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            request = json.loads(conn.makefile().readline())
            if request["op"] == "subscribe":
                conn.sendall(b'{"ok": true}\n')
                self.subscribers.append(conn)
                continue
            self.requests.append(request)
            conn.sendall(json.dumps(self.answer).encode() + b"\n")
            conn.close()

    def push(self, event: dict) -> None:
        for conn in self.subscribers:
            conn.sendall(json.dumps(event).encode() + b"\n")

    def close(self) -> None:
        self.server.close()
        for conn in self.subscribers:
            conn.close()


class MatrixBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "token").write_text("tok\n")
        self.hs = FakeHomeserver()
        self.addCleanup(self.hs.close)
        self.sup = FakeSupervisor(self.root / "control.sock")
        self.addCleanup(self.sup.close)
        self.start()

    def start(self, since: str | None = "s0") -> None:
        if since:
            (self.root / "sync").write_text(since)
        env = {
            **os.environ,
            "CLAUDE_AGENT_MATRIX_HOMESERVER": self.hs.url,
            "CLAUDE_AGENT_MATRIX_TOKEN_FILE": str(self.root / "token"),
            "CLAUDE_AGENT_MATRIX_ALLOW": f"{OWNER}, ",
            "CLAUDE_AGENT_MATRIX_STATE": str(self.root / "sync"),
            "CLAUDE_AGENT_SOCKET": str(self.root / "control.sock"),
        }
        self.proc = subprocess.Popen([sys.executable, str(BRIDGE)], env=env, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop)
        self.assertTrue(self.wait_for(lambda: self.sup.subscribers), "the bridge never subscribed")

    def restart(self, since: str | None, history: list[dict]) -> None:
        """Start again, with ``history`` waiting on the homeserver for the first sync."""
        self.stop()
        # The killed bridge's long-poll is still parked in the fake; let it lapse.
        time.sleep(1)
        (self.root / "sync").unlink(missing_ok=True)
        self.sup.subscribers.clear()
        for batch in history:
            self.hs.batches.put(batch)
        self.start(since)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

    def wait_for(self, predicate, timeout: float = 5) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def text(self, body: str, sender: str = OWNER) -> None:
        self.hs.batches.put(message(sender, body))

    def requested(self, count: int = 1) -> list[dict]:
        self.assertTrue(self.wait_for(lambda: len(self.sup.requests) >= count), self.sup.requests)
        return self.sup.requests

    def texted(self, count: int = 1) -> list[str]:
        self.assertTrue(self.wait_for(lambda: len(self.hs.texts()) >= count), self.hs.texts())
        return self.hs.texts()

    def test_a_text_becomes_a_prompt(self) -> None:
        self.text("check the deploy")
        self.assertEqual(self.requested(), [{"op": "submit", "text": "check the deploy"}])

    def test_strangers_are_dropped(self) -> None:
        self.text("hello", sender=STRANGER)
        self.text("from the owner")
        self.assertEqual(self.requested(), [{"op": "submit", "text": "from the owner"}])

    def test_shell_escapes_are_refused_here(self) -> None:
        self.text("!rm -rf /")
        self.assertEqual(self.texted(), ["Shell commands by text are off."])
        self.assertEqual(self.sup.requests, [])

    def test_allowed_commands_are_checked_and_relayed(self) -> None:
        self.text("/Effort High.")
        self.text("/fast maybe")
        self.assertEqual(self.requested(), [{"op": "command", "line": "/effort high"}])
        self.assertEqual(self.texted(), ["Usage: /fast on|off"])

    def test_other_slash_texts_run_as_skills_never_as_keys(self) -> None:
        self.text("/cc wrap up")
        self.text("/exit")
        self.assertEqual(self.requested(2), [{"op": "submit", "text": "Run /cc wrap up"},
                                             {"op": "submit", "text": "Run /exit"}])

    def test_stop_carries_its_text(self) -> None:
        self.text("/stop do this instead")
        self.assertEqual(self.requested(), [{"op": "stop", "text": "do this instead"}])

    def test_help_is_answered_here(self) -> None:
        self.text("/help")
        self.assertTrue(self.texted()[0].startswith("Commands by text: /stop [text], /clear"))

    def test_a_refusal_is_texted(self) -> None:
        self.sup.answer = {"ok": False, "error": "shell escapes are refused"}
        self.text("anything")
        self.assertEqual(self.texted(), ["Not sent: shell escapes are refused."])

    def test_a_turn_types_then_texts_its_final_answer(self) -> None:
        self.text("go")
        self.requested()
        self.sup.push({"event": "busy"})
        self.assertTrue(self.wait_for(lambda: self.hs.typing() == [True]))
        self.sup.push({"event": "heartbeat"})
        self.sup.push({"event": "reply", "text": "Deployed."})
        self.assertEqual(self.texted(), ["Deployed."])
        self.assertEqual(self.hs.typing(), [True, False])
        typing_path = [path for _, path, _ in self.hs.requests if "/typing/" in path][0]
        self.assertTrue(typing_path.endswith("/typing/%40agent1%3Aexample.org"))

    def test_results_questions_and_stops_are_texted(self) -> None:
        self.text("go")
        self.requested()
        self.sup.push({"event": "result", "command": "fast", "texts": ["/fast: Fast mode ON"]})
        self.sup.push({"event": "question", "tool": "AskUserQuestion",
                       "input": {"questions": [{"question": "Which env?", "options": [{"label": "dev"}]}]}})
        self.sup.push({"event": "stopped"})
        texts = self.texted(3)
        self.assertEqual(texts[0], "/fast: Fast mode ON")
        self.assertTrue(texts[1].startswith("Which env?\n- dev\n"))
        self.assertEqual(texts[2], "Stopped.")

    def test_only_dialog_notices_are_texted(self) -> None:
        self.text("go")
        self.requested()
        self.sup.push({"event": "notice", "text": "Claude is waiting for your input", "type": "idle_prompt"})
        self.sup.push({"event": "notice", "text": "Claude needs your permission",
                       "type": "permission_prompt"})
        self.assertEqual(self.texted(), ["Claude needs your permission"])

    def test_texts_in_a_room_others_read_are_ignored(self) -> None:
        self.hs.batches.put(message(OWNER, "in the group", room=GROUP))
        self.text("in the dm")
        self.assertEqual(self.requested(), [{"op": "submit", "text": "in the dm"}])

    def test_a_room_that_gains_a_member_stops_being_answered(self) -> None:
        self.text("first")
        self.requested()
        self.hs.members[ROOM] = [ME, OWNER, STRANGER]
        self.text("second")
        time.sleep(1.5)
        self.assertEqual(self.sup.requests, [{"op": "submit", "text": "first"}])

    def test_an_answer_is_held_back_if_someone_joined_meanwhile(self) -> None:
        self.text("go")
        self.requested()
        self.hs.members[ROOM] = [ME, OWNER, STRANGER]
        self.sup.push({"event": "reply", "text": "secret answer"})
        time.sleep(1)
        self.assertEqual(self.hs.texts(), [])

    def test_unknown_members_count_as_shared(self) -> None:
        self.hs.batches.put(message(OWNER, "nowhere", room="!unknown:example.org"))
        self.text("here")
        self.assertEqual(self.requested(), [{"op": "submit", "text": "here"}])

    def test_commands_with_no_arguments_are_relayed(self) -> None:
        self.text("/reload-skills")
        self.assertEqual(self.requested(), [{"op": "command", "line": "/reload-skills"}])

    def test_a_new_store_does_not_replay_history_and_keeps_its_place(self) -> None:
        self.restart(since=None, history=[message(OWNER, "old news")])
        self.text("new")
        self.assertEqual(self.requested(), [{"op": "submit", "text": "new"}])
        self.assertTrue(self.wait_for(lambda: (self.root / "sync").read_text().startswith("s")))

    def test_invites_are_joined_only_from_the_owner(self) -> None:
        self.hs.batches.put(invite(STRANGER, "!spam:example.org"))
        self.hs.batches.put(invite(OWNER, "!new:example.org"))
        self.assertTrue(self.wait_for(lambda: self.hs.requests))
        time.sleep(0.5)
        self.assertEqual([(m, p) for m, p, _ in self.hs.requests],
                         [("POST", "/_matrix/client/v3/join/%21new%3Aexample.org")])


class PlaintextTest(unittest.TestCase):
    """When the access token would cross a network unencrypted."""

    def test_classification(self) -> None:
        plaintext_off_host = runpy.run_path(str(BRIDGE))["plaintext_off_host"]
        cases = {
            "https://matrix.example.net": False,
            "http://127.0.0.1:8008": False,
            "http://[::1]:8008": False,
            "http://10.0.0.5:6167": False,
            "http://192.168.1.2": False,
            "http://localhost:8008": False,
            # A name proves nothing until it resolves; one that does not is refused.
            "http://homeserver.invalid": True,
            # A public resolver's address: documentation ranges are not global.
            "http://8.8.8.8:8008": True,
            "http://203.0.113.7:8008": False,
            "http://matrix.example.net": True,
        }
        for url, warned in cases.items():
            self.assertEqual(plaintext_off_host(url), warned, url)


    def test_a_public_plaintext_homeserver_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "token").write_text("tok")
            env = {**os.environ, "CLAUDE_AGENT_MATRIX_HOMESERVER": "http://8.8.8.8",
                   "CLAUDE_AGENT_MATRIX_TOKEN_FILE": str(Path(tmp) / "token")}
            result = subprocess.run([sys.executable, str(BRIDGE)], env=env, capture_output=True,
                                    text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing a plain-http homeserver", result.stderr)

if __name__ == "__main__":
    unittest.main()
