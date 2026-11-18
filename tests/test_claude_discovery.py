"""Claude adapter: which processes are runs, which session each is, and the safe argv summary."""
import builtins
import io
import json
import os

import pytest

from adapters.claude import parse_args, summarize_args
from conftest import CWD, SECRET_PROMPT, SID_A, SID_B, SID_C, START, native_claude, proc, static_source

# --- process matching --------------------------------------------------------------------------


@pytest.mark.parametrize("p", [
    # macOS / Linux native install: the process is named after the version, argv[0] is "claude"
    native_claude(args=["-p", SECRET_PROMPT]),
    # argv[0] with a full path
    proc(1, "claude", ["/home/dev/.local/bin/claude"]),
    # only the exe path gives it away (argv unreadable)
    proc(2, "2.3.4", [], exe="/home/dev/.local/share/claude/versions/2.3.4"),
    # Windows native install
    proc(3, "claude.exe", ["C:\\Users\\dev\\.local\\bin\\claude.exe", "--model", "opus"]),
    proc(4, "Claude.EXE", ["C:\\tools\\CLAUDE.EXE"]),
    # npm install on macOS/Linux and Windows
    proc(5, "node", ["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js", "-p", "x"]),
    proc(6, "node.exe", ["C:\\Program Files\\nodejs\\node.exe",
                         "C:\\Users\\dev\\AppData\\Roaming\\npm\\node_modules\\@anthropic-ai\\claude-code\\cli.js"]),
])
def test_matches_claude_code(adapter, p):
    assert adapter.match_process(p)


@pytest.mark.parametrize("p", [
    proc(10, "python3", ["python3", "-m", "http.server"]),
    proc(11, "node", ["node", "/srv/app/server.js"]),
    proc(12, "bash", ["bash", "-c", "echo claude"]),
    proc(13, "claude-helper", ["claude-helper"]),
    # the Claude desktop app and its Electron helpers are not Claude Code
    proc(14, "Claude", ["/Applications/Claude.app/Contents/MacOS/Claude"], exe="/Applications/Claude.app/Contents/MacOS/Claude"),
    proc(15, "claude.exe", ["C:\\Users\\dev\\AppData\\Local\\AnthropicClaude\\app-1.0\\claude.exe"],
         exe="C:\\Users\\dev\\AppData\\Local\\AnthropicClaude\\app-1.0\\claude.exe"),
    proc(16, "claude.exe", ["claude.exe", "--type=renderer"]),
    # Claude Code subcommands that are tools, not runs
    proc(17, "claude", ["claude", "mcp", "serve"]),
    proc(18, "claude", ["claude", "doctor"]),
    proc(19, "node", ["node", "/x/node_modules/@anthropic-ai/claude-code/cli.js", "update"]),
])
def test_ignores_other_processes(adapter, p):
    assert not adapter.match_process(p)


def test_session_file_claims_a_renamed_binary(adapter, home):
    p = proc(77, "mywrapper", ["mywrapper", "--model", "opus"])
    home.session_file(77, SID_A)
    adapter.begin_scan()
    assert not adapter.match_process(p) and adapter.claims_pid(77)
    info = adapter.describe(p, {}, static_source([p]))
    assert info["session_id"] == SID_A


def test_stale_session_file_for_reused_pid_is_ignored(adapter, home):
    other = proc(77, "postgres", ["postgres"], start=START + 3600)
    home.session_file(77, SID_A)  # written by an earlier process that had pid 77
    adapter.begin_scan()
    assert adapter.describe(other, {}, static_source([other])) is None


def test_key_files_are_never_read(adapter, home, monkeypatch):
    home.session_file(4242, SID_A)
    (home.claude / "sessions" / "4242.abcdef.key").write_text("secret", "utf-8")
    opened = []
    real_io_open = io.open

    def spy(path, *a, **k):
        opened.append(str(path))
        return real_io_open(path, *a, **k)

    # Path.read_text/open go through io.open; plain open() is builtins.open.
    monkeypatch.setattr(io, "open", spy)
    monkeypatch.setattr(builtins, "open", spy)
    adapter.begin_scan()
    p = native_claude()
    info = adapter.describe(p, {}, static_source([p]))
    assert info["session_id"] == SID_A
    assert opened, "the spy saw no file reads at all"
    assert not [o for o in opened if o.endswith(".key")]


