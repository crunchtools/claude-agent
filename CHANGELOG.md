# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- `sdlc ci` now builds, smokes and scans the reference image from the root
  Containerfile (`stacks = ["container"]`), and `[tool.sdlc.image] publish =
  false` keeps it unpublished, ready for the fleet `ci.yml` (sdlc 0.15).
- Gatehouse triage gets `review_result` from the review job, so a pull request
  whose review died before posting anything is held instead of passing.

### Fixed
- The `discarded_errors` exception on `hooks.py` now names its finding by
  item and line (`exc`, 153) rather than by the log text `could not tighten`,
  which the finding does not contain. Gourmand is about to honour `pattern`
  (crunchtools/gourmand#43), and the old text would have stopped suppressing it.
- The count of typed bytes tmux never confirmed, logged at a reconnect, no
  longer includes those sent to a control client that ended on its own.

## [1.2.0] - 2026-11-17

### Changed
- The session needs tmux 3.2 or later, whose `attach-session` takes `-f`. The
  packages recommend `tmux >= 3.2`. An older tmux that is merely installed is
  passed over for the pty, with a log line; one asked for with
  `CLAUDE_AGENT_TERMINAL=tmux` stops the start with a message. Before, an old
  tmux started Claude Code and then took it for dead at every poll, a quiet
  respawn loop.
- A tmux that stops answering long enough to be reconnected logs how many
  typed bytes it never confirmed, which may be lost.
- In tmux, the supervisor polls the pane, reads the screen and types through
  one control-mode client (`tmux -C`), read in its own loop, in place of a tmux
  command run to the end each time. A tmux server that stops answering no
  longer holds up the control socket, and the hooks with it, for up to 10
  seconds a poll. Starting Claude Code and signalling it still run a tmux
  command each (#30).
- `CLAUDE_AGENT_RESPAWN` has a floor of 2 seconds; a shorter delay is raised
  to it, with a log line (#33).

### Fixed
- A paste's Enter, a stop's Escape and a dialog's answer, each timed, reach
  only the Claude Code they were meant for. In tmux, one that ended in that
  window was replaced in the same pane, and the key landed on the next one's
  startup screen, where Enter on the bypass warning exits. A stop no longer
  sends Escape to a Claude Code still at its startup screens, and an answer
  whose Claude Code ended waits for the next one (#33).
- A supervisor that was killed outright (SIGKILL, with no systemd to end the
  tmux server too) no longer blocks the next one. The session names its
  supervisor in a tmux option, and `claude-agent run` ends a session whose
  supervisor is gone. One with no supervisor named, as 1.1.0 left them, is
  still refused (#32).
- In tmux, a restart that failed is tried again in the same pane. It used to
  forget the pane, so every later attempt asked for a new session under a name
  that was still taken and failed too, for good (#31).

## [1.1.0] - 2026-11-16

### Added
- The session runs in tmux when tmux is installed, on a tmux server of its
  own (`tmux -L claude-agent`, session `claude-agent`). `claude-agent attach`
  is a `tmux attach`, so the window takes the size of the terminal attaching
  and redraws cleanly; `--rw` makes it read-write, prefix d detaches. Remote
  Control and the Matrix bridge are unchanged: both talk to Claude Code and
  the control socket, not the terminal. Without tmux it is the pty as before.
  `CLAUDE_AGENT_TERMINAL`, `CLAUDE_AGENT_TMUX_SOCKET` and
  `CLAUDE_AGENT_TMUX_SESSION` choose.
- `CLAUDE_AGENT_RESPAWN=N` starts Claude Code again N seconds after it exits,
  in the same terminal, instead of exiting; the socket, the bridge and anyone
  attached stay.
- `status` (op and command) says which terminal holds the session.
- The packages recommend tmux. UBI 10 and Hummingbird do not carry it, so
  their images keep the pty.

## [1.0.1] - 2026-11-14

### Fixed
- The Matrix bridge sends `User-Agent: claude-agent-matrix`. urllib's default,
  `Python-urllib/3.x`, is a banned signature behind Cloudflare (error 1010), so
  a homeserver behind it refused every call and the bridge never got past
  `whoami`.

## [1.0.0] - 2026-10-21

The stable interface: the `claude-agent` subcommands and their options, the
control socket's ops and events, the `CLAUDE_AGENT_*` variables, and the
`managed-settings.d` file names. A breaking change to any of them is a major
release.

### Added
- `examples/`: Containerfiles for UBI, Debian and Hummingbird, and a Quadlet
  unit, installed to `/usr/share/doc/claude-agent/examples/`. CI builds each
  from the published repositories.

### Changed
- Docs cut to three pages: `install.md`, `run.md` (was `supervisor.md` and
  `matrix.md`) and `controls.md` (was `hooks.md` and `policy.md`).

## [0.2.1] - 2026-10-21

### Fixed
- The Matrix bridge acts only on `m.text`. A notice (a gateway's "your
  message was not sent", for one) was treated as a prompt from the sender,
  and the agent answered it.

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
