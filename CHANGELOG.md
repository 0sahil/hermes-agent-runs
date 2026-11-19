# Changelog

All notable changes to Agent Runs for Hermes are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-19

First public release.

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
  you were. The pane goes back to its run list while the run is open large.
- Settings: default view, Hermes' own model calls, thinking, how long to keep finished runs, and
  when a run counts as quiet.
- Works on macOS, Linux and Windows; finds native, npm and Windows installs of Claude Code with no
  setup.
- Claude Code's own helper processes (its Grep and Glob searches, which run as copies of the
  `claude` binary) are not listed as runs, while a `claude` started by another run's Bash tool is.
- The pane uses the Terminal's background, so it reads the same wherever it is docked, also under
  the translucent (glass) look, and follows the light and dark themes.

[Unreleased]: https://github.com/0sahil/hermes-agent-runs/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/0sahil/hermes-agent-runs/releases/tag/v0.1.0
