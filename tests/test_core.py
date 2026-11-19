"""Core: origin rules, status, scopes, embedded runs, finished-run memory and failure isolation."""
import json
import os

import pytest

import runs_core
from conftest import CWD, SECRET_PROMPT, SID_A, SID_B, START, assistant, native_claude, proc, static_source, \
    tool_use, user_prompt
from procs import ProcInfo, StaticSource
from runs_core import Backend, RunStore, detect_origin, is_hermes_process, normalize_settings, run_status


class Clock:
    def __init__(self, t=START + 60):
        self.t = t

    def __call__(self):
        return self.t


def backend(source, adapter, tmp_path, settings=None, clock=None, store=None):
    return Backend(source, [adapter], store or RunStore(tmp_path / "data" / "runs.json"), settings or {},
                   clock or Clock())


# --- origin ------------------------------------------------------------------------------------


def test_origin_from_hermes_env():
    p = native_claude()
    chat = detect_origin({"HERMES_SESSION_ID": "local-abc123", "HERMES_SESSION_PLATFORM": "local",
                          "HERMES_CRON_SESSION": ""}, p, {})
    assert chat == {"kind": "hermes_chat", "label": "Hermes chat", "hermes_session_id": "local-abc123"}
    cron = detect_origin({"HERMES_SESSION_ID": "cron_job_1", "HERMES_CRON_SESSION": "1"}, p, {})
    assert cron == {"kind": "hermes_cron", "label": "Hermes cron", "hermes_session_id": "cron_job_1"}
    # An empty HERMES_CRON_SESSION (Hermes sets it for every chat) is not cron.
    assert detect_origin({"HERMES_CRON_SESSION": "", "HERMES_SESSION_ID": "s"}, p, {})["kind"] == "hermes_chat"


def chain(*names_and_argv):
    """claude (pid 1000) <- parent 1001 <- parent 1002 ..."""
    procs_ = {1000: native_claude(pid=1000, ppid=1001)}
    for i, (name, argv) in enumerate(names_and_argv):
        pid = 1001 + i
        procs_[pid] = proc(pid, name, argv, ppid=pid + 1)
    return procs_


@pytest.mark.parametrize("parents, want", [
    ([("zsh", ["-zsh"]), ("login", ["login"]), ("Terminal", ["/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"])],
     ("terminal", "Terminal")),
    ([("bash", ["bash"]), ("tmux: server", ["tmux"])], ("terminal", "Terminal")),
    ([("pwsh.exe", ["pwsh.exe"]), ("WindowsTerminal.exe", ["C:\\Program Files\\WindowsApps\\WindowsTerminal.exe"])],
     ("terminal", "Terminal")),
    ([("cmd.exe", ["cmd.exe"]), ("conhost.exe", ["conhost.exe"])], ("terminal", "Terminal")),
    ([("bash", ["bash"]), ("gnome-terminal-server", ["/usr/libexec/gnome-terminal-server"])], ("terminal", "Terminal")),
    ([("zsh", ["zsh"]), ("Code Helper (Plugin)", ["/Applications/Visual Studio Code.app/x/Code Helper (Plugin)"])],
     ("ide", "VS Code")),
    ([("zsh", ["zsh"]), ("Cursor Helper (Plugin)", ["Cursor Helper (Plugin)"])], ("ide", "Cursor")),
    ([("bash", ["bash"]), ("pycharm", ["pycharm"])], ("ide", "PyCharm")),
    ([("zsh", ["zsh"]), ("zed", ["zed"])], ("ide", "Zed")),
    # the closest known parent wins: tmux inside VS Code is a terminal
    ([("bash", ["bash"]), ("tmux", ["tmux"]), ("code", ["code"])], ("terminal", "Terminal")),
    ([("bash", ["bash"]), ("python3.12", ["python3", "-m", "hermes_cli.main", "chat"]), ("zsh", ["zsh"]),
      ("iTerm2", ["iTerm2"])], ("hermes", "Hermes")),
    ([("bash", ["bash"]), ("launchd", ["launchd"])], ("other", "Other")),
])
def test_origin_from_parent_chain(parents, want):
    procs_ = chain(*parents)
    origin = detect_origin(None, procs_[1000], procs_)
    assert (origin["kind"], origin["label"]) == want


