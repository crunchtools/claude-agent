# Controls

The `claude-agent` package installs breaker and audit hooks as managed
settings, the one layer no session can override. `claude-agent-trentina` adds a
machine-wide lockdown.

## Why it matters

Nobody is watching an agent: it can loop, and afterward someone has to know
what it did.

## How it works

- **Gate.** Hooks act only when `CLAUDE_AGENT=1`.
- **Breaker.** Denies every later call once a conversation passes
  `CLAUDE_AGENT_MAX_TOOL_CALLS` (500) or repeats one call
  `CLAUDE_AGENT_MAX_REPEAT` (5) times. It fails closed.
- **Audit.** One JSON line per call in `CLAUDE_AGENT_AUDIT_DIR`, secrets
  redacted, kept 120 days. A failed write is reported and never blocks.
- **Prompts off.** With `CLAUDE_AGENT_ANSWER_PERMISSIONS=1`, every permission
  prompt is allowed except questions to you and plan approval.
- **trentina policy** (every session on the machine, so for images only):

| Setting | Effect |
|---------|--------|
| `allowedMcpServers: trentina` | the gateway is the only MCP server |
| deny `WebSearch`, `WebFetch`; connectors off | no web except through the gateway |
| `allowManagedHooksOnly` | only packaged hooks run |
| auto memory off | the gateway's memory is the only store |
| autoupdater and error reporting off | the package manager owns the version |

## Example

```console
$ export CLAUDE_AGENT=1 CLAUDE_AGENT_MAX_REPEAT=2
$ e='{"session_id":"s","tool_name":"Bash","tool_input":{"command":"ls"}}'
$ for i in 1 2 3; do echo "$e" | /usr/libexec/claude-agent/hooks.py breaker; done
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", ...
```

## Related

[run.md](run.md) · [install.md](install.md)
