#!/usr/bin/env python3
"""Carry a Matrix DM to an agent's session and its answers back.

`claude-agent run` starts this beside the supervisor when
CLAUDE_AGENT_MATRIX_HOMESERVER is set. It long-polls /sync as the agent's
own Matrix user and turns each text from an allowed sender
(CLAUDE_AGENT_MATRIX_ALLOW, checked on the sender, never the room) into a
request to the supervisor:

    !anything          refused: a typed ! would run a shell command
    /help              answered here
    /stop [text]       interrupt the turn; then send text, if any
    /<allowed> [args]  a built-in from COMMANDS, arguments checked here
    /<other> ...       sent as "Run /<other> ...", so a skill runs and a
                       built-in that would end or reconfigure the session
                       never reaches the keyboard
    anything else      sent as a prompt, held while a turn runs

From the supervisor's events it shows a typing indicator for the length of a
turn and texts the turn's final answer, a command's result, a question
waiting on screen, and nothing else. Standard library only.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import runpy
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# The supervisor beside this file says where its socket is.
socket_path = runpy.run_path(str(Path(__file__).resolve().parent / "supervisor.py"))["socket_path"]
SYNC_TIMEOUT_MS = 30000
REQUEST_TIMEOUT_SECONDS = 30
RETRY_SECONDS = 5
TYPING_TIMEOUT_MS = 30000
TYPING_REFRESH_SECONDS = 20
TYPING_IDLE_SECONDS = 180
TYPING_CAP_SECONDS = 900
QUESTION_CHARS = 1500
# Built-ins that work typed, with no picker: name -> (arguments pattern,
# usage). The supervisor allows the same names. Arguments of the commands
# outside FREE_TEXT are lowercased: phone keyboards capitalize.
COMMANDS = {
    "clear": (r".{0,500}", "/clear [name]"),
    "compact": (r".{0,500}", "/compact [instructions]"),
    "model": (r"[\w.\[\]-]{1,100}", "/model <alias or id>"),
    "effort": (r"low|medium|high|xhigh|max|auto", "/effort low|medium|high|xhigh|max|auto"),
    "fast": (r"on|off", "/fast on|off"),
    "goal": (r".{1,500}", "/goal <condition>|clear"),
    "mcp": (r"reconnect [\w.-]{1,100}", "/mcp reconnect <server>|all"),
    "reload-skills": (r"", "/reload-skills"),
    "reload-plugins": (r"", "/reload-plugins"),
}
FREE_TEXT = {"clear", "compact", "goal"}
HELP = (
    "Commands by text: /stop [text], " + ", ".join(usage for _, usage in COMMANDS.values())
    + ". A text sent while Claude works waits for the turn to end; /stop interrupts it."
    " Other /names run as skills."
)
NOTICE_TYPES = {None, "permission_prompt", "elicitation_dialog"}


def log(message: str) -> None:
    print(f"matrix-bridge: {message}", file=sys.stderr, flush=True)


class Homeserver:
    """The few client-server API calls the bridge makes."""

    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token

    def call(
        self, method: str, path: str, body: dict | None = None, timeout: float = REQUEST_TIMEOUT_SECONDS
    ) -> dict:
        payload = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=payload, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        if payload is not None:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")

    def whoami(self) -> str:
        return self.call("GET", "/_matrix/client/v3/account/whoami")["user_id"]

    def sync(self, since: str | None) -> dict:
        query = {"timeout": str(SYNC_TIMEOUT_MS if since else 0)}
        if since:
            query["since"] = since
        path = "/_matrix/client/v3/sync?" + urllib.parse.urlencode(query)
        return self.call("GET", path, timeout=SYNC_TIMEOUT_MS / 1000 + REQUEST_TIMEOUT_SECONDS)

    def join(self, room_id: str) -> None:
        self.call("POST", f"/_matrix/client/v3/join/{urllib.parse.quote(room_id, safe='')}", {})

    def send_text(self, room_id: str, text: str) -> None:
        room = urllib.parse.quote(room_id, safe="")
        path = f"/_matrix/client/v3/rooms/{room}/send/m.room.message/{uuid.uuid4().hex}"
        self.call("PUT", path, {"msgtype": "m.text", "body": text})

    def members(self, room_id: str) -> set[str]:
        room = urllib.parse.quote(room_id, safe="")
        return set(self.call("GET", f"/_matrix/client/v3/rooms/{room}/joined_members").get("joined", {}))

    def typing(self, room_id: str, user_id: str, on: bool) -> None:
        room, user = urllib.parse.quote(room_id, safe=""), urllib.parse.quote(user_id, safe="")
        body = {"typing": True, "timeout": TYPING_TIMEOUT_MS} if on else {"typing": False}
        self.call("PUT", f"/_matrix/client/v3/rooms/{room}/typing/{user}", body)


class Supervisor:
    """Requests to the supervisor's socket, one connection each."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def request(self, message: dict) -> dict:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(10)
                conn.connect(str(self.path))
                conn.sendall(json.dumps(message).encode() + b"\n")
                return json.loads(conn.makefile().readline() or "{}")
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"the session is not reachable ({exc.__class__.__name__})"}

    def events(self):
        """Yield the supervisor's events, reconnecting while it starts or restarts."""
        while True:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                    conn.connect(str(self.path))
                    conn.sendall(b'{"op": "subscribe"}\n')
                    lines = conn.makefile()
                    lines.readline()
                    for line in lines:
                        yield json.loads(line)
            except (OSError, ValueError) as exc:
                log(f"supervisor events: {exc!r}, reconnecting")
            time.sleep(RETRY_SECONDS)


