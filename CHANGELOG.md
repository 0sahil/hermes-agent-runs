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
- The pane arrives as a tab next to **Terminal** (or in the right sidebar when the terminal is not
  in the layout), so it has a tab to drag; where you move it stays.
- A compact run view for narrow sidebars (under about 420 px): one line per log entry that opens in
  place on click, Enter or Space, and a two-line header with the rest under **Details**.
- **Open large**: one run full size in the main area, with a Back control that returns to where
  you were.
- Settings: default view, Hermes' own model calls, thinking, how long to keep finished runs, and
  when a run counts as quiet.
- Works on macOS, Linux and Windows; finds native, npm and Windows installs of Claude Code with no
  setup.

### Changed
- The pane paints its own background, the same one as the Terminal beside it, so every line has
  the same background wherever the pane is docked, including under the translucent (glass) look.
  The full-size run page uses the chat's background. Both follow the light and dark themes.
- Easier to read: the latest action under each run, the chat and time line, the run details and
  the quieter log lines (thinking, results, times) are one step brighter, and finished runs are
  only slightly faded.
- Long chat titles in the run list are shortened in the middle, keeping the end, so a chat and its
  branch ("… task" and "… task #2") stay told apart. The full title is in the tooltip, wider panes
  show more of it, and the runtime and last activity after it always stay visible.

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
