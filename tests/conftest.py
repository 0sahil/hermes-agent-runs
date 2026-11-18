"""Shared test helpers. Every process, path, id and prompt here is invented."""
import json
import sys
from pathlib import Path

import pytest

# The backend modules are loaded by path inside Hermes; import them directly here.
DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))

from procs import ProcInfo, StaticSource  # noqa: E402

SID_A = "11111111-2222-4333-8444-555555555555"
SID_B = "66666666-7777-4888-9999-aaaaaaaaaaaa"
SID_C = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
START = 1_700_000_000.0  # a fixed process start time (seconds)
CWD = "/work/demo-app"
SECRET_PROMPT = "please refactor the invented payment module"


def native_claude(pid=4242, ppid=100, args=(), start=START, name="2.1.0", exe="/opt/tools/claude/versions/2.1.0"):
    """A natively installed Claude Code process as macOS/Linux show it: named after its version."""
    return ProcInfo(pid=pid, ppid=ppid, name=name, exe=exe, cmdline=["claude", *args], create_time=start)


def proc(pid, name, cmdline=None, ppid=None, exe=None, start=START):
    return ProcInfo(pid=pid, ppid=ppid, name=name, exe=exe, cmdline=list(cmdline or [name]), create_time=start)


class Home:
    """A fake Claude config dir (`<tmp>/.claude`)."""

    def __init__(self, root: Path):
        self.root = root
        self.claude = root / ".claude"
        (self.claude / "sessions").mkdir(parents=True)
        (self.claude / "projects").mkdir(parents=True)

    def session_file(self, pid, session_id, cwd=CWD, started_ms=None, status="busy", entrypoint="cli",
                     config=None, **extra):
        base = Path(config) if config else self.claude
        (base / "sessions").mkdir(parents=True, exist_ok=True)
        data = {"pid": pid, "sessionId": session_id, "cwd": cwd,
                "startedAt": int((START * 1000) if started_ms is None else started_ms),
                "kind": "interactive", "entrypoint": entrypoint, "status": status,
                "updatedAt": int(START * 1000), "statusUpdatedAt": int(START * 1000), **extra}
        path = base / "sessions" / f"{pid}.json"
        path.write_text(json.dumps(data), "utf-8")
        return path

    def transcript(self, session_id, lines=(), folder="-work-demo-app", config=None):
        base = Path(config) if config else self.claude
        path = base / "projects" / folder / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for line in lines:
                f.write((line if isinstance(line, str) else json.dumps(line)) + "\n")
        return path


@pytest.fixture
def home(tmp_path):
    return Home(tmp_path)


@pytest.fixture
def adapter(home):
    from adapters.claude import ClaudeAdapter
    a = ClaudeAdapter(home=home.root)
    a.default_dir = home.claude  # ignore a CLAUDE_CONFIG_DIR the test runner itself may have
    return a


# --- transcript line builders ------------------------------------------------------------------

def ts(seconds=0):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(START + seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def user_prompt(text, t=0):
    return {"type": "user", "timestamp": ts(t), "cwd": CWD, "message": {"role": "user", "content": text}}


def assistant(blocks, t=0, model="claude-test-model"):
    return {"type": "assistant", "timestamp": ts(t), "cwd": CWD,
            "message": {"role": "assistant", "model": model, "content": blocks}}


def tool_use(name, inp, tid="toolu_1", t=0):
    return assistant([{"type": "tool_use", "id": tid, "name": name, "input": inp}], t)


def tool_result(content, tid="toolu_1", is_error=False, t=0):
    return {"type": "user", "timestamp": ts(t), "cwd": CWD,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": is_error}]}}


def static_source(procs_, environ=None, cwd=None):
    return StaticSource(procs_, environ=environ, cwd=cwd)