class Bridge:
    def __init__(self, homeserver: Homeserver, allow: set[str], state: Path, supervisor: Supervisor) -> None:
        self.homeserver = homeserver
        self.allow = allow
        self.state = state
        self.supervisor = supervisor
        self.user_id = ""
        self.room = ""
        self.sender = ""
        self.busy_since = 0.0
        self.activity = 0.0

    # Texts in --------------------------------------------------------------

    def handle_text(self, room_id: str, body: str) -> None:
        text = body.strip()
        self.room = room_id
        if text.startswith("!"):
            self.tell("Shell commands by text are off.")
        elif text.startswith("/"):
            self.handle_slash(text)
        else:
            self.submit(text)

    def handle_slash(self, text: str) -> None:
        name, _, args = " ".join(text[1:].split()).partition(" ")
        # Phone keyboards capitalize and punctuate.
        name = name.lower().rstrip(".!?")
        if name == "help":
            self.tell(HELP)
        elif name == "stop":
            self.answer(self.supervisor.request({"op": "stop", "text": args}))
        elif name in COMMANDS:
            pattern, usage = COMMANDS[name]
            if name not in FREE_TEXT:
                args = args.lower().rstrip(".!?")
            if re.fullmatch(pattern, args) and args.isprintable():
                self.answer(self.supervisor.request({"op": "command", "line": f"/{name} {args}".rstrip()}))
            else:
                self.tell(f"Usage: {usage}")
        else:
            self.submit(f"Run {text}")

    def submit(self, text: str) -> None:
        self.answer(self.supervisor.request({"op": "submit", "text": text}))

    def answer(self, reply: dict) -> None:
        """Only a refusal is texted back; the turn's answer comes as an event."""
        if not reply.get("ok"):
            self.tell(f"Not sent: {reply.get('error', 'unknown error')}.")

    # Events out ------------------------------------------------------------

    def on_event(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "busy":
            self.busy_since = self.activity = time.monotonic()
            self.typing(True)
        elif kind == "heartbeat":
            self.activity = time.monotonic()
        elif kind in ("idle", "exited", "reply", "error", "stopped"):
            self.typing(False)
        text = self.event_text(event)
        if text:
            self.tell(text)

    def event_text(self, event: dict) -> str:
        """What an event tells the operator; most tell nothing."""
        texts = {
            "reply": lambda: event.get("text", ""),
            "error": lambda: f"The turn ended with an error: {event.get('text', '')}",
            "stopped": lambda: "Stopped.",
            "result": lambda: "\n\n".join(event.get("texts", [])),
            "question": lambda: question_text(event),
            "notice": lambda: event.get("text", "") if event.get("type") in NOTICE_TYPES else "",
        }
        text = texts.get(str(event.get("event")))
        return text() if text else ""

    def typing(self, on: bool) -> None:
        if not on:
            self.busy_since = 0.0
        if not self.room or not self.user_id:
            return
        try:
            self.homeserver.typing(self.room, self.user_id, on)
        except (urllib.error.URLError, OSError) as exc:
            log(f"typing failed: {exc}")

    def keep_typing(self) -> None:
        """Renew the indicator while hooks keep firing, for at most a quarter hour."""
        while True:
            time.sleep(TYPING_REFRESH_SECONDS)
            now = time.monotonic()
            if (self.busy_since and now - self.activity < TYPING_IDLE_SECONDS
                    and now - self.busy_since < TYPING_CAP_SECONDS):
                self.typing(True)

    def tell(self, text: str) -> None:
        if not text.strip() or not self.room:
            return
        # Checked again at send time: someone may have joined since the text came in.
        if not self.is_direct(self.room, self.sender):
            log(f"held back a text: {self.room} is no longer one-to-one")
            return
        try:
            self.homeserver.send_text(self.room, text)
        except (urllib.error.URLError, OSError) as exc:
            log(f"send to {self.room} failed: {exc}")

    # Matrix ----------------------------------------------------------------

    def run(self) -> None:
        while not self.user_id:
            try:
                self.user_id = self.homeserver.whoami()
            except (urllib.error.URLError, OSError, KeyError) as exc:
                log(f"whoami failed, retrying: {exc}")
                time.sleep(RETRY_SECONDS)
        log(f"signed in as {self.user_id}")
        threading.Thread(target=self.follow, daemon=True).start()
        threading.Thread(target=self.keep_typing, daemon=True).start()
        since = self.load_since()
        while True:
            try:
                batch = self.homeserver.sync(since)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log(f"sync failed, retrying: {exc}")
                time.sleep(RETRY_SECONDS)
                continue
            # The first sync of a new store sets the mark without replaying
            # history; after that, texts sent while the agent was down arrive.
            self.process(batch, deliver=since is not None)
            since = batch.get("next_batch", since)
            self.save_since(since)

    def follow(self) -> None:
        for event in self.supervisor.events():
            self.on_event(event)

    def process(self, batch: dict, deliver: bool) -> None:
        rooms = batch.get("rooms", {})
        for room_id, room in rooms.get("invite", {}).items():
            self.consider_invite(room_id, room)
        for room_id, room in rooms.get("join", {}).items():
            for event in room.get("timeline", {}).get("events", []):
                self.consider_event(room_id, event, deliver)

    def consider_invite(self, room_id: str, room: dict) -> None:
        for event in room.get("invite_state", {}).get("events", []):
            if (event.get("type") == "m.room.member" and event.get("state_key") == self.user_id
                    and event.get("content", {}).get("membership") == "invite"):
                if event.get("sender") not in self.allow:
                    log(f"ignored an invite to {room_id} from {event.get('sender')}")
                    return
                try:
                    self.homeserver.join(room_id)
                    log(f"joined {room_id} on an invite from {event['sender']}")
                except (urllib.error.URLError, OSError) as exc:
                    log(f"join {room_id} failed: {exc}")
                return

    def consider_event(self, room_id: str, event: dict, deliver: bool) -> None:
        sender = event.get("sender")
        if event.get("type") != "m.room.message" or sender == self.user_id:
            return
        if sender not in self.allow:
            log(f"dropped a message in {room_id} from {sender}")
            return
        msgtype = event.get("content", {}).get("msgtype")
        if msgtype != "m.text":
            # A notice is a bot talking (the spec says never answer one); a
            # gateway's "not sent" notice would otherwise become a prompt.
            log(f"ignored {msgtype} in {room_id} from {sender}")
            return
        body = event.get("content", {}).get("body")
        if deliver and isinstance(body, str) and body.strip():
            if not self.is_direct(room_id, sender):
                log(f"ignored a text in {room_id}: answers go only to a one-to-one room")
                return
            self.sender = sender
            self.handle_text(room_id, body)

    def is_direct(self, room_id: str, sender: str) -> bool:
        """Only the agent and that sender in the room, now: nobody else reads the answers.

        Asked on every text, never cached, since anyone may join later. A room
        whose members cannot be read counts as shared.
        """
        try:
            return self.homeserver.members(room_id) == {self.user_id, sender}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"members of {room_id} unknown: {exc}")
            return False

    def load_since(self) -> str | None:
        try:
            return self.state.read_text().strip() or None
        except OSError:
            return None

    def save_since(self, since: str | None) -> None:
        if since:
            self.state.parent.mkdir(parents=True, exist_ok=True)
            staged = self.state.with_suffix(".tmp")
            staged.write_text(since)
            staged.replace(self.state)


