# claude-agent

Run Claude Code as an unattended agent. claude-agent ships, as RPM and deb
packages, what a Claude Code session needs when nobody is at the keyboard:
circuit breakers, an audit log, and a permission policy, as managed settings
no session can switch off. Install it next to Anthropic's `claude-code`
package to build your own agent image on UBI, Hummingbird, Fedora, Debian or
Ubuntu.

## Capabilities

1. **Circuit breakers and audit.** Every tool call is counted and logged. A
   conversation that runs away (too many calls, or the same call over and
   over) is halted and stays halted. See [docs/hooks.md](docs/hooks.md).
2. **Prompts off, on purpose.** An opt-in hook answers permission prompts for
   an agent nobody is watching, except questions to the person and plan
   approval. See [docs/hooks.md](docs/hooks.md#prompts-off).
3. **Crunchtools policy.** `claude-agent-trentina` makes the Trentina gateway
   the only MCP server, turns off web tools and connectors, and lets only
   managed hooks run. See [docs/policy.md](docs/policy.md).
4. **Build your own image.** Two repositories and one `dnf install`. See
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
CLAUDE_AGENT=1 claude-agent status
```

The hooks act only in sessions started with `CLAUDE_AGENT=1`; your own
interactive Claude Code is unchanged. apt instructions are in
[docs/install.md](docs/install.md).

## Documentation

| Page | What it covers |
|------|----------------|
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
