# Security policy

## What the plugin can do
Agent Runs runs inside Hermes. Its backend reads the process list (including command lines, and the
environment and working folder of coding-agent processes), reads Claude Code's session files and
logs, and writes one file in its own data folder. The README lists everything under "What the plugin
does on your computer".

Built-in protections:
- Read-only toward agents: no route sends input to a run or stops one, and nothing is written into
  `~/.claude`. The `.key` files Claude Code keeps beside its session files are never opened.
- No network calls and no subprocesses.
- The log route only opens logs of runs the plugin found itself (by process discovery or its own
  memory of recent runs). A run id from the page is looked up, never used as a path.
- Environment values never leave the backend. The run list carries a command-line summary with flags
  only and a few safe values (model, effort, permission mode, turn limit, formats); the prompt is
  dropped.
- The routes sit behind Hermes' own authentication, like every plugin route.

Not protected against:
- The log view shows what the agent saved: prompts, messages, commands and the start of tool output.
  Anyone who can use your Hermes window, or reach your Hermes backend, can read them.
- Process names and command lines are what the operating system reports; a process can pretend to
  be something else.

## Reporting a vulnerability
Please **do not** open a public issue. Use GitHub's private
[security advisory form](https://github.com/0sahil/hermes-agent-runs/security/advisories/new)
instead. You'll get a response within a few days. Please include steps to reproduce and the
versions of the plugin, Hermes and the agent.

## Supported versions
Only the latest release receives security fixes.
