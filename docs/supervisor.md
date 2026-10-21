# The session supervisor

`claude-agent run` holds an interactive Claude Code session in a pseudo-terminal
nobody has to watch, and drives it from a local socket. Prompts, allowlisted
built-in commands and interrupts go in; each turn's final answer, command
results and open questions come out as events.

## Why it matters

Some Claude Code features exist only in the interactive session: built-in
commands like `/compact` and `/clear`, skills typed by name, Remote Control's
peer sessions. An agent that runs headless (`claude -p`) gives those up. A
script that types into a terminal and reads the screen keeps them, but breaks
on every redraw and every release. The supervisor types into the terminal and
reads Claude Code's documented hooks instead of the screen.

## How it works

**State comes from hooks.** `hooks.py event` hands each SessionStart,
UserPromptSubmit, Stop, StopFailure, Notification and tool event to the
supervisor's socket. The session is idle after SessionStart, Stop or
StopFailure, and busy from UserPromptSubmit.

**Texts are held while a turn runs.** A paste mid-turn would be folded into
the running turn and its answer lost; a paste into an open dialog would answer
it. Texts that arrive while the session is busy are held and pasted together,
as one prompt, when the turn ends. Each paste is one bracketed paste and one
Enter, so a multi-line text is one prompt.

**What is never typed.** A text starting with `!` would run a shell command
with no hook at all, and one starting with `/` would run a built-in such as
`/exit`; both are refused. Built-ins go through the `command` request, which
allows a fixed list (`/clear`, `/compact`, `/model`, `/effort`, `/fast`,
`/goal`, `/mcp reconnect`, `/reload-skills`, `/reload-plugins`). A built-in
fires no hook, so its output is read from the session transcript, and the
confirmation `/effort` opens mid-conversation is answered.

**Interrupts and questions.** `stop` sends Escape once any pending Enter has
been sent, drops held texts, and reports `stopped` (Stop does not fire on an
interrupt). While a question or plan approval is open, the next text closes it
with Escape and becomes the answer.

**The screen is read twice, narrowly:** at startup, to report an untrusted
working directory and to accept bypass-permissions mode when it was asked for,
and for three seconds after a typed built-in, to confirm its dialog.

## Example

```console
$ claude-agent run -- --permission-mode acceptEdits &
$ claude-agent status | tail -1
session:               idle, 0 held, pid 4242
$ claude-agent send --wait "Reply with exactly the word: pong"
pong
$ claude-agent attach          # read-only; --rw to answer the trust question
```

| Variable | Default | Meaning |
|----------|---------|---------|
| `CLAUDE_AGENT_RUNTIME_DIR` | `$XDG_RUNTIME_DIR/claude-agent` | where `control.sock` lives (mode 0600 in a 0700 directory) |
| `CLAUDE_AGENT_CLAUDE` | `claude` | the program to run |
| `CLAUDE_AGENT_STARTUP_QUIET` | `60` | seconds to wait for SessionStart before taking prompts anyway |
| `CLAUDE_AGENT_RESULT_TIMEOUT` | `900` | seconds to wait for a command's result |

## Related

- [matrix.md](matrix.md): reaching the session by text
- [hooks.md](hooks.md): the breaker and audit hooks that run in the same session
