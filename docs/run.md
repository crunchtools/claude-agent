# Run it unattended

`claude-agent run` holds an interactive Claude Code session in tmux, or in a
pty where tmux 3.2 or later is not installed, and drives it over a local socket (mode 0600, under `$XDG_RUNTIME_DIR/claude-agent`).
Prompts go in; each turn's final answer comes out. `claude-agent-matrix` puts
that socket on your phone.

## Why it matters

Headless `claude -p` loses `/compact`, skills and Remote Control. The
supervisor keeps the real session and takes its state from Claude Code's
hooks, not the screen.

## How it works

- Texts sent mid-turn are held and sent as one prompt when the turn ends.
- `!` and unlisted `/` commands are never typed.
- In tmux, the session is on its own server (`tmux -L claude-agent`), so
  `claude-agent attach` sizes it to your terminal and Remote Control and the
  Matrix bridge carry on while you watch. In a pty it stays 160×50.
- `/stop [text]` interrupts, then sends `text`.
- Matrix: only senders in `CLAUDE_AGENT_MATRIX_ALLOW`, only in a one-to-one
  room, checked again before each answer. Plain `http` only to loopback or a private network.
  You get a typing bubble, then the final answer. Notices are ignored.

| Text | Result |
|------|--------|
| `/help` | the command list |
| `/clear` `/compact` `/model` `/effort` `/fast`, the rest in `/help` | run, result texted back |
| `/stop [text]` | interrupt |
| `!…` | refused |
| other `/x` | sent as "Run /x" (skills) |

## Example

```console
$ claude-agent run -- --permission-mode acceptEdits &
$ claude-agent send --wait "Reply with exactly: pong"
pong
$ claude-agent status | tail -1
session:               idle, 0 held, pid 4242, tmux -L claude-agent
$ claude-agent attach            # read-only; tmux prefix d detaches (Ctrl-] in a pty)
```

| Variable | Meaning |
|----------|---------|
| `CLAUDE_AGENT_MATRIX_HOMESERVER` | turns the bridge on |
| `CLAUDE_AGENT_MATRIX_TOKEN_FILE` | the agent user's access token |
| `CLAUDE_AGENT_MATRIX_ALLOW` | comma-separated senders |
| `CLAUDE_AGENT_TERMINAL` | `pty` forces the pty; anything else, or unset with tmux 3.2 or later installed, is tmux. Unset, an older tmux is passed over for the pty, with a log line. A tmux that is asked for but older than 3.2, or broken, fails the start with exit 1 |
| `CLAUDE_AGENT_TMUX_SOCKET`, `CLAUDE_AGENT_TMUX_SESSION` | the tmux server and session; both default `claude-agent` |
| `CLAUDE_AGENT_RESPAWN` | seconds, at least 2; start Claude Code again after it exits, keeping the socket |

## Related

[controls.md](controls.md) · [install.md](install.md)
