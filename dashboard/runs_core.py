"""Agent Runs core: finds coding-agent runs, merges them with remembered ones, builds API payloads.

Nothing here knows about a particular agent. Each agent (Claude Code today) is an adapter object
with the interface documented in `Adapter`; the core asks it which processes are its own, what a
process is doing, and how to read its log. The core owns what every agent shares: where a run was
started from (`detect_origin`), its status, the memory of finished runs and the payload shapes.

No web framework and no psutil import here, so the whole module is tested with a fake process list.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

PLUGIN_ID = "agent-runs"
__version__ = "0.1.0"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "default_scope": "hermes",
    "show_embedded": False,
    "show_thinking": False,
    "keep_finished_hours": 6,
    "quiet_minutes": 10,
}
SCOPES = ("hermes", "all")
HERMES_KINDS = ("hermes_chat", "hermes_cron", "hermes")
DEFAULT_EVENT_LIMIT = 300
MAX_EVENT_LIMIT = 2000
_TRUE = {"1", "true", "yes", "on"}


class UnknownRun(KeyError):
    """A run id that discovery and the finished-run memory don't know."""


# --- settings ----------------------------------------------------------------------------------


def plugin_settings() -> Dict[str, Any]:
    """`plugins.entries.agent-runs.settings` from Hermes' config (read-only), or {}."""
    try:
        from hermes_cli.config import load_config_readonly  # documented for plugin backends
    except Exception:
        return {}
    try:
        entries = (load_config_readonly().get("plugins") or {}).get("entries") or {}
        settings = (entries.get(PLUGIN_ID) or {}).get("settings") or {}
        return dict(settings) if isinstance(settings, dict) else {}
    except Exception:
        return {}


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _TRUE
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def normalize_settings(raw: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Settings with defaults filled in and out-of-range values clamped, never an error."""
    raw = raw or {}
    scope = str(raw.get("default_scope") or DEFAULT_SETTINGS["default_scope"]).strip().lower()
    return {
        "default_scope": scope if scope in SCOPES else DEFAULT_SETTINGS["default_scope"],
        "show_embedded": _as_bool(raw.get("show_embedded"), DEFAULT_SETTINGS["show_embedded"]),
        "show_thinking": _as_bool(raw.get("show_thinking"), DEFAULT_SETTINGS["show_thinking"]),
        "keep_finished_hours": _as_int(raw.get("keep_finished_hours"), DEFAULT_SETTINGS["keep_finished_hours"], 0, 24 * 30),
        "quiet_minutes": _as_int(raw.get("quiet_minutes"), DEFAULT_SETTINGS["quiet_minutes"], 1, 24 * 60),
    }


def data_dir() -> Path:
    """`plugin-data/agent-runs` in the active profile's Hermes home."""
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "plugin-data" / PLUGIN_ID


# --- the adapter seam --------------------------------------------------------------------------


class Adapter(Protocol):
    """One coding agent. The core calls these in this order on every scan:

    begin_scan(config_dirs)      once, before looking at processes (config_dirs: folders this agent
                                 used in remembered runs, e.g. a custom CLAUDE_CONFIG_DIR)
    match_process(proc) -> bool  cheap: name, exe and argv only
    claims_pid(pid) -> bool      the agent itself recorded this pid as one of its runs
    describe(proc, env, source, parent_is_hermes) -> dict | None
                                 the run (keys below), or None when the process is not a run
    summarize_latest(path) -> str | None, transcript_model(path) -> str | None
    read_events(path, cursor, limit, show_thinking) -> {"events", "cursor", "reset"}
    find_transcript(session_id, config_dir) -> Path | None
    config_dirs() -> list[Path]  for /status

    describe() keys: session_id, session_source, transcript (Path|None), config_dir (Path),
    cwd, embedded, args_summary, model, interactive, status_hint ("busy"|"idle"|None),
    activity_ms (int|None), log_note (str|None).
    """

    agent: str
    label: str


# --- where a run was started from ---------------------------------------------------------------

# One table for every rule that names an app from a parent process. Matched against the lowercased
# process name (and argv[0]'s file name) without ".exe"; an entry ending in "*" is a prefix, so
# helper processes such as "Code Helper (Plugin)" match too. The closest matching parent wins.
ORIGIN_RULES: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("terminal", "Terminal", ("terminal", "iterm2", "iterm", "wezterm", "wezterm-gui", "kitty",
                              "alacritty", "ghostty", "windowsterminal", "openconsole", "conhost",
                              "gnome-terminal", "gnome-terminal-server", "konsole", "xterm",
                              "tmux", "tmux: server", "screen", "warp")),
    ("ide", "VS Code", ("code", "code helper*", "code - insiders*", "code-insiders", "codium", "vscodium*")),
    ("ide", "Cursor", ("cursor", "cursor helper*")),
    ("ide", "Windsurf", ("windsurf", "windsurf helper*")),
    ("ide", "Zed", ("zed", "zed-editor")),
    ("ide", "IntelliJ IDEA", ("idea", "idea64")),
    ("ide", "PyCharm", ("pycharm", "pycharm64")),
    ("ide", "WebStorm", ("webstorm", "webstorm64")),
    ("ide", "GoLand", ("goland", "goland64")),
    ("ide", "CLion", ("clion", "clion64")),
    ("ide", "Rider", ("rider", "rider64")),
    ("ide", "RubyMine", ("rubymine", "rubymine64")),
    ("ide", "PhpStorm", ("phpstorm", "phpstorm64")),
    ("ide", "RustRover", ("rustrover", "rustrover64")),
    ("ide", "Android Studio", ("studio", "studio64")),
)
_MAX_PARENT_DEPTH = 24