def question_text(event: dict) -> str:
    """A question waiting on screen, as a text the operator can answer by replying."""
    tool_input = event.get("input") or {}
    if event.get("tool") == "ExitPlanMode":
        plan = str(tool_input.get("plan", ""))[:QUESTION_CHARS]
        return f"Plan ready for approval:\n{plan}\n\nReply to approve or change it, or /stop."
    lines = []
    for question in tool_input.get("questions", []):
        lines.append(str(question.get("question", "")))
        lines += [f"- {option.get('label', '')}" for option in question.get("options", [])]
    return "\n".join(lines)[:QUESTION_CHARS] + "\n\nReply with your answer, or /stop."


def plaintext_off_host(url: str) -> bool:
    """http to any address that is globally routable, or that cannot be resolved."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http":
        return False
    try:
        resolved = {info[4][0] for info in socket.getaddrinfo(parsed.hostname or "", None)}
    except OSError:
        return True
    return any(ipaddress.ip_address(address.split("%")[0]).is_global for address in resolved)


def main() -> int:
    env = os.environ
    try:
        homeserver = env["CLAUDE_AGENT_MATRIX_HOMESERVER"]
        token = Path(env["CLAUDE_AGENT_MATRIX_TOKEN_FILE"]).read_text().strip()
    except (KeyError, OSError) as exc:
        log(f"not configured: {exc}")
        return 1
    if plaintext_off_host(homeserver):
        log("refusing a plain-http homeserver on a public address: the access token would cross the "
            "network unencrypted. Use https, or a homeserver on a private network.")
        return 1
    allow = {mxid.strip() for mxid in env.get("CLAUDE_AGENT_MATRIX_ALLOW", "").split(",") if mxid.strip()}
    if not allow:
        log("[WARNING] CLAUDE_AGENT_MATRIX_ALLOW is empty. No text will get through.")
    default_state = Path.home() / ".local/state/claude-agent/matrix-sync"
    state = Path(env.get("CLAUDE_AGENT_MATRIX_STATE") or default_state)
    Bridge(Homeserver(homeserver, token), allow, state, Supervisor(socket_path())).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