def test_parent_chain_stops_at_an_unreadable_parent():
    procs_ = chain(("zsh", ["zsh"]))  # parent 1002 is missing: the OS refused it (AccessDenied)
    assert detect_origin(None, procs_[1000], procs_)["kind"] == "other"


def test_parent_chain_survives_a_cycle():
    procs_ = {1000: native_claude(pid=1000, ppid=1001), 1001: proc(1001, "sh", ["sh"], ppid=1000)}
    assert detect_origin({}, procs_[1000], procs_)["kind"] == "other"


def test_unreadable_environment_falls_back_to_parents(adapter, tmp_path):
    p = native_claude(pid=1000, ppid=1001)
    term = proc(1001, "kitty", ["kitty"], ppid=1)
    source = static_source([p, term], environ={1000: PermissionError("access denied")})
    [run] = backend(source, adapter, tmp_path).runs_payload("all")["runs"]
    assert run["origin"] == {"kind": "terminal", "label": "Terminal"}


@pytest.mark.parametrize("argv, name, want", [
    (["python3", "-m", "hermes_cli.main", "gateway", "run"], "python3.14", True),
    (["/usr/bin/python3", "-m", "tui_gateway.entry"], "python3", True),
    (["/home/dev/.local/bin/hermes", "chat"], "hermes", True),
    (["/opt/venv/bin/python", "/opt/hermes/hermes_cli/main.py"], "python", True),
    (["python3", "-m", "http.server"], "python3", False),
    (["zsh", "-c", "cd ~/hermes-agent && ls"], "zsh", False),
])
def test_is_hermes_process(argv, name, want):
    assert is_hermes_process(ProcInfo(pid=5, name=name, cmdline=argv)) is want


# --- status ------------------------------------------------------------------------------------


def test_status_rules():
    now = 10_000_000
    assert run_status(False, now, now, 10, False, None) == "finished"
    assert run_status(True, now - 60_000, now, 10, True, "idle") == "waiting"
    assert run_status(True, now - 60_000, now, 10, False, "idle") == "running"  # -p runs never wait for a person
    assert run_status(True, now - 9 * 60_000, now, 10, False, "busy") == "running"
    assert run_status(True, now - 11 * 60_000, now, 10, False, "busy") == "quiet"


def test_quiet_after_quiet_minutes_without_writes(adapter, home, tmp_path):
    home.session_file(4242, SID_A, entrypoint="sdk-cli")
    path = home.transcript(SID_A, [user_prompt("x")])
    os.utime(path, (START, START))
    source = static_source([native_claude(args=["-p"])])
    clock = Clock(START + 5 * 60)
    assert backend(source, adapter, tmp_path, {"quiet_minutes": 10}, clock).runs_payload("all")["runs"][0]["status"] == "running"
    clock.t = START + 30 * 60
    assert backend(source, adapter, tmp_path, {"quiet_minutes": 10}, clock).runs_payload("all")["runs"][0]["status"] == "quiet"


# --- the list ----------------------------------------------------------------------------------


def hermes_env(session="local-chat-1"):
    return {"HERMES_SESSION_ID": session, "HERMES_SESSION_PLATFORM": "local", "HERMES_CRON_SESSION": "",
            "HERMES_SESSION_USER_NAME": "invented-user", "ANTHROPIC_API_KEY": "sk-invented-key"}


