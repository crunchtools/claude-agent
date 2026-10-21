# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-20

### Added

- `claude-agent` package: the circuit breaker, audit and prompts-off hooks,
  moved from crunchtools/josui, as a managed-settings fragment. They act only
  with `CLAUDE_AGENT=1`, and read `CLAUDE_AGENT_*` variables with defaults
  under `$XDG_STATE_HOME` and `$XDG_RUNTIME_DIR`.
- `claude-agent-trentina` package: the crunchtools policy (Trentina as the
  only MCP server, web tools and connectors off, managed hooks only, local
  memory guard) as a second fragment.
- `claude-agent --version` and `claude-agent status`.
- noarch `.rpm` and `all` `.deb` packages, smoke-tested with Anthropic's
  `claude-code` on UBI 9 and 10, Fedora, Hummingbird, Debian 12 and 13, and
  Ubuntu 24.04 and 26.04, and a reference UBI 10 `Containerfile` built from
  them.
