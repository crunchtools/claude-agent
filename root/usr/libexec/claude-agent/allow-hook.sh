#!/bin/bash
# PreToolUse and PermissionRequest. Answer the permission question so nobody
# is asked, for an agent (CLAUDE_AGENT=1) whose deployment takes the
# constitution's prompts-off deviation and sets
# CLAUDE_AGENT_ANSWER_PERMISSIONS=1. A server can start sessions with prompts
# off, but the app sets its own mode on each session it opens; a hook answers
# whatever mode that is. Without both variables it answers nothing.
#
# Both events are needed. A PreToolUse allow does not answer a prompt the
# session's mode still calls for: file writes in default or plan mode were
# still put to the person. PermissionRequest runs when that prompt is about
# to be shown, and its allow grants the call.
#
# Two tools are a question to the person and still reach them: a question,
# and approval of a plan. Plan approval is the gate on a plan, so the hook
# answers for everything else in every mode, plan mode included. Each call
# left to the person is logged, so a prompt that should not have happened can
# be explained. A deny from any other hook or rule still wins.
set -euo pipefail
event=$(cat)
[ "${CLAUDE_AGENT:-}" = 1 ] && [ "${CLAUDE_AGENT_ANSWER_PERMISSIONS:-}" = 1 ] || exit 0
{
  read -r tool
  read -r hook_event
} < <(jq -r '(.tool_name // ""), (.hook_event_name // "PreToolUse")' <<<"$event")
case "$tool" in
  AskUserQuestion | ExitPlanMode)
    audit_dir=${CLAUDE_AGENT_AUDIT_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/claude-agent/audit}
    if mkdir -p "$audit_dir" 2>/dev/null; then
      jq -c '{timestamp: (now | todate), hook: "allow", event: (.hook_event_name // "PreToolUse"), left_to_person: .tool_name, mode: .permission_mode, conversation_id: .session_id}' \
        <<<"$event" >>"$audit_dir/allow-hook.jsonl" ||
        echo "allow-hook: could not write the audit line" >&2
    fi
    exit 0
    ;;
esac
case "$hook_event" in
  PermissionRequest)
    printf '%s' '{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}}'
    ;;
  *)
    printf '%s' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"prompts are off in this container"}}'
    ;;
esac