def test_runs_payload_shape_and_scopes(adapter, home, tmp_path):
    home.session_file(4242, SID_A, cwd=CWD, entrypoint="sdk-cli")
    home.transcript(SID_A, [user_prompt(SECRET_PROMPT), tool_use("Bash", {"command": "make test"})])
    from_hermes = native_claude(pid=4242, args=["-p", SECRET_PROMPT, "--model", "opus"])
    from_terminal = native_claude(pid=5151, ppid=6000, args=[])
    terminal = proc(6000, "Ghostty", ["ghostty"], ppid=1)
    source = static_source([from_hermes, from_terminal, terminal],
                           environ={4242: hermes_env(), 5151: {"HOME": "/home/dev"}}, cwd={5151: "/work/other"})
    b = backend(source, adapter, tmp_path)

    payload = b.runs_payload()  # default scope comes from the settings
    assert payload["scope"] == "hermes" and payload["default_scope"] == "hermes"
    [run] = payload["runs"]
    assert set(run) == {"id", "agent", "agent_label", "pid", "status", "started_at", "last_activity_at", "cwd",
                        "folder", "origin", "session_id", "has_log", "log_note", "model", "args_summary",
                        "latest", "embedded"}
    assert run["id"] == f"claude:{SID_A}" and run["agent_label"] == "Claude Code"
    assert run["origin"]["kind"] == "hermes_chat" and run["origin"]["hermes_session_id"] == "local-chat-1"
    assert run["folder"] == "demo-app" and run["has_log"] and run["model"] == "opus"
    assert run["latest"] == "Bash: make test" and run["args_summary"] == "-p --model opus"
    for secret in (SECRET_PROMPT, "sk-invented-key", "invented-user", "HERMES_SESSION_PLATFORM"):
        assert secret not in json.dumps(payload)

    everything = b.runs_payload("all")
    assert {r["pid"] for r in everything["runs"]} == {4242, 5151}
    other = next(r for r in everything["runs"] if r["pid"] == 5151)
    assert other["id"] == f"claude:pid:5151:{int(START)}" and other["origin"]["label"] == "Terminal"
    assert other["has_log"] is False and other["log_note"] == "No log found yet."
    assert set(payload) >= {"runs", "scanned_at", "warnings", "platform"}


def test_model_falls_back_to_the_transcript(adapter, home, tmp_path):
    home.session_file(4242, SID_A)
    home.transcript(SID_A, [assistant([{"type": "text", "text": "hi"}], model="claude-invented-1")])
    [run] = backend(static_source([native_claude()]), adapter, tmp_path).runs_payload("all")["runs"]
    assert run["model"] == "claude-invented-1"


def test_latest_is_the_last_tool_call_or_message_never_the_prompt(adapter, home, tmp_path):
    from conftest import tool_result
    home.session_file(4242, SID_A)
    home.transcript(SID_A, [tool_use("Bash", {"command": "make build\nmake test"}), tool_result("ok"),
                            user_prompt(SECRET_PROMPT)])
    [run] = backend(static_source([native_claude()]), adapter, tmp_path).runs_payload("all")["runs"]
    assert run["latest"] == "Bash: make build"


def test_embedded_runs_hidden_unless_the_setting_is_on(adapter, tmp_path):
    gateway = proc(300, "python3.14", ["python3", "-m", "hermes_cli.main", "gateway", "run"], ppid=1)
    model_call = native_claude(pid=301, ppid=300, args=["-p", "--input-format", "stream-json", "--output-format",
                                                         "stream-json", "--no-session-persistence"])
    source = static_source([gateway, model_call])
    hidden = backend(source, adapter, tmp_path).runs_payload("all")
    assert hidden["runs"] == [] and hidden["hidden_embedded"] == 1
    shown = backend(source, adapter, tmp_path, {"show_embedded": True}).runs_payload("all")
    [run] = shown["runs"]
    assert run["embedded"] is True and run["origin"]["kind"] == "hermes" and run["has_log"] is False
    assert run["log_note"] == "This run does not save its session, so there is no live log."


def test_embedded_runs_are_not_remembered(adapter, tmp_path):
    p = native_claude(args=["-p", "--input-format", "stream-json", "--no-session-persistence"])
    store = RunStore(tmp_path / "runs.json")
    backend(static_source([p]), adapter, tmp_path, store=store).runs_payload("all")
    assert store.runs == {}


def test_two_runs_in_one_folder_do_not_share_a_guessed_log(adapter, home, tmp_path):
    home.session_file(1, SID_A, cwd=CWD)
    path = home.transcript(SID_A, [user_prompt("x")])
    os.utime(path, (START + 5, START + 5))
    owner = native_claude(pid=1)
    guesser = native_claude(pid=2)  # no sessions file: falls back to "newest log in this folder"
    source = static_source([owner, guesser], cwd={2: CWD})
    runs = backend(source, adapter, tmp_path).runs_payload("all")["runs"]
    by_pid = {r["pid"]: r for r in runs}
    assert by_pid[1]["session_id"] == SID_A and by_pid[1]["has_log"]
    assert by_pid[2]["session_id"] is None and not by_pid[2]["has_log"]
    assert len({r["id"] for r in runs}) == 2