# --- session id resolution ---------------------------------------------------------------------


def test_session_id_from_sessions_file_wins_over_argv(adapter, home):
    home.session_file(4242, SID_A)
    home.transcript(SID_A)
    p = native_claude(args=["--session-id", SID_B, "-p", SECRET_PROMPT])
    adapter.begin_scan()
    info = adapter.describe(p, {}, static_source([p]))
    assert (info["session_id"], info["session_source"]) == (SID_A, "sessions_file")
    assert info["transcript"].name == f"{SID_A}.jsonl"


def test_session_id_from_argv(adapter, home):
    home.transcript(SID_B, folder="some-other-folder")
    for args in (["--session-id", SID_B], [f"--session-id={SID_B}"], ["--resume", SID_B], ["-r", SID_B]):
        p = native_claude(args=args)
        info = adapter.describe(p, {}, static_source([p]))
        assert (info["session_id"], info["session_source"]) == (SID_B, "argv"), args
        assert info["transcript"] is not None


def test_resume_with_fork_does_not_use_the_old_id(adapter, home):
    home.transcript(SID_B)
    p = native_claude(args=["--resume", SID_B, "--fork-session"])
    info = adapter.describe(p, {}, static_source([p], cwd={4242: "/elsewhere"}))
    assert info["session_id"] is None
    assert info["log_note"] == "No log found yet."


def test_session_file_with_mismatched_pid_inside_is_ignored(adapter, home):
    path = home.session_file(4242, SID_A)
    data = json.loads(path.read_text("utf-8"))
    data["pid"] = 999
    path.write_text(json.dumps(data), "utf-8")
    p = native_claude()
    info = adapter.describe(p, {}, static_source([p], cwd={4242: "/nowhere"}))
    assert info["session_id"] is None


def test_transcript_found_by_id_across_project_folders(adapter, home):
    home.transcript(SID_C, folder="folder-one")
    found = home.transcript(SID_A, folder="a-folder-whose-name-says-nothing")
    assert adapter.find_transcript(SID_A) == found
    assert adapter.find_transcript("not-a-uuid") is None
    assert adapter.find_transcript("../" + SID_A) is None


def test_fallback_by_cwd_picks_newest_matching_transcript(adapter, home):
    older = home.transcript(SID_A, [{"type": "user", "cwd": CWD, "message": {"content": "x"}}], folder="f1")
    newer = home.transcript(SID_B, [{"type": "user", "cwd": CWD, "message": {"content": "y"}}], folder="f2")
    elsewhere = home.transcript(SID_C, [{"type": "user", "cwd": "/other", "message": {"content": "z"}}], folder="f3")
    os.utime(older, (START + 10, START + 10))
    os.utime(newer, (START + 20, START + 20))
    os.utime(elsewhere, (START + 30, START + 30))
    p = native_claude()
    info = adapter.describe(p, {}, static_source([p], cwd={4242: CWD}))
    assert (info["session_id"], info["session_source"]) == (SID_B, "cwd")


def test_fallback_by_cwd_ignores_transcripts_older_than_the_process(adapter, home):
    old = home.transcript(SID_A, [{"type": "user", "cwd": CWD}])
    os.utime(old, (START - 600, START - 600))
    p = native_claude()
    info = adapter.describe(p, {}, static_source([p], cwd={4242: CWD}))
    assert info["session_id"] is None and info["transcript"] is None


def test_claude_config_dir_is_honoured_per_process(adapter, home, tmp_path):
    custom = tmp_path / "custom-claude"
    home.session_file(4242, SID_A, config=custom)
    home.transcript(SID_A, config=custom)
    p = native_claude()
    info = adapter.describe(p, {"CLAUDE_CONFIG_DIR": str(custom)}, static_source([p]))
    assert info["session_id"] == SID_A
    assert info["config_dir"] == custom
    assert str(info["transcript"]).startswith(str(custom))


