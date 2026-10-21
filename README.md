# claude-agent

Run Claude Code as an unattended agent. claude-agent ships, as RPM and deb
packages, what a Claude Code session needs when nobody is at the keyboard: a
supervisor that holds an interactive session and takes prompts over a socket,
circuit breakers, an audit log, a permission policy as managed settings no
session can switch off, and a Matrix bridge to reach it all by text. Install
it next to Anthropic's `claude-code` package to build your own agent image on
UBI, Hummingbird, Fedora, Debian or Ubuntu.

## Capabilities

1. **A session nobody has to watch.** `claude-agent run` holds an interactive
   session in a pty and drives it from its hooks, never its screen. Texts wait
   while a turn runs; `!` and unlisted `/` commands are never typed. See
   [docs/supervisor.md](docs/supervisor.md).
2. **Reach it by text.** `claude-agent-matrix` carries texts from allowed
   Matrix senders in, and each turn's final answer out, with a typing
   indicator. See [docs/matrix.md](docs/matrix.md).
3. **Circuit breakers and audit.** Every tool call is counted and logged. A
   conversation that runs away (too many calls, or the same call over and
   over) is halted and stays halted. See [docs/hooks.md](docs/hooks.md).
4. **Prompts off, on purpose.** An opt-in hook answers permission prompts for
   an agent nobody is watching, except questions to the person and plan
   approval. See [docs/hooks.md](docs/hooks.md#prompts-off).
5. **Crunchtools policy.** `claude-agent-trentina` makes the Trentina gateway
   the only MCP server, turns off web tools and connectors, and lets only
   managed hooks run. See [docs/policy.md](docs/policy.md).
6. **Build your own image.** Two repositories and one `dnf install`. See
   [docs/install.md](docs/install.md).

## Quick Start

```bash
# Anthropic's claude-code repository
sudo tee /etc/yum.repos.d/claude-code.repo <<'EOF'
[claude-code]
name=Claude Code
baseurl=https://downloads.claude.ai/claude-code/rpm/stable
enabled=1
gpgcheck=1
gpgkey=https://downloads.claude.ai/keys/claude-code.asc
EOF
# crunchtools' repository
sudo curl -fsSL -o /etc/yum.repos.d/crunchtools.repo \
  https://crunchtools.github.io/packages/rpm/crunchtools.repo

sudo dnf install claude-agent
claude-agent run &
claude-agent send --wait "Reply with exactly: pong"
```

The hooks act only in sessions started with `CLAUDE_AGENT=1`; your own
interactive Claude Code is unchanged. apt instructions are in
[docs/install.md](docs/install.md).

## Documentation

| Page | What it covers |
|------|----------------|
| [docs/supervisor.md](docs/supervisor.md) | `claude-agent run`, the socket, holding and refusing texts, attach |
| [docs/matrix.md](docs/matrix.md) | The Matrix bridge: commands by text, what is texted back |
| [docs/hooks.md](docs/hooks.md) | Breakers, audit, prompts off, the `CLAUDE_AGENT` gate, environment |
| [docs/policy.md](docs/policy.md) | What `claude-agent-trentina` enforces machine-wide |
| [docs/install.md](docs/install.md) | dnf and apt setup, and building an agent image |

## Development

```bash
python3 -m unittest discover -s tests -v     # unit tests
pre-commit run --all-files                   # ruff, Gourmand, Gatehouse
podman build -t localhost/claude-agent-reference .   # reference image
```

`root/` is the install tree; `packaging/files.txt` says which package ships
each file. `.github/workflows/packages.yml` builds the packages, installs them
on every target distro and attaches them to each GitHub release, from which
[crunchtools/packages](https://github.com/crunchtools/packages) publishes
the signed repositories.

## License

AGPL-3.0-or-later
