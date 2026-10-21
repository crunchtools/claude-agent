# claude-agent Constitution

> **Version:** 1.0.0
> **Ratified:** 2026-10-20
> **Status:** Active
> **Inherits:** [crunchtools/constitution](https://github.com/crunchtools/constitution) v1.26.0
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
the packages under test; it is not published.

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

The runtime holds no credentials. The audit record redacts secrets by key
name and by token pattern before writing.

## Layer 6 — Monitoring, Detection & Response

`hooks.py audit` writes one JSON line per tool call to
`CLAUDE_AGENT_AUDIT_DIR`, with the profile's fields, and deletes files older
than `CLAUDE_AGENT_AUDIT_RETENTION_DAYS` (default 120) once a day. Shipping
the log off the host is the deployment's.

## Packaging Contract

- `packaging/files.txt` names the package and mode of every file under
  `root/`; the spec's `%files`, the deb control files and `stage.sh` follow it,
  and `tests/test_packaging.py` holds them together.
- Each package owns its own `managed-settings.d` fragment. Fragments merge
  (hooks included); no two packages write one file.
- The smoke test installs the packages next to Anthropic's `claude-code` on
  every target distro, Hummingbird included, and runs the installed hooks.
