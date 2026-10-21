# Reaching the agent by Matrix text

`claude-agent-matrix` carries texts from allowed Matrix senders to the
session `claude-agent run` holds, and texts back each turn's final answer,
with a typing indicator while the turn runs. `claude-agent run` starts it when
`CLAUDE_AGENT_MATRIX_HOMESERVER` is set, and restarts it if it dies.

## Why it matters

An agent that works for hours should be reachable from a phone. Matrix is
open, self-hostable and bridges to SMS and RCS, so a text from the phone can
land in the agent's own DM room.

## How it works

The bridge long-polls `/sync` as the agent's Matrix user and acts only on
senders in `CLAUDE_AGENT_MATRIX_ALLOW`, checked on the sender, never the room.
It joins invites from the same senders.

| Text | What happens |
|------|--------------|
| `!anything` | refused: a typed `!` would run a shell command |
| `/help` | the list of commands, answered by the bridge |
| `/stop [text]` | interrupt the turn, then send `text` |
| `/clear`, `/compact`, `/model`, `/effort`, `/fast`, `/goal`, `/mcp reconnect`, `/reload-skills`, `/reload-plugins` | the built-in, arguments checked; its result is texted back |
| `/other ...` | sent as "Run /other ...", so a skill runs and a built-in never reaches the keyboard |
| anything else | a prompt; held while a turn runs |

What comes back: the turn's final answer (only the final one), a command's
result, a question or plan waiting on screen (reply to answer it), a dialog
Claude Code is waiting on, and "Stopped." The typing indicator starts when a
turn starts, is renewed every 20 seconds while hooks keep firing, lapses after
three minutes without one, and stops at a quarter hour.

## Example

```bash
CLAUDE_AGENT_MATRIX_HOMESERVER=http://matrix.example.net:8008
CLAUDE_AGENT_MATRIX_TOKEN_FILE=/etc/claude-agent/matrix-token
CLAUDE_AGENT_MATRIX_ALLOW=@alice:example.org
claude-agent run -- --permission-mode acceptEdits
```

| Variable | Default | Meaning |
|----------|---------|---------|
| `CLAUDE_AGENT_MATRIX_HOMESERVER` | unset | the homeserver; set to turn the bridge on |
| `CLAUDE_AGENT_MATRIX_TOKEN_FILE` | required | a file holding the agent user's access token |
| `CLAUDE_AGENT_MATRIX_ALLOW` | empty | comma-separated Matrix IDs whose texts get through |
| `CLAUDE_AGENT_MATRIX_STATE` | `~/.local/state/claude-agent/matrix-sync` | the sync position, so texts sent while down are delivered |

## Related

- [supervisor.md](supervisor.md): how texts are held, typed and refused