def test_one_broken_run_never_breaks_the_list(adapter, home, tmp_path, monkeypatch):
    good = native_claude(pid=1)
    bad = native_claude(pid=2)
    real = adapter.describe

    def flaky(p, *a, **k):
        if p.pid == 2:
            raise RuntimeError("invented failure")
        return real(p, *a, **k)

    monkeypatch.setattr(adapter, "describe", flaky)
    payload = backend(static_source([good, bad]), adapter, tmp_path).runs_payload("all")
    assert [r["pid"] for r in payload["runs"]] == [1]
    assert payload["warnings"] == ["Claude Code process 2: RuntimeError: invented failure"]


def test_process_list_failure_is_a_warning(adapter, tmp_path):
    class Broken(StaticSource):
        def processes(self):
            raise OSError("no /proc")

    payload = backend(Broken([]), adapter, tmp_path).runs_payload("all")
    assert payload["runs"] == [] and "Could not read the process list" in payload["warnings"][0]


def test_psutil_missing(monkeypatch, adapter, tmp_path):
    import sys
    import procs
    monkeypatch.setitem(sys.modules, "psutil", None)  # makes `import psutil` raise ImportError
    source = procs.PsutilSource()
    assert source.available is False and source.processes() == [] and source.environ(1) is None
    payload = backend(source, adapter, tmp_path).runs_payload("all")
    assert payload["runs"] == [] and "psutil is not available" in payload["warnings"][0]


def test_sorting_newest_activity_first_finished_last(adapter, home, tmp_path):
    for pid, sid, age in ((1, SID_A, 300), (2, SID_B, 10)):
        home.session_file(pid, sid)
        path = home.transcript(sid, [user_prompt("x")], folder=f"f{pid}")
        os.utime(path, (START + 600 - age, START + 600 - age))
    store = RunStore(tmp_path / "runs.json")
    clock = Clock(START + 600)
    b = backend(static_source([native_claude(pid=1), native_claude(pid=2)]), adapter, tmp_path, clock=clock, store=store)
    assert [r["pid"] for r in b.runs_payload("all")["runs"]] == [2, 1]
    # pid 2 exits: it moves below the live one even though its activity is newer
    b = backend(static_source([native_claude(pid=1)]), adapter, tmp_path, clock=clock, store=store)
    runs = b.runs_payload("all")["runs"]
    assert [(r["pid"], r["status"]) for r in runs] == [(1, "running"), (2, "finished")]


# --- finished-run memory -----------------------------------------------------------------------


def test_finished_runs_are_remembered_then_expire(adapter, home, tmp_path):
    home.session_file(4242, SID_A)
    home.transcript(SID_A, [user_prompt("x"), tool_use("Read", {"file_path": "/work/demo-app/a.py"})])
    data = tmp_path / "data" / "runs.json"
    clock = Clock(START + 60)
    live = static_source([native_claude()], environ={4242: hermes_env()})
    backend(live, adapter, tmp_path, clock=clock, store=RunStore(data)).runs_payload()
    assert data.is_file()

    # The process is gone; a new backend (e.g. after a gateway restart) still lists it.
    clock.t = START + 3600
    gone = static_source([])
    [run] = backend(gone, adapter, tmp_path, {"keep_finished_hours": 6}, clock, RunStore(data)).runs_payload()["runs"]
    assert run["status"] == "finished" and run["id"] == f"claude:{SID_A}"
    assert run["origin"]["kind"] == "hermes_chat" and run["latest"] == "Read: /work/demo-app/a.py"
    events = backend(gone, adapter, tmp_path, {}, clock, RunStore(data)).events_payload(run["id"])
    assert [e["kind"] for e in events["events"]] == ["prompt", "tool"] and events["done"] is True

    clock.t = START + 3600 + 7 * 3600
    assert backend(gone, adapter, tmp_path, {"keep_finished_hours": 6}, clock, RunStore(data)).runs_payload()["runs"] == []
    assert json.loads(data.read_text("utf-8"))["runs"] == {}


