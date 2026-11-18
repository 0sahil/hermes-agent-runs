"""Claude Code adapter: which processes are Claude Code runs, which session each one is, and what
its transcript says.

How Claude Code leaves traces (checked against Claude Code 2.1):
- A running `claude` writes `<config dir>/sessions/<pid>.json` with its session id, cwd, start
  time (ms) and status ("busy"/"idle"). That file is the authority; the `<pid>.<hash>.key` file
  beside it is a secret and is never opened.
- The transcript is `<config dir>/projects/<folder>/<session id>.jsonl`, one JSON object per line.
  The folder name is derived from the cwd, but that encoding is Claude's business: the file is found
  by session id across every project folder.
- The config dir is `~/.claude`, or the process's own `CLAUDE_CONFIG_DIR`.

Only `user`, `assistant` and `system` lines become events; every other line type (there are many,
and new ones appear) is skipped. Nothing here writes anywhere.
"""
from __future__ import annotations

import json
import os
import re
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

AGENT = "claude"
LABEL = "Claude Code"

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_FLAG_RE = re.compile(r"^--?[A-Za-z][A-Za-z0-9-]{0,40}$")
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9._:\[\]-]{1,64}$")
_RELEVANT_RE = re.compile(rb'"type"\s*:\s*"(?:user|assistant|system)"')
_COMMAND_RE = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.S)

# Flags whose value is shown in the summary: a model or mode name, never free text.
KEPT_VALUE_FLAGS = frozenset({"--model", "--effort", "--permission-mode", "--max-turns",
                              "--output-format", "--input-format"})
# Flags whose value is read (to find the session) but never shown.
ID_FLAGS = frozenset({"--session-id", "--resume", "-r"})
# `claude <subcommand>` processes that are tools, not agent runs.
SUBCOMMANDS = frozenset({"mcp", "doctor", "update", "upgrade", "install", "config", "setup-token",
                         "migrate-installer", "plugin", "plugins", "auth"})
_TRUE = {"1", "true", "yes", "on"}

START_TOLERANCE_S = 10.0  # sessions/<pid>.json startedAt vs. the process start: tells a reused pid apart
PROMPT_CHARS = 2000
RESULT_CHARS = 400
INPUT_CHARS = 300
COMMAND_CHARS = 1000
LINE_CHARS = 160
CHUNK = 256 * 1024
MAX_TAIL_BYTES = 32 * 1024 * 1024
MAX_FORWARD_BYTES = 8 * 1024 * 1024
HEAD_PROMPT_BYTES = 4 * 1024 * 1024
SUMMARY_TAIL_BYTES = (512 * 1024, 4 * 1024 * 1024)
CWD_HEAD_BYTES = 256 * 1024

_summary_cache: "OrderedDict[Tuple[str, int, int], Tuple[Optional[str], Optional[str]]]" = OrderedDict()


def _slash(path: Optional[str]) -> str:
    return (path or "").replace("\\", "/")


def _name(path: Optional[str]) -> str:
    return _slash(path).rsplit("/", 1)[-1]


def _plain(name: str) -> str:
    name = name.lower()
    return name[:-4] if name.endswith(".exe") else name