def path_name(path: str) -> str:
    """The last part of a path written with / or \\ (process paths may come from any OS)."""
    return re.split(r"[\\/]", path or "")[-1]


def _plain_name(name: str) -> str:
    name = (name or "").strip().lower()
    return name[:-4] if name.endswith(".exe") else name


def _rule_for(names: Iterable[str]) -> Optional[Tuple[str, str]]:
    for name in names:
        if not name:
            continue
        for kind, label, patterns in ORIGIN_RULES:
            for pattern in patterns:
                if pattern.endswith("*") and name.startswith(pattern[:-1]) or name == pattern:
                    return kind, label
    return None


def is_hermes_process(proc: Any) -> bool:
    """A Hermes CLI, gateway or desktop app process (by its own name and first arguments only)."""
    if proc is None:
        return False
    if _plain_name(proc.name or "") in ("hermes", "hermes-agent"):
        return True
    argv = list(proc.cmdline or [])
    if not argv:
        return False
    first = _plain_name(path_name(argv[0]))
    if first in ("hermes", "hermes-agent"):
        return True
    if first.startswith("python") or first in ("uv", "uvx"):
        for i, arg in enumerate(argv[1:6], start=1):
            if arg == "-m" and i + 1 < len(argv) and argv[i + 1].split(".")[0] in ("hermes_cli", "tui_gateway"):
                return True
            base = _plain_name(path_name(arg))
            if base == "hermes" or arg.replace("\\", "/").endswith("hermes_cli/main.py"):
                return True
    return False


def detect_origin(env: Optional[Mapping[str, str]], proc: Any, procs_by_pid: Mapping[int, Any]) -> Dict[str, Any]:
    """Where a run came from: Hermes' own environment variables first (they survive detaching),
    then the closest known app among the process's parents."""
    if env:
        # Only the chat id leaves the backend (for the title and "Open chat"); no other env value does.
        session_id = (env.get("HERMES_SESSION_ID") or "").strip()
        if (env.get("HERMES_CRON_SESSION") or "").strip().lower() in _TRUE:
            origin: Dict[str, Any] = {"kind": "hermes_cron", "label": "Hermes cron"}
            if session_id:
                origin["hermes_session_id"] = session_id
            return origin
        if session_id:
            return {"kind": "hermes_chat", "label": "Hermes chat", "hermes_session_id": session_id}

    seen = {proc.pid}
    ppid = proc.ppid
    for _ in range(_MAX_PARENT_DEPTH):
        if not ppid or ppid in seen:
            break
        parent = procs_by_pid.get(ppid)
        if parent is None:  # gone, or the OS would not show it to us (launchd / PID 1 on macOS)
            break
        seen.add(ppid)
        if is_hermes_process(parent):
            return {"kind": "hermes", "label": "Hermes"}
        argv0 = path_name(parent.cmdline[0]) if parent.cmdline else ""
        rule = _rule_for([_plain_name(parent.name or ""), _plain_name(argv0)])
        if rule:
            return {"kind": rule[0], "label": rule[1]}
        ppid = parent.ppid
    return {"kind": "other", "label": "Other"}