def test_a_damaged_memory_file_is_a_warning_not_a_crash(adapter, tmp_path):
    data = tmp_path / "runs.json"
    data.write_text("{ this is not json", "utf-8")
    payload = backend(static_source([]), adapter, tmp_path, store=RunStore(data)).runs_payload("all")
    assert payload["runs"] == [] and "starting fresh" in payload["warnings"][0]


def test_pid_id_keeps_working_after_the_session_id_appears(adapter, home, tmp_path):
    p = native_claude()
    store = RunStore(tmp_path / "runs.json")
    source = static_source([p], cwd={4242: "/nowhere"})
    [early] = backend(source, adapter, tmp_path, store=store).runs_payload("all")["runs"]
    assert early["id"].startswith("claude:pid:4242:")
    home.session_file(4242, SID_A)
    home.transcript(SID_A, [user_prompt("now it has a log")])
    [later] = backend(source, adapter, tmp_path, store=store).runs_payload("all")["runs"]
    assert later["id"] == f"claude:{SID_A}"
    got = backend(source, adapter, tmp_path, store=store).events_payload(early["id"])
    assert got["id"] == later["id"] and got["events"][0]["detail"] == "now it has a log"


def test_store_records_custom_config_dirs_for_the_next_scan(adapter, home, tmp_path):
    custom = tmp_path / "elsewhere"
    home.session_file(4242, SID_A, config=custom)
    home.transcript(SID_A, [user_prompt("x")], config=custom)
    store = RunStore(tmp_path / "runs.json")
    source = static_source([native_claude()], environ={4242: {"CLAUDE_CONFIG_DIR": str(custom)}})
    backend(source, adapter, tmp_path, store=store).runs_payload("all")
    assert custom in store.config_dirs("claude")


# --- Claude Code's ripgrep helpers --------------------------------------------------------------

RG_ARGS = ["--no-config", "--files", "--null", "--glob", "**/*.py", "--sort=modified", "--no-ignore", "--hidden", "."]


def test_ripgrep_helper_of_a_hermes_run_is_not_listed(adapter, home, tmp_path):
    # The helper inherits the run's environment, so it would read as a second Hermes-chat run.
    home.session_file(4242, SID_A)
    run = native_claude(args=["-p", SECRET_PROMPT])
    helper = ProcInfo(pid=5001, ppid=4242, name="2.1.0", exe="/opt/tools/claude/versions/2.1.0",
                      cmdline=["rg", *RG_ARGS], create_time=START + 5)
    source = static_source([run, helper], environ={4242: hermes_env(), 5001: hermes_env()}, cwd={5001: "/Applications"})
    store = RunStore(tmp_path / "runs.json")
    runs = backend(source, adapter, tmp_path, store=store).runs_payload("all")["runs"]
    assert [r["pid"] for r in runs] == [4242]
    assert list(store.runs) == [f"claude:{SID_A}"]


def test_nested_run_through_a_shell_is_listed(adapter, home, tmp_path):
    outer = native_claude(pid=4242, args=["-p", SECRET_PROMPT])
    shell = proc(4300, "zsh", ["/bin/zsh", "-c", "claude -p ..."], ppid=4242)
    inner = native_claude(pid=4301, ppid=4300, args=["-p", "--model", "sonnet"])
    source = static_source([outer, shell, inner], environ={4242: hermes_env(), 4301: hermes_env()},
                           cwd={4242: "/nowhere", 4301: "/nowhere"})
    runs = backend(source, adapter, tmp_path).runs_payload("all")["runs"]
    assert sorted(r["pid"] for r in runs) == [4242, 4301]