def _cut(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _first_line(text: str, limit: int = LINE_CHARS) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return _cut(line, limit)
    return ""


def _ts_ms(value: Any) -> Optional[int]:
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


# --- argv --------------------------------------------------------------------------------------


def parse_args(args: List[str]) -> Tuple[List[str], Dict[str, Optional[str]]]:
    """(summary tokens, flag values). Positional arguments (the prompt) and the values of flags
    not in KEPT_VALUE_FLAGS / ID_FLAGS are dropped, so they can never reach the page."""
    tokens: List[str] = []
    values: Dict[str, Optional[str]] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            break  # everything after it is positional
        if not arg.startswith("-") or len(arg) < 2:
            i += 1
            continue
        name, eq, inline = arg.partition("=")
        if not _FLAG_RE.match(name):
            i += 1
            continue
        value: Optional[str] = None
        if name in KEPT_VALUE_FLAGS or name in ID_FLAGS:
            if eq:
                value = inline
            elif i + 1 < len(args) and not args[i + 1].startswith("-"):
                value = args[i + 1]
                i += 1
        values[name] = value
        shown = f"{name} {value}" if name in KEPT_VALUE_FLAGS and value and _SAFE_VALUE_RE.match(value) else name
        if name not in {t.split(" ", 1)[0] for t in tokens}:
            tokens.append(shown)
        i += 1
    return tokens, values


def summarize_args(args: List[str]) -> str:
    """`-p --model opus --output-format json --allowedTools`: flags only, safe values only."""
    return " ".join(parse_args(args)[0])


# --- transcript lines → events -----------------------------------------------------------------


def _event(ts: Optional[int], kind: str, title: str, detail: Optional[str] = None) -> Dict[str, Any]:
    ev: Dict[str, Any] = {"ts": ts, "kind": kind, "title": title}
    if detail:
        ev["detail"] = detail
    return ev


def _compact_json(value: Any, limit: int = INPUT_CHARS) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        text = str(value)
    return _cut(text, limit)


def tool_detail(name: str, inp: Any) -> str:
    """The one input that says what a tool call does."""
    if not isinstance(inp, dict):
        return _compact_json(inp)
    if name == "Bash" and isinstance(inp.get("command"), str):
        return _cut(inp["command"], COMMAND_CHARS)
    if name in ("Read", "Edit", "Write", "MultiEdit") and isinstance(inp.get("file_path"), str):
        return inp["file_path"]
    if name == "NotebookEdit" and isinstance(inp.get("notebook_path"), str):
        return inp["notebook_path"]
    if name in ("Grep", "Glob") and isinstance(inp.get("pattern"), str):
        where = inp.get("path")
        return _cut(inp["pattern"], INPUT_CHARS) + (f"  in {where}" if isinstance(where, str) and where else "")
    if name == "WebFetch" and isinstance(inp.get("url"), str):
        return _cut(inp["url"], INPUT_CHARS)
    if name == "WebSearch" and isinstance(inp.get("query"), str):
        return _cut(inp["query"], INPUT_CHARS)
    if name in ("Task", "Agent") and isinstance(inp.get("description"), str):
        return _cut(inp["description"], INPUT_CHARS)
    if name == "TodoWrite" and isinstance(inp.get("todos"), list):
        n = len(inp["todos"])
        return f"{n} todo" + ("" if n == 1 else "s")
    return _compact_json(inp)


def _blocks_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: List[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif block.get("type") == "image":
                    parts.append("[image]")
            elif isinstance(block, str):
                parts.append(block)
    return "\n".join(parts)


def _user_text(ts: Optional[int], text: str) -> List[Dict[str, Any]]:
    text = text.strip()
    if not text:
        return []
    if text.startswith("[Request interrupted"):
        return [_event(ts, "system", "Interrupted by the user")]
    match = _COMMAND_RE.search(text)
    if match and text.startswith("<command-"):
        return [_event(ts, "system", f"Command {_cut(match.group(1), 80)}")]
    if text.startswith(("<local-command-", "<command-", "Caveat: ")):
        return []
    if text.startswith("This session is being continued from a previous conversation"):
        return [_event(ts, "system", "Conversation compacted")]
    return [_event(ts, "prompt", "Prompt", _cut(text, PROMPT_CHARS))]


def line_events(obj: Mapping[str, Any], show_thinking: bool = False) -> List[Dict[str, Any]]:
    """The events in one transcript line. Unknown line or block types give no events."""
    kind = obj.get("type")
    ts = _ts_ms(obj.get("timestamp"))
    message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    content = message.get("content")

    if kind == "assistant":
        if obj.get("isApiErrorMessage"):
            return [_event(ts, "error", "API error", _cut(_blocks_text(content), RESULT_CHARS) or None)]
        out: List[Dict[str, Any]] = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                text = block["text"].strip()
                title = _first_line(text)
                out.append(_event(ts, "text", title, text if text != title else None))
            elif btype == "thinking" and show_thinking:
                thought = block.get("thinking")
                if isinstance(thought, str) and thought.strip():
                    out.append(_event(ts, "thinking", "Thinking", thought.strip()))
            elif btype == "tool_use":
                name = str(block.get("name") or "tool")
                out.append(_event(ts, "tool", name, tool_detail(name, block.get("input")) or None))
        if isinstance(content, str) and content.strip():
            out.append(_event(ts, "text", _first_line(content), content.strip()))
        return out

    if kind == "user":
        if obj.get("isMeta"):
            return []
        if obj.get("isCompactSummary"):
            return [_event(ts, "system", "Conversation compacted")]
        if isinstance(content, str):
            return _user_text(ts, content)
        out = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and isinstance(block.get("text"), str):
                out.extend(_user_text(ts, block["text"]))
            elif btype == "tool_result":
                failed = bool(block.get("is_error"))
                text = _cut(_blocks_text(block.get("content")), RESULT_CHARS)
                out.append(_event(ts, "error" if failed else "result", "Error" if failed else "Result", text or None))
        return out

    if kind == "system":
        subtype = obj.get("subtype")
        if subtype == "api_error":
            err = obj.get("error") if isinstance(obj.get("error"), dict) else {}
            status = err.get("status")
            title = "API error" + (f" {status}" if isinstance(status, int) else "")
            attempt, most = obj.get("retryAttempt"), obj.get("maxRetries")
            if isinstance(attempt, int) and isinstance(most, int):
                title += f", retrying ({attempt}/{most})"
            msg = err.get("message")
            return [_event(ts, "system", title, _cut(msg, RESULT_CHARS) if isinstance(msg, str) and msg else None)]
        if subtype == "compact_boundary":
            return [_event(ts, "system", "Conversation compacted")]
        text = obj.get("content")
        if isinstance(text, str) and text.strip():
            if _COMMAND_RE.search(text) or text.lstrip().startswith("<local-command-"):
                return _user_text(ts, text.lstrip())
            return [_event(ts, "system", _first_line(text), _cut(text, RESULT_CHARS) if "\n" in text.strip() else None)]
        return []
    return []


def _parse(raw: bytes) -> Optional[Dict[str, Any]]:
    if not raw.strip() or not _RELEVANT_RE.search(raw):
        return None
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None  # a damaged or half-written line
    return obj if isinstance(obj, dict) else None


def _events_of(lines: Iterable[bytes], show_thinking: bool) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for raw in lines:
        obj = _parse(raw)
        if obj is not None:
            out.extend(line_events(obj, show_thinking))
    return out


# --- reading a growing JSONL file --------------------------------------------------------------


def _complete_end(f, size: int) -> int:
    """Offset just after the last newline: a line still being written is not read yet."""
    pos = size
    while pos > 0:
        start = max(0, pos - CHUNK)
        f.seek(start)
        data = f.read(pos - start)
        idx = data.rfind(b"\n")
        if idx >= 0:
            return start + idx + 1
        pos = start
    return 0


def _first_prompt(f, show_thinking: bool) -> Optional[Dict[str, Any]]:
    f.seek(0)
    read = 0
    for raw in f:
        read += len(raw)
        if read > HEAD_PROMPT_BYTES:
            break
        for ev in _events_of([raw], show_thinking):
            if ev["kind"] == "prompt":
                return ev
    return None


def tail_events(path: Path, limit: int, show_thinking: bool = False) -> Dict[str, Any]:
    """The last `limit` events, reading backwards from the end so a large file costs little."""
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        end = _complete_end(f, size)
        pos, pending, read = end, b"", 0
        collected: List[Dict[str, Any]] = []
        while pos > 0 and len(collected) < limit and read < MAX_TAIL_BYTES:
            start = max(0, pos - CHUNK)
            f.seek(start)
            data = f.read(pos - start)
            read += len(data)
            pos = start
            pending = data + pending
            if pos > 0:
                nl = pending.find(b"\n")
                if nl < 0:
                    continue  # still inside one long line
                complete, pending = pending[nl + 1:], pending[: nl + 1]
            else:
                complete, pending = pending, b""
            collected = _events_of(complete.split(b"\n"), show_thinking) + collected
        events = collected[-limit:]
        if (pos > 0 or len(collected) > limit) and not any(e["kind"] == "prompt" for e in events[:1]):
            # The run's first prompt is further back than the tail: show it first anyway.
            first = _first_prompt(f, show_thinking)
            if first is not None:
                events = [first] + events[1:] if len(events) >= limit else [first] + events
        return {"events": events, "cursor": end, "reset": False, "more": False}


def forward_events(path: Path, cursor: int, limit: int, show_thinking: bool = False) -> Dict[str, Any]:
    """Events written after byte offset `cursor`. A cursor that no longer fits the file (it shrank,
    or was replaced) restarts from the end with `reset` set."""
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        valid = 0 <= cursor <= size
        if valid and cursor > 0:
            f.seek(cursor - 1)
            valid = f.read(1) == b"\n"
        if not valid:
            got = tail_events(path, limit, show_thinking)
            got["reset"] = True
            return got
        f.seek(cursor)
        data = f.read(min(size - cursor, MAX_FORWARD_BYTES))
        last_nl = data.rfind(b"\n")
        if last_nl < 0:
            if len(data) == MAX_FORWARD_BYTES:
                # One line longer than the read cap: skip it rather than stall on it.
                skip = cursor + len(data)
                while True:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        return {"events": [], "cursor": cursor, "reset": False, "more": False}
                    idx = chunk.find(b"\n")
                    if idx >= 0:
                        return {"events": [], "cursor": skip + idx + 1, "reset": False, "more": True}
                    skip += len(chunk)
            return {"events": [], "cursor": cursor, "reset": False, "more": False}
        events: List[Dict[str, Any]] = []
        offset = cursor
        for raw in data[: last_nl + 1].split(b"\n")[:-1]:
            offset += len(raw) + 1
            events.extend(_events_of([raw], show_thinking))
            if len(events) >= limit:
                return {"events": events, "cursor": offset, "reset": False, "more": offset < size}
        more = len(data) == MAX_FORWARD_BYTES and offset < size
        return {"events": events, "cursor": offset, "reset": False, "more": more}


def _tail_bytes(path: Path, nbytes: int) -> List[bytes]:
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        start = max(0, size - nbytes)
        f.seek(start)
        data = f.read(size - start)
    lines = data.split(b"\n")
    return lines[1:] if start > 0 else lines  # the first piece may be the end of a longer line


def _summary(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """(one line about the latest action, model of the latest answer), cached per file version."""
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key in _summary_cache:
        _summary_cache.move_to_end(key)
        return _summary_cache[key]
    latest: Optional[str] = None
    model: Optional[str] = None
    for nbytes in SUMMARY_TAIL_BYTES:
        lines = _tail_bytes(path, nbytes)
        for raw in reversed(lines):
            obj = _parse(raw)
            if obj is None:
                continue
            if model is None and obj.get("type") == "assistant":
                m = (obj.get("message") or {}).get("model") if isinstance(obj.get("message"), dict) else None
                if isinstance(m, str) and m and not m.startswith("<"):
                    model = m
            if latest is None:
                for ev in reversed(line_events(obj)):
                    # The list never shows prompt text; plain tool output says little on one line.
                    if ev["kind"] in ("prompt", "result"):
                        continue
                    if ev["kind"] in ("tool", "error") and ev.get("detail"):
                        latest = _cut(f"{ev['title']}: {_first_line(ev['detail'], LINE_CHARS)}", LINE_CHARS)
                    else:
                        latest = _cut(ev["title"], LINE_CHARS)
                    break
            if latest is not None and model is not None:
                break
        if latest is not None and model is not None or st.st_size <= nbytes:
            break
    _summary_cache[key] = (latest, model)
    while len(_summary_cache) > 128:
        _summary_cache.popitem(last=False)
    return latest, model


# --- the adapter -------------------------------------------------------------------------------


class ClaudeAdapter:
    """Claude Code runs on this computer."""

    agent = AGENT
    label = LABEL

    def __init__(self, home: Optional[Path] = None):
        configured = os.environ.get("CLAUDE_CONFIG_DIR")
        self.default_dir = Path(configured).expanduser() if configured else (home or Path.home()) / ".claude"
        self._dirs: List[Path] = [self.default_dir]
        self._session_pids: set = set()

    # -- per scan --

    def begin_scan(self, extra_dirs: Iterable[Path] = ()) -> None:
        dirs: List[Path] = []
        for d in [self.default_dir, *extra_dirs]:
            d = Path(d)
            if d not in dirs:
                dirs.append(d)
        self._dirs = dirs
        pids = set()
        for d in dirs:
            try:
                for entry in (d / "sessions").glob("*.json"):  # never *.key
                    if entry.stem.isdigit():
                        pids.add(int(entry.stem))
            except OSError:
                continue
        self._session_pids = pids

    def config_dirs(self) -> List[Path]:
        return list(self._dirs)

    def claims_pid(self, pid: int) -> bool:
        return pid in self._session_pids

    # -- processes --

    @staticmethod
    def program_args(proc: Any) -> Optional[List[str]]:
        """The arguments after the Claude Code program, or None when the process is not Claude Code."""
        argv = [str(a) for a in (proc.cmdline or [])]
        exe = _slash(proc.exe).lower()
        if any(a.startswith("--type=") for a in argv[1:]):
            return None  # an Electron helper (the Claude desktop app), not the CLI
        if "/claude.app/" in exe or "/anthropicclaude/" in exe:
            return None  # the Claude desktop app itself
        first = _name(argv[0]) if argv else ""
        if first == "claude" or first.lower() == "claude.exe" or "/claude/versions/" in exe:
            return argv[1:]
        if _plain(first) in ("node", "bun"):
            for i, arg in enumerate(argv[1:], start=1):
                if "@anthropic-ai/claude-code" in _slash(arg):
                    return argv[i + 1:]
        return None

    def match_process(self, proc: Any) -> bool:
        args = self.program_args(proc)
        return args is not None and not (args and args[0] in SUBCOMMANDS)

    def _session_record(self, config_dir: Path, proc: Any) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads((config_dir / "sessions" / f"{int(proc.pid)}.json").read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("pid") != proc.pid:
            return None
        started = data.get("startedAt")
        if not isinstance(started, (int, float)) or abs(started / 1000 - proc.create_time) > START_TOLERANCE_S:
            return None  # the pid was reused: this file belongs to an earlier process
        return data

    def describe(self, proc: Any, env: Optional[Mapping[str, str]], source: Any,
                 parent_is_hermes: bool = False) -> Optional[Dict[str, Any]]:
        args = self.program_args(proc)
        env = env or {}
        config_dir = Path(env["CLAUDE_CONFIG_DIR"]).expanduser() if env.get("CLAUDE_CONFIG_DIR") else self.default_dir
        record = self._session_record(config_dir, proc)
        if args is None:
            if record is None:
                return None  # a stale pid file, now another program's pid
            args = [str(a) for a in (proc.cmdline or [])[1:]]
        elif args and args[0] in SUBCOMMANDS and record is None:
            return None
        tokens, values = parse_args(args)

        no_persist = "--no-session-persistence" in values or \
            (env.get("CLAUDE_CODE_SKIP_PROMPT_HISTORY") or "").strip().lower() in _TRUE
        embedded = no_persist and (values.get("--input-format") == "stream-json" or parent_is_hermes)
        print_mode = "-p" in values or "--print" in values or \
            str((record or {}).get("entrypoint") or "").startswith("sdk")

        cwd = (record or {}).get("cwd") if isinstance((record or {}).get("cwd"), str) else None
        if not cwd:
            try:
                cwd = source.cwd(proc.pid)
            except Exception:
                cwd = None

        session_id, how, transcript = None, None, None
        sid = (record or {}).get("sessionId")
        if isinstance(sid, str) and UUID_RE.match(sid):
            session_id, how = sid, "sessions_file"
        else:
            forked = "--fork-session" in values  # resuming with a fork writes a new session id
            for flag in ("--session-id",) + (() if forked else ("--resume", "-r")):
                value = values.get(flag)
                if value and UUID_RE.match(value):
                    session_id, how = value, "argv"
                    break
        if session_id and not no_persist:
            transcript = self.find_transcript(session_id, config_dir)
        if session_id is None and not no_persist and cwd:
            found = self.guess_transcript_by_cwd(config_dir, cwd, proc.create_time)
            if found is not None:
                session_id, how, transcript = found.stem, "cwd", found

        if no_persist:
            note = "This run does not save its session, so there is no live log."
        elif transcript is None:
            note = "No log found yet."
        else:
            note = None
        activity = [v for v in ((record or {}).get("updatedAt"), (record or {}).get("statusUpdatedAt"))
                    if isinstance(v, (int, float))]
        status = (record or {}).get("status")
        model = values.get("--model")
        return {
            "session_id": session_id, "session_source": how, "transcript": transcript,
            "config_dir": config_dir, "cwd": cwd, "embedded": embedded,
            "args_summary": " ".join(tokens),
            "model": model if model and _SAFE_VALUE_RE.match(model) else None,
            "interactive": not print_mode,
            "status_hint": status if status in ("busy", "idle") else None,
            "activity_ms": int(max(activity)) if activity else None,
            "log_note": note,
        }

    # -- transcripts --

    def find_transcript(self, session_id: str, config_dir: Optional[Path] = None) -> Optional[Path]:
        """`projects/*/<session id>.jsonl` in the config dir, whatever the folder is called."""
        if not session_id or not UUID_RE.match(session_id):
            return None
        root = Path(config_dir or self.default_dir) / "projects"
        try:
            hits = [p for p in root.glob(f"*/{session_id}.jsonl") if p.is_file()]
        except OSError:
            return None
        if not hits:
            return None
        return max(hits, key=lambda p: p.stat().st_mtime)

    def guess_transcript_by_cwd(self, config_dir: Path, cwd: str, create_time: float) -> Optional[Path]:
        """Newest transcript written since the process started whose lines carry this cwd."""
        needles = [f'"cwd":{json.dumps(cwd, ensure_ascii=False)}'.encode("utf-8"),
                   f'"cwd": {json.dumps(cwd, ensure_ascii=False)}'.encode("utf-8")]
        best: Optional[Tuple[float, Path]] = None
        try:
            folders = [d for d in (Path(config_dir) / "projects").iterdir() if d.is_dir()]
        except OSError:
            return None
        for folder in folders:
            try:
                files = list(folder.glob("*.jsonl"))
            except OSError:
                continue
            for path in files:
                if not UUID_RE.match(path.stem):
                    continue
                try:
                    mtime = path.stat().st_mtime
                    if mtime < create_time - 1 or (best and mtime <= best[0]):
                        continue
                    with open(path, "rb") as f:
                        head = f.read(CWD_HEAD_BYTES)
                except OSError:
                    continue
                if any(n in head for n in needles):
                    best = (mtime, path)
        return best[1] if best else None

    def read_events(self, path: Path, cursor: Optional[int], limit: int,
                    show_thinking: bool = False) -> Dict[str, Any]:
        if cursor is None:
            return tail_events(Path(path), limit, show_thinking)
        return forward_events(Path(path), int(cursor), limit, show_thinking)

    def summarize_latest(self, path: Path) -> Optional[str]:
        return _summary(Path(path))[0]

    def transcript_model(self, path: Path) -> Optional[str]:
        return _summary(Path(path))[1]
