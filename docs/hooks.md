# Breakers, audit and prompts off

The `claude-agent` package installs three hooks as Claude Code managed
settings (`/etc/claude-code/managed-settings.d/10-claude-agent.json`): a
circuit breaker before every tool call, an audit record after it, and an
opt-in hook that answers permission prompts.

## Why it matters

An agent with nobody watching can loop: the same failing command a hundred
times, or a task that never converges. Without a record, nobody can say
afterwards what it did. Managed settings are the one layer no user, project
or session setting overrides, so the controls hold even when the agent edits
its own configuration.

## How it works

**The gate.** Managed settings reach every Claude Code session on the
machine. Every hook does nothing unless `CLAUDE_AGENT=1` is in the session's
environment, so installing the package on a workstation leaves interactive
sessions alone. An agent image sets `ENV CLAUDE_AGENT=1`.

**Breaker** (`hooks.py breaker`, PreToolUse). Counts calls per conversation
in a small state file. It denies the call, and every later call in that
conversation, once there are more than `CLAUDE_AGENT_MAX_TOOL_CALLS` calls
(default 500) or more than `CLAUDE_AGENT_MAX_REPEAT` identical calls in a row
(default 5). If it cannot read or write its state, it denies: a breaker that
fails open is not a breaker.

**Audit** (`hooks.py audit`, PostToolUse). One JSON line per call in
`audit-YYYY-MM-DD.jsonl`: timestamp, tool, server, arguments, result, and
conversation. Values whose key looks like a secret, and tokens recognized by
pattern, are written as `[REDACTED]`; long values are cut at 200 characters.
If it cannot write, it reports the error and the session carries on.

### Prompts off

`allow-hook.sh` runs on PreToolUse and PermissionRequest. With
`CLAUDE_AGENT_ANSWER_PERMISSIONS=1` it allows every tool in every permission
mode, except `AskUserQuestion` and `ExitPlanMode`: a question to the person
and plan approval still reach the person, and each is logged to
`allow-hook.jsonl` in the audit directory. A deny from any other hook or
rule still wins.

## Example

| Variable | Default | Meaning |
|----------|---------|---------|
| `CLAUDE_AGENT` | unset | `1` turns the hooks on for this session |
| `CLAUDE_AGENT_AUDIT_DIR` | `$XDG_STATE_HOME/claude-agent/audit` | where audit lines go |
| `CLAUDE_AGENT_BREAKER_DIR` | `$XDG_RUNTIME_DIR/claude-agent/breaker` | breaker state, per conversation |
| `CLAUDE_AGENT_MAX_TOOL_CALLS` | `500` | calls per conversation |
| `CLAUDE_AGENT_MAX_REPEAT` | `5` | identical calls in a row |
| `CLAUDE_AGENT_ANSWER_PERMISSIONS` | unset | `1` answers permission prompts |

```console
$ export CLAUDE_AGENT=1 CLAUDE_AGENT_MAX_REPEAT=2
$ e='{"session_id":"s","tool_name":"Bash","tool_input":{"command":"ls"}}'
$ for i in 1 2 3; do echo "$e" | /usr/libexec/claude-agent/hooks.py breaker; done
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", ...
```

## Related

- [policy.md](policy.md): turning on `allowManagedHooksOnly` so no other hook runs
- [install.md](install.md): an image with `CLAUDE_AGENT=1` set
