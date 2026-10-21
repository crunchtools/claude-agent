# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-10-21

### Added

- `claude-agent run`: a supervisor that holds an interactive Claude Code
  session in a pty, with no tmux or expect, and drives it over a unix socket
  from the session's hooks (`hooks.py event`), never its screen. Texts that
  arrive mid-turn are held and sent together when the turn ends; `!` and
  unlisted `/` texts are refused; built-in commands come from an allowlist,
  their results read from the transcript and `/effort`'s confirmation
  answered; `stop` interrupts and can redirect; a text answers an open
  question.
- `claude-agent attach [--rw]`, `send [--wait]` and `stop`; `status` shows
  the running session.
- `claude-agent-matrix`: a Matrix bridge that `run` starts and restarts.
  Commands by text, the turn's final answer texted back, and a typing
  indicator while a turn runs.

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
