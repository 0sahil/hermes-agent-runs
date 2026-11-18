# Contributing to Agent Runs for Hermes

Thanks for your interest! Bug reports, ideas and pull requests are all welcome.

## Before you start
- For anything bigger than a small fix, please open an issue first so we can agree on the approach.
- Check existing issues to avoid duplicates.

## Development setup
Requirements: Hermes Desktop 0.23.1 or newer, Python 3.11+, and Node.js (for a syntax check).

```sh
git clone https://github.com/0sahil/hermes-agent-runs.git
cd hermes-agent-runs
python -m pip install pytest psutil       # fastapi and httpx too, to run the route tests
python -m pytest                          # unit tests
hermes plugins validate .                 # the check the Hermes plugin catalog runs
```

The tests never look at your real processes or `~/.claude`: they use an invented process list and
invented transcripts, and pass without psutil.

To try your changes in Hermes, install your checkout over the installed copy:

```sh
hermes plugins install "file://$PWD" --force --enable
hermes gateway restart                    # the backend (dashboard/*.py) loads on a restart
```

The pane (`desktop/plugin.js`) reloads with **⌘K → Reload desktop plugins**.

## Project layout
- `plugin.yaml`: the plugin manifest (name, version, `requires_hermes`, settings).
- `desktop/plugin.js`: the pane, the status-bar chip and the ⌘K command. Plain ESM loaded without a
  build step; it may import only `@hermes/plugin-sdk`, `react` and `react/jsx-runtime`.
- `dashboard/plugin_api.py`: the backend routes, mounted at `/api/plugins/agent-runs/`.
- `dashboard/runs_core.py`: what every agent shares: where a run came from, its status, the memory
  of finished runs and the API payloads. No web framework and no psutil, so it is tested alone.
- `dashboard/procs.py`: the process list, behind an interface the tests replace.
- `dashboard/adapters/claude.py`: everything Claude Code specific.
- `tests/`: unit tests with invented data.

## Adding an agent
Write `dashboard/adapters/<agent>.py` with the methods listed in `runs_core.Adapter` (which processes
are the agent's, which session a process is, how to read its log), load it in
`dashboard/plugin_api.py`, and add tests with invented processes and logs. The core, the API and the
pane need no change. Document what the adapter reads in the README.

## Pull requests
- Keep PRs focused; one change per PR.
- Add or update tests for backend changes. Fixtures must be invented: never paste a real transcript,
  prompt, path or session id.
- Make sure `python -m pytest` and `hermes plugins validate .` pass (CI runs both, on macOS, Linux
  and Windows).
- For changes to the pane, include a screenshot or short recording with invented data.
- Add a line under `## [Unreleased]` in `CHANGELOG.md`.
- Anything new the plugin reads or does on the user's computer must be listed under "What the plugin
  does on your computer" in the README.

## Ground rules for the code
These keep the plugin acceptable for the Hermes plugin catalog and keep it trustworthy:
- Read-only: the plugin never sends input to a run, never stops one and never writes outside its own
  data folder.
- No network, no subprocesses, no model calls.
- The pane never receives environment values, full command lines or the prompt in the run list.
- `desktop/plugin.js` uses only the plugin SDK: no `eval` or `new Function`, no reaching into Hermes'
  own markup or stores, theme variables instead of colours.
- Code must work on macOS, Linux and Windows: use `pathlib`, and give anything platform-specific a
  path for every OS.

## License
By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