def test_remembered_helpers_from_older_versions_are_dropped(adapter, tmp_path):
    data = tmp_path / "runs.json"
    real = {"id": f"claude:{SID_A}", "agent": "claude", "pid": 1, "start": START, "session_id": SID_A,
            "args_summary": "-p --model opus --output-format json", "finished_at": int(START * 1000) + 60_000,
            "origin": {"kind": "hermes_chat", "label": "Hermes chat"}}
    helper_id = f"claude:pid:5001:{int(START)}"
    helper = {"id": helper_id, "agent": "claude", "pid": 5001, "start": START, "session_id": None,
              "args_summary": "--no-config --files --null --glob --sort --no-ignore --hidden",
              "finished_at": int(START * 1000) + 60_000, "origin": {"kind": "hermes_chat", "label": "Hermes chat"}}
    data.write_text(json.dumps({"version": 1, "runs": {real["id"]: real, helper_id: helper},
                                "aliases": {"claude:pid:9:1": helper_id}}), "utf-8")
    store = RunStore(data)
    runs = backend(static_source([]), adapter, tmp_path, store=store).runs_payload("all")["runs"]
    assert [r["id"] for r in runs] == [real["id"]]
    saved = json.loads(data.read_text("utf-8"))
    assert list(saved["runs"]) == [real["id"]] and saved["aliases"] == {}


# --- events through the backend ---------------------------------------------------------------


def test_events_payload_and_unknown_ids(adapter, home, tmp_path):
    home.session_file(4242, SID_A)
    path = home.transcript(SID_A, [user_prompt("go"), tool_use("Bash", {"command": "ls"})])
    b = backend(static_source([native_claude()]), adapter, tmp_path)
    run_id = b.runs_payload("all")["runs"][0]["id"]
    first = b.events_payload(run_id, None, 300)
    assert [e["kind"] for e in first["events"]] == ["prompt", "tool"] and first["done"] is False
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(assistant([{"type": "text", "text": "listed"}])) + "\n")
    nxt = b.events_payload(run_id, first["cursor"])
    assert [e["title"] for e in nxt["events"]] == ["listed"]
    garbage = b.events_payload(run_id, "not-a-number")
    assert len(garbage["events"]) == 3  # an unusable cursor starts again from the end
    for bad in ("claude:00000000-0000-4000-8000-000000000000", "../../etc/passwd", ""):
        with pytest.raises(runs_core.UnknownRun):
            b.events_payload(bad)


def test_events_of_a_run_without_log(adapter, tmp_path):
    p = native_claude(args=["-p", "--no-session-persistence"])
    b = backend(static_source([p]), adapter, tmp_path, {"show_embedded": True})
    run_id = b.runs_payload("all")["runs"][0]["id"]
    got = b.events_payload(run_id)
    assert got["events"] == [] and got["has_log"] is False and "does not save" in got["note"]


def test_show_thinking_setting_reaches_the_events(adapter, home, tmp_path):
    home.session_file(4242, SID_A)
    home.transcript(SID_A, [assistant([{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "ok"}])])
    source = static_source([native_claude()])
    run_id = f"claude:{SID_A}"
    backend(source, adapter, tmp_path).runs_payload("all")
    assert [e["kind"] for e in backend(source, adapter, tmp_path).events_payload(run_id)["events"]] == ["text"]
    kinds = [e["kind"] for e in backend(source, adapter, tmp_path, {"show_thinking": True}).events_payload(run_id)["events"]]
    assert kinds == ["thinking", "text"]


# --- settings and status ----------------------------------------------------------------------


def test_settings_defaults_and_clamping():
    assert normalize_settings(None) == runs_core.DEFAULT_SETTINGS
    s = normalize_settings({"default_scope": "ALL", "show_embedded": "true", "show_thinking": 0,
                            "keep_finished_hours": "-5", "quiet_minutes": "abc"})
    assert s == {"default_scope": "all", "show_embedded": True, "show_thinking": False,
                 "keep_finished_hours": 0, "quiet_minutes": 10}
    assert normalize_settings({"default_scope": "nonsense"})["default_scope"] == "hermes"


def test_status_payload(adapter, home, tmp_path):
    home.session_file(4242, SID_A)
    payload = backend(static_source([native_claude()]), adapter, tmp_path).status_payload()
    assert payload["plugin"] == "agent-runs" and payload["version"] == runs_core.__version__
    assert payload["counts"]["running"] == 1 and payload["counts"]["total"] == 1
    assert payload["config_dirs"] == [{"agent": "claude", "path": str(home.claude), "exists": True}]
    assert payload["settings"] == runs_core.DEFAULT_SETTINGS
