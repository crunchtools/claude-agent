#!/usr/bin/env bash
# PreToolUse guard: keep Claude Code's local file-based memory store empty.
# The MCP graph behind the gateway is the only memory store; two writable
# stores is a split brain.
#
# Denies writes landing in the auto-memory directory. Edit tools are matched on
# file_path. Bash is matched on the command text, but only where a write
# operator is directly aimed at such a path -- so reading or listing the
# directory is fine, and so is a command that merely mentions it in passing.

set -uo pipefail

payload=$(cat)
tool=$(jq -r '.tool_name // empty' <<<"$payload")

# ~, /home/<user> and /var/home/<user> all reach the same place: /home is a
# symlink to /var/home on this ostree box. Split literal so this file, and any
# command that writes it, never trips its own guard.
MEM='(~|/var/home/[^/[:space:]]+|/home/[^/[:space:]]+)?/?\.claude/projects/[^/[:space:]]+/mem'
MEM="${MEM}"'ory(/|$|/[^[:space:]]*)'

deny() {
  jq -nc --arg t "$1" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: ("Local file-based memory is disabled on this machine. The MCP graph (mcp__trentina__memory__*) is the only memory store - use memory_store/memory_update instead. Blocked target: " + $t)
    }
  }'
  exit 0
}

case "$tool" in
  Write|Edit|MultiEdit|NotebookEdit)
    target=$(jq -r '.tool_input.file_path // .tool_input.notebook_path // empty' <<<"$payload")
    [[ -z "$target" ]] && exit 0
    grep -Eq "(^|[^[:alnum:]_])${MEM}" <<<"$target" && deny "$target"
    ;;

  Bash)
    cmd=$(jq -r '.tool_input.command // empty' <<<"$payload")
    [[ -z "$cmd" ]] && exit 0

    # A redirect whose target is the memory dir. The path must follow the
    # operator directly, so a heredoc body mentioning it does not count.
    grep -Eq ">>?[[:space:]]*['\"]?${MEM}" <<<"$cmd" && deny "$cmd"

    # A file-creating command with such a path among its arguments, confined to
    # one command (no newline, pipe or separator in between).
    grep -Eq "(^|[^[:alnum:]_])(tee|touch|cp|mv|install|rsync|ln|truncate)[^|;&]*[[:space:]]['\"]?${MEM}" <<<"$cmd" && deny "$cmd"

    # In-place edits and raw writes.
    grep -Eq "(^|[^[:alnum:]_])sed[^|;&]*-i[^|;&]*[[:space:]]['\"]?${MEM}" <<<"$cmd" && deny "$cmd"
    grep -Eq "(^|[^[:alnum:]_])dd[^|;&]*of=[[:space:]]*['\"]?${MEM}" <<<"$cmd" && deny "$cmd"
    ;;
esac

exit 0
