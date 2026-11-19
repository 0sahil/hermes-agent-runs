# Changelog

All notable changes to Agent Runs for Hermes are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

First public release, 0.1.0.

### Added
- An **Agent Runs** pane in the Hermes Desktop right sidebar listing the Claude Code runs on this
  computer, newest activity first, with status (running, waiting, quiet, finished), project folder,
  where each run was started from (Hermes chat with its title, Hermes cron, terminal, IDE), runtime,
  last activity and latest action.
- A **Hermes / All** switch: runs started by Hermes chats and cron jobs, or every run.
- A live log for each run, read from Claude Code's own session log: prompt, messages, tool calls,
  results, errors, interruptions and compactions. It follows new lines and stops when you scroll up.
  **Open chat** goes to the Hermes chat that started a run.
- Finished runs stay listed for a few hours, also across gateway restarts.
- A status-bar count of live runs, and the ⌘K command **Show agent runs**.
- Settings: default view, Hermes' own model calls, thinking, how long to keep finished runs, and
  when a run counts as quiet.
- Works on macOS, Linux and Windows; finds native, npm and Windows installs of Claude Code with no
  setup.

### Fixed
- The run log is readable in the narrow right sidebar: each line keeps a small kind mark and time
  on the left and gives all remaining width to the text, which wraps normally and breaks long
  commands and paths instead of squeezing to a few characters per line.
- Runs started from a Hermes chat show the chat's title instead of "Hermes chat". The title
  lookup now works with both Hermes gateway kinds and the desktop's list of saved chats, retries
  for chats it has not found yet, and says why in a folded line at the bottom of the pane when a
  title still cannot be found.
- Claude Code's file searches no longer show up as runs. The native Claude Code binary runs its
  Grep and Glob searches as short-lived copies of itself (as ripgrep), which were listed as extra
  runs with the parent's origin, no session and ripgrep flags. They are now skipped, while a
  `claude` started by another run through its Bash tool still counts. Such entries remembered by
  earlier builds are dropped from the finished list.

[Unreleased]: https://github.com/0sahil/hermes-agent-runs/commits/main
