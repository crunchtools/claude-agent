# claude-supervisor

Run Claude Code as an unattended agent. RPM and deb packages that sit next to
Anthropic's `claude-code`: a supervisor that holds an interactive session,
circuit breakers, an audit log, and an optional Matrix bridge so you can text it.

## Capabilities

1. **Run it unattended.** `claude-supervisor run` holds a session and takes prompts
   over a local socket; `claude-supervisor-matrix` lets you text it. [docs/run.md](docs/run.md)
2. **Keep it in bounds.** Breakers stop runaway loops, every tool call is
   audited, and an optional policy package locks the session down. [docs/controls.md](docs/controls.md)
3. **Build your own image.** Two repo files and one install, on UBI, Fedora,
   Hummingbird, Debian or Ubuntu. [docs/install.md](docs/install.md)

## Quick Start

```bash
sudo dnf install claude-supervisor      # repos: docs/install.md
CLAUDE_SUPERVISOR=1 claude-supervisor run &
claude-supervisor send --wait "Reply with exactly: pong"
```

Hooks act only where `CLAUDE_SUPERVISOR=1`, so your own Claude Code is unaffected.

Until 2.0.0 this was `claude-agent`. Through 2.x the packages replace the old
ones, `claude-agent` is an alias of the command, `CLAUDE_AGENT` and
`CLAUDE_AGENT_*` are still read (with a warning) where the new names are unset,
and the tmux server and session keep the name `claude-agent`. 3.0.0 drops the
aliases and renames the tmux server and session.

## Documentation

| Page | Covers |
|------|--------|
| [install.md](docs/install.md) | Repositories, example images, Quadlet |
| [run.md](docs/run.md) | The supervisor, the CLI, texting by Matrix |
| [controls.md](docs/controls.md) | Breakers, audit, prompts off, the trentina policy |

## Development

```bash
python3 -m unittest discover -s tests
pre-commit run --all-files
```

## License

AGPL-3.0-or-later