# --- status ------------------------------------------------------------------------------------


def run_status(alive: bool, activity_ms: int, now_ms: int, quiet_minutes: int,
               interactive: bool, status_hint: Optional[str]) -> str:
    if not alive:
        return "finished"
    if interactive and status_hint == "idle":
        return "waiting"
    if now_ms - activity_ms <= quiet_minutes * 60_000:
        return "running"
    return "quiet"


def _mtime_ms(path: Optional[Path]) -> Optional[int]:
    if path is None:
        return None
    try:
        return int(Path(path).stat().st_mtime * 1000)
    except OSError:
        return None


# --- finished-run memory -----------------------------------------------------------------------

_REMEMBERED = ("id", "agent", "pid", "start", "session_id", "transcript", "config_dir", "origin",
               "cwd", "args_summary", "model", "started_at", "last_activity_at", "interactive",
               "log_note", "latest")


class RunStore:
    """Runs seen recently, kept in `<data dir>/runs.json` so finished ones stay visible for
    `keep_finished_hours` and their logs can still be opened."""

    _SAVE_EVERY_S = 30.0

    def __init__(self, path: Optional[Path]):
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.runs: Dict[str, Dict[str, Any]] = {}
        self.aliases: Dict[str, str] = {}
        self.warning: Optional[str] = None
        self._dirty = False
        self._last_save = 0.0
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text("utf-8"))
            runs = data.get("runs") if isinstance(data, dict) else None
            aliases = data.get("aliases") if isinstance(data, dict) else None
            self.runs = {k: v for k, v in (runs or {}).items() if isinstance(v, dict) and v.get("id") == k}
            self.aliases = {k: v for k, v in (aliases or {}).items() if isinstance(k, str) and isinstance(v, str)}
        except Exception as e:  # a damaged file only costs the memory of finished runs
            self.runs, self.aliases = {}, {}
            self.warning = f"Could not read the remembered runs ({type(e).__name__}); starting fresh."

    def save(self, force: bool = False, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        if not self.path or not (force or self._dirty or now - self._last_save >= self._SAVE_EVERY_S):
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"version": 1, "runs": self.runs, "aliases": self.aliases}), "utf-8")
            os.replace(tmp, self.path)  # atomic on POSIX and Windows
            self._dirty = False
            self._last_save = now
        except OSError as e:
            self.warning = f"Could not save the remembered runs: {e}"

    def remember(self, record: Dict[str, Any]) -> None:
        old = self.runs.get(record["id"])
        keep = {k: record.get(k) for k in _REMEMBERED}
        keep["finished_at"] = None
        if old is None or any(old.get(k) != keep.get(k) for k in _REMEMBERED if k not in ("last_activity_at", "latest")):
            self._dirty = True
        self.runs[record["id"]] = keep
        pid_id = pid_run_id(record["agent"], record["pid"], record["start"])
        if record["id"] != pid_id:
            # The run was listed under its pid before its session id was known: keep that id working.
            if self.runs.pop(pid_id, None) is not None or self.aliases.get(pid_id) != record["id"]:
                self._dirty = True
            self.aliases[pid_id] = record["id"]

    def finish_missing(self, live_ids: Iterable[str], now_ms: int) -> None:
        live = set(live_ids)
        for run_id, rec in self.runs.items():
            if run_id not in live and not rec.get("finished_at"):
                rec["finished_at"] = now_ms
                self._dirty = True

    def expire(self, keep_hours: int, now_ms: int) -> None:
        cutoff = now_ms - keep_hours * 3_600_000
        gone = [k for k, r in self.runs.items() if r.get("finished_at") and r["finished_at"] <= cutoff]
        for run_id in gone:
            del self.runs[run_id]
        if gone:
            self.aliases = {k: v for k, v in self.aliases.items() if v in self.runs}
            self._dirty = True

    def resolve(self, run_id: str) -> Optional[str]:
        if run_id in self.runs:
            return run_id
        target = self.aliases.get(run_id)
        return target if target in self.runs else None

    def config_dirs(self, agent: str) -> List[Path]:
        dirs = {r.get("config_dir") for r in self.runs.values() if r.get("agent") == agent and r.get("config_dir")}
        return [Path(d) for d in sorted(dirs)]


