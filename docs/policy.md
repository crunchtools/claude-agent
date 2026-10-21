# Crunchtools agent policy

`claude-agent-trentina` installs the policy crunchtools agents run under, as
`/etc/claude-code/managed-settings.d/20-claude-agent-trentina.json`. Unlike
the core package's hooks, it applies to every Claude Code session on the
machine: install it in an agent image, not on a workstation.

## Why it matters

An agent's reach is what its policy allows. Crunchtools agents reach the
outside world only through the [Trentina](https://github.com/crunchtools/trentina)
gateway, which judges every tool result before the agent reads it. The policy
closes the paths around the gateway.

## How it works

| Setting | Effect |
|---------|--------|
| `allowedMcpServers: [{serverName: trentina}]` | the gateway is the only MCP server |
| `disableClaudeAiConnectors`, `ENABLE_CLAUDEAI_MCP_SERVERS=false` | no claude.ai connectors |
| `permissions.deny: [WebSearch, WebFetch]` | no web content except through the gateway |
| `allowManagedHooksOnly` | only packaged hooks run; a hook in a home directory or a checkout's `.claude/settings.json` does not |
| `autoMemoryEnabled: false` plus `deny-local-memory.sh` | Claude Code's local file memory stays empty; the gateway's memory graph is the only store |
| `DISABLE_AUTOUPDATER`, `DISABLE_ERROR_REPORTING` | the package manager owns the version; no crash reports leave |

Claude Code merges the fragments in `managed-settings.d/` in name order, hooks
included, so this policy adds to the core package's hooks rather than
replacing them.

## Example

```console
$ sudo dnf install claude-agent-trentina
$ claude-agent status
claude-agent 0.1.0
claude:                2.1.304 (Claude Code)
claude-agent:          installed
claude-agent-trentina: installed
...
```

## Related

- [hooks.md](hooks.md): the breaker, audit and prompts-off hooks this adds to
- [install.md](install.md): building an image with both packages
