# claude-agent Constitution

> **Version:** 1.1.0
> **Ratified:** 2026-10-21
> **Status:** Active
> **Inherits:** [crunchtools/constitution](https://github.com/crunchtools/constitution) v1.27.0
> **Profile:** Autonomous Agent

claude-agent is the runtime an autonomous Claude Code agent is built from,
shipped as RPM and deb packages: the controls the Autonomous Agent profile
asks of a deployment, packaged once so every agent image installs the same
ones. A deployment (crunchtools/josui is the first) adds its identity, its
mounts and its network.

This file holds what is specific to this repo. The fleet rules and the
Autonomous Agent profile apply at the inherited version and are checked by
`constitution.yml`. They are not restated here.

## Trust Boundary

The runtime does not draw the boundary; the deployment does (container
mounts, network, gateway). What the runtime guarantees is that its controls
cannot be switched off from inside a session: they are managed settings under
`/etc/claude-code/managed-settings.d/`, which no user, project or session
setting overrides, and with `claude-agent-trentina` installed only managed
hooks run.

## Layer 1 — Trust Boundary Architecture

Provided by the deployment. `claude-agent-trentina` denies `WebFetch` and
`WebSearch`, so untrusted web content reaches the agent only through the
gateway's judged tools.

## Layer 2 — MCP Server Governance

`claude-agent-trentina` allows one MCP server, `trentina`, and turns off
claude.ai connectors. The core package sets no MCP policy.

## Layer 3 — Container & Supply Chain Security

Packages carry no binaries: stdlib Python and shell, installed as-is.
Claude Code comes from Anthropic's signed repositories and is never vendored.
Packages are published only as release assets, signed and served by
crunchtools/packages. The root `Containerfile` is a reference built in CI from
the packages under test; it is not published. The `examples/` Containerfiles
(UBI, Debian, Hummingbird) and Quadlet unit ship in the core package's docs;
CI builds each from the published repositories on every change.

## Layer 4 — Runtime Security & Behavioral Controls

`hooks.py breaker` denies a conversation after `CLAUDE_AGENT_MAX_TOOL_CALLS`
(default 500) calls or `CLAUDE_AGENT_MAX_REPEAT` (default 5) identical calls
in a row, stays tripped, and fails closed. `allow-hook.sh` answers permission
prompts only with `CLAUDE_AGENT_ANSWER_PERMISSIONS=1`, and never a question to
the person or plan approval.

**Gate:** every hook in the core `claude-agent` package does nothing unless
`CLAUDE_AGENT=1`. Managed settings reach every session on the machine;
installing the core package must not change a person's interactive Claude
Code. Policy that applies machine-wide, the memory guard included, lives only
in `claude-agent-trentina`, whose description says so.

## Layer 5 — Credential & Identity Management

The core runtime holds no credentials. The Matrix bridge holds one: the
access token of the agent's own Matrix user, read from a file the deployment
supplies (`CLAUDE_AGENT_MATRIX_TOKEN_FILE`), never from the image or the
environment. The audit record redacts secrets by key name and by token
pattern before writing.

## Layer 6 — Monitoring, Detection & Response

`hooks.py audit` writes one JSON line per tool call to
`CLAUDE_AGENT_AUDIT_DIR`, with the profile's fields, and deletes files older
than `CLAUDE_AGENT_AUDIT_RETENTION_DAYS` (default 120) once a day. Shipping
the log off the host is the deployment's.

## Text Path Contract

- The supervisor's state comes from hooks only. It reads the screen in two
  narrow places: startup warnings, and the confirmation a typed built-in may
  open.
- Nothing typed starts with `!` or `/`: a typed `!` runs a shell command with
  no hook, and a typed `/` can run `/exit` or `/config`. Built-ins are typed
  only from the supervisor's allowlist, which the bridge's matches.
- Texts are pasted only when the session is idle, with one exception: a text
  that answers an open question or plan approval follows the Escape that
  closes it. That Escape interrupts the turn and no hook reports it, so a
  settle delay stands in for idle, as it does after `/stop`. `tests/test_supervisor.py`
  drives a stand-in Claude Code (`tests/fake_claude.py`) that fires the real
  `hooks.py event`; a change to a Claude Code behavior the supervisor depends
  on is checked against a live session before release.

## Packaging Contract

- `packaging/files.txt` names the package and mode of every file under
  `root/`; the spec's `%files`, the deb control files and `stage.sh` follow it,
  and `tests/test_packaging.py` holds them together.
- Each package owns its own `managed-settings.d` fragment. Fragments merge
  (hooks included); no two packages write one file.
- The smoke test installs the packages next to Anthropic's `claude-code` on
  every target distro, Hummingbird included, and runs the installed hooks.