def run_id_for(agent: str, session_id: Optional[str], pid: int, start: float) -> str:
    return f"{agent}:{session_id}" if session_id else pid_run_id(agent, pid, start)


def pid_run_id(agent: str, pid: int, start: float) -> str:
    return f"{agent}:pid:{pid}:{int(start)}"


# --- the backend -------------------------------------------------------------------------------


class Backend:
    """What the API routes call. One instance per request is fine: the store and the process
    source are passed in and outlive it."""

    def __init__(self, source: Any, adapters: Sequence[Any], store: RunStore,
                 settings: Optional[Mapping[str, Any]] = None, clock: Callable[[], float] = time.time):
        self.source = source
        self.adapters = list(adapters)
        self.by_agent = {a.agent: a for a in self.adapters}
        self.store = store
        self.settings = normalize_settings(settings)
        self.clock = clock

    # -- discovery --

    def _scan(self) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Live runs (every one, embedded included) and warnings."""
        warnings: List[str] = []
        if not getattr(self.source, "available", True):
            warnings.append("psutil is not available to the Hermes backend, so running agents cannot be "
                            "listed. Finished runs remembered earlier are still shown.")
        try:
            procs = self.source.processes()
        except Exception as e:
            warnings.append(f"Could not read the process list: {type(e).__name__}: {e}")
            procs = []
        by_pid = {p.pid: p for p in procs}
        live: List[Dict[str, Any]] = []
        for adapter in self.adapters:
            try:
                adapter.begin_scan(self.store.config_dirs(adapter.agent))
            except Exception as e:
                warnings.append(f"{adapter.label}: {type(e).__name__}: {e}")
                continue
            for proc in procs:
                try:
                    if not (adapter.match_process(proc) or adapter.claims_pid(proc.pid)):
                        continue
                    live_run = self._describe(adapter, proc, by_pid)
                except Exception as e:  # one broken run never breaks the list
                    warnings.append(f"{adapter.label} process {proc.pid}: {type(e).__name__}: {e}")
                    continue
                if live_run is not None:
                    live.append(live_run)
        return self._dedupe(live), warnings

    def _describe(self, adapter: Any, proc: Any, by_pid: Mapping[int, Any]) -> Optional[Dict[str, Any]]:
        try:
            env = self.source.environ(proc.pid)
        except Exception:  # AccessDenied and friends: fall back to the parent chain
            env = None
        parent_is_hermes = is_hermes_process(by_pid.get(proc.ppid)) if proc.ppid else False
        info = adapter.describe(proc, env, self.source, parent_is_hermes)
        if info is None:
            return None
        origin = detect_origin(env, proc, by_pid)
        transcript = info.get("transcript")
        start_ms = int(proc.create_time * 1000)
        activity = max(x for x in (start_ms, info.get("activity_ms"), _mtime_ms(transcript)) if x is not None)
        return {
            "agent": adapter.agent, "pid": proc.pid, "start": proc.create_time,
            "session_id": info.get("session_id"), "session_source": info.get("session_source"),
            "transcript": str(transcript) if transcript else None,
            "config_dir": str(info["config_dir"]) if info.get("config_dir") else None,
            "origin": origin, "cwd": info.get("cwd"), "embedded": bool(info.get("embedded")),
            "args_summary": info.get("args_summary") or "", "model": info.get("model"),
            "interactive": bool(info.get("interactive")), "status_hint": info.get("status_hint"),
            "started_at": start_ms, "last_activity_at": activity, "log_note": info.get("log_note"),
        }

    @staticmethod
    def _dedupe(live: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Give every run a unique id. A session id guessed from the folder loses to one the agent
        recorded itself; two processes that truly share a session are told apart by pid."""
        order = {"sessions_file": 0, "argv": 1, "cwd": 2}
        live.sort(key=lambda r: (order.get(r.get("session_source") or "", 3), r["start"]))
        taken: Dict[str, Dict[str, Any]] = {}
        for run in live:
            sid = run["session_id"]
            if sid and sid in taken:
                if run.get("session_source") == "cwd":
                    run.update(session_id=None, session_source=None, transcript=None,
                               log_note="Not found yet: another run in this folder owns the newest log.")
                    sid = None
                else:
                    run["id"] = f"{run['agent']}:{sid}:pid:{run['pid']}"
                    continue
            run["id"] = run_id_for(run["agent"], sid, run["pid"], run["start"])
            if sid:
                taken[sid] = run
        return live

    def _summaries(self, adapter: Any, rec: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        path = Path(rec["transcript"]) if rec.get("transcript") else None
        if path is None:
            return None, None
        latest = adapter.summarize_latest(path)
        model = adapter.transcript_model(path)
        return latest, model

    def _payload(self, rec: Dict[str, Any], status: str) -> Dict[str, Any]:
        adapter = self.by_agent.get(rec["agent"])
        cwd = rec.get("cwd")
        return {
            "id": rec["id"], "agent": rec["agent"],
            "agent_label": adapter.label if adapter else rec["agent"],
            "pid": rec["pid"], "status": status,
            "started_at": rec.get("started_at"), "last_activity_at": rec.get("last_activity_at"),
            "cwd": cwd, "folder": path_name(cwd.rstrip("/\\")) if cwd else None,
            "origin": rec.get("origin") or {"kind": "other", "label": "Other"},
            "session_id": rec.get("session_id"), "has_log": bool(rec.get("transcript")),
            "log_note": None if rec.get("transcript") else rec.get("log_note"),
            "model": rec.get("model"), "args_summary": rec.get("args_summary") or "",
            "latest": rec.get("latest"), "embedded": bool(rec.get("embedded")),
        }

    def collect(self) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Every run to show (live and remembered), as payloads, with warnings. Updates the memory."""
        now_ms = int(self.clock() * 1000)
        with self.store.lock:
            live, warnings = self._scan()
            out: List[Dict[str, Any]] = []
            for run in live:
                adapter = self.by_agent[run["agent"]]
                try:
                    latest, transcript_model = self._summaries(adapter, run)
                except Exception as e:
                    warnings.append(f"{adapter.label} run {run['id']}: {type(e).__name__}: {e}")
                    latest, transcript_model = None, None
                run["latest"] = latest
                run["model"] = run.get("model") or transcript_model
                status = run_status(True, run["last_activity_at"], now_ms, self.settings["quiet_minutes"],
                                    run["interactive"], run.get("status_hint"))
                if not run["embedded"]:  # Hermes' own model calls come and go by the second
                    self.store.remember(run)
                out.append(self._payload(run, status))
            self.store.finish_missing([r["id"] for r in live if not r["embedded"]], now_ms)
            self.store.expire(self.settings["keep_finished_hours"], now_ms)
            for rec in self.store.runs.values():
                if not rec.get("finished_at"):
                    continue
                path = Path(rec["transcript"]) if rec.get("transcript") else None
                rec["last_activity_at"] = max(x for x in (rec.get("last_activity_at") or 0, _mtime_ms(path)) if x is not None)
                adapter = self.by_agent.get(rec.get("agent"))
                if adapter is not None and path is not None:
                    try:  # the last lines may have landed after the last live scan
                        rec["latest"] = adapter.summarize_latest(path) or rec.get("latest")
                    except Exception:
                        pass  # keep what was seen while it ran
                out.append(self._payload(rec, "finished"))
            if self.store.warning:
                warnings.append(self.store.warning)
            self.store.save()
        return out, warnings

    # -- payloads --

    def runs_payload(self, scope: Optional[str] = None) -> Dict[str, Any]:
        scope = scope if scope in SCOPES else self.settings["default_scope"]
        runs, warnings = self.collect()
        hidden = sum(1 for r in runs if r["embedded"])
        if not self.settings["show_embedded"]:
            runs = [r for r in runs if not r["embedded"]]
        if scope == "hermes":
            runs = [r for r in runs if r["origin"].get("kind") in HERMES_KINDS]
        runs.sort(key=lambda r: (r["status"] == "finished", -(r.get("last_activity_at") or 0)))
        return {
            "runs": runs, "scope": scope, "default_scope": self.settings["default_scope"],
            "scanned_at": int(self.clock() * 1000), "warnings": warnings,
            "platform": getattr(self.source, "platform", sys.platform),
            "hidden_embedded": 0 if self.settings["show_embedded"] else hidden,
        }

    def _find(self, run_id: str) -> Tuple[Dict[str, Any], bool]:
        """A run by id, from the memory first (cheap), else a fresh scan. Never a client path."""
        with self.store.lock:
            key = self.store.resolve(run_id)
            rec = dict(self.store.runs[key]) if key else None
        if rec is not None:
            alive = not rec.get("finished_at") and bool(self.source.alive(rec["pid"], rec["start"]))
            return rec, alive
        runs, _ = self.collect()
        for run in runs:
            if run["id"] == run_id:
                with self.store.lock:
                    key = self.store.resolve(run_id)
                    if key:
                        return dict(self.store.runs[key]), run["status"] != "finished"
                # embedded runs are not remembered; they have no log anyway
                return {"id": run_id, "agent": run["agent"], "transcript": None, "log_note": run.get("log_note")}, \
                    run["status"] != "finished"
        raise UnknownRun(run_id)

    def events_payload(self, run_id: str, cursor: Optional[str] = None,
                       limit: int = DEFAULT_EVENT_LIMIT) -> Dict[str, Any]:
        limit = max(1, min(MAX_EVENT_LIMIT, int(limit or DEFAULT_EVENT_LIMIT)))
        rec, alive = self._find(run_id)
        adapter = self.by_agent.get(rec["agent"])
        transcript = Path(rec["transcript"]) if rec.get("transcript") else None
        if adapter is None or transcript is None:
            return {"id": rec["id"], "events": [], "cursor": None, "done": not alive, "reset": False,
                    "has_log": False, "note": rec.get("log_note")}
        offset: Optional[int] = None
        if cursor not in (None, ""):
            try:
                offset = int(str(cursor))
            except ValueError:
                offset = None  # a cursor we never issued: start again from the end of the log
        try:
            got = adapter.read_events(transcript, offset, limit, self.settings["show_thinking"])
        except FileNotFoundError:
            return {"id": rec["id"], "events": [], "cursor": None, "done": True, "reset": False,
                    "has_log": False, "note": "The log file is gone."}
        # `more`: the adapter stopped early (event limit or read cap) and the next call continues.
        return {"id": rec["id"], "events": got["events"], "cursor": None if got["cursor"] is None else str(got["cursor"]),
                "done": (not alive) and not got.get("more"), "reset": bool(got.get("reset")), "has_log": True,
                "note": None}

    def status_payload(self) -> Dict[str, Any]:
        runs, warnings = self.collect()
        counts: Dict[str, int] = {"running": 0, "quiet": 0, "waiting": 0, "finished": 0}
        for r in runs:
            if not r["embedded"]:
                counts[r["status"]] = counts.get(r["status"], 0) + 1
        dirs = []
        for adapter in self.adapters:
            for d in adapter.config_dirs():
                dirs.append({"agent": adapter.agent, "path": str(d), "exists": Path(d).is_dir()})
        return {
            "plugin": PLUGIN_ID, "version": __version__,
            "psutil": bool(getattr(self.source, "available", False)),
            "psutil_version": getattr(self.source, "version", None),
            "platform": getattr(self.source, "platform", sys.platform),
            "agents": [{"id": a.agent, "label": a.label} for a in self.adapters],
            "config_dirs": dirs,
            "counts": {**counts, "embedded": sum(1 for r in runs if r["embedded"]),
                       "total": sum(1 for r in runs if not r["embedded"])},
            "settings": dict(self.settings),
            "data_dir": str(self.store.path.parent) if self.store.path else None,
            "warnings": warnings,
        }