# --- embedded runs -----------------------------------------------------------------------------

EMBEDDED_ARGS = ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                 "--no-session-persistence", "--session-id", SID_C]


def test_hermes_model_call_is_embedded(adapter):
    p = native_claude(args=EMBEDDED_ARGS)
    info = adapter.describe(p, {}, static_source([p]))
    assert info["embedded"] is True
    assert info["transcript"] is None
    assert info["log_note"] == "This run does not save its session, so there is no live log."


def test_no_persistence_under_a_hermes_parent_is_embedded(adapter):
    p = native_claude(args=["-p", "--no-session-persistence"])
    assert adapter.describe(p, {}, static_source([p]), parent_is_hermes=True)["embedded"] is True
    assert adapter.describe(p, {}, static_source([p]), parent_is_hermes=False)["embedded"] is False


def test_skip_prompt_history_env_counts_as_no_persistence(adapter):
    p = native_claude(args=["-p", "--input-format", "stream-json"])
    env = {"CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1"}
    assert adapter.describe(p, env, static_source([p]))["embedded"] is True


def test_ordinary_print_run_is_not_embedded(adapter):
    p = native_claude(args=["-p", SECRET_PROMPT, "--output-format", "stream-json"])
    info = adapter.describe(p, {}, static_source([p]))
    assert info["embedded"] is False and info["interactive"] is False


# --- argv summary ------------------------------------------------------------------------------


def test_argv_summary_keeps_flags_and_safe_values_only():
    args = ["-p", SECRET_PROMPT, "--model", "opus", "--effort", "high", "--permission-mode", "acceptEdits",
            "--max-turns", "40", "--output-format", "json", "--allowedTools", "Bash(git:*)", "Read",
            "--add-dir", "/private/one", "--add-dir", "/private/two", "--append-system-prompt", "be secret",
            "--session-id", SID_A, "--mcp-config", '{"token":"abc"}']
    summary = summarize_args(args)
    assert summary == ("-p --model opus --effort high --permission-mode acceptEdits --max-turns 40 "
                       "--output-format json --allowedTools --add-dir --append-system-prompt --session-id --mcp-config")
    for secret in (SECRET_PROMPT, "Bash(git", "/private", "be secret", SID_A, "token"):
        assert secret not in summary


def test_argv_summary_drops_unsafe_values_of_kept_flags():
    assert summarize_args(["--model", "opus; rm -rf /", "-p", "hi"]) == "--model -p"
    assert summarize_args(["--model=claude-opus-4[1m]"]) == "--model claude-opus-4[1m]"


def test_argv_summary_stops_at_double_dash():
    assert summarize_args(["-p", "--", "--looks-like-a-flag but is the prompt"]) == "-p"


def test_parse_args_values():
    _, values = parse_args(["--resume", SID_A, "--no-session-persistence"])
    assert values["--resume"] == SID_A and "--no-session-persistence" in values


def test_describe_never_returns_the_prompt(adapter, home):
    home.session_file(4242, SID_A)
    p = native_claude(args=["-p", SECRET_PROMPT, "--model", "sonnet"])
    adapter.begin_scan()
    info = adapter.describe(p, {"SECRET_ENV": "do-not-leak"}, static_source([p]))
    blob = json.dumps(info, default=str)
    assert SECRET_PROMPT not in blob and "do-not-leak" not in blob
    assert info["model"] == "sonnet"


def test_status_hint_and_interactive(adapter, home):
    home.session_file(4242, SID_A, status="idle", entrypoint="cli")
    p = native_claude()
    adapter.begin_scan()
    info = adapter.describe(p, {}, static_source([p]))
    assert info["interactive"] is True and info["status_hint"] == "idle"
    home.session_file(4242, SID_A, status="busy", entrypoint="sdk-cli")
    info = adapter.describe(p, {}, static_source([p]))
    assert info["interactive"] is False and info["status_hint"] == "busy"
