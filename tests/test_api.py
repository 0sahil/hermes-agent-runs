"""The HTTP routes, through FastAPI's TestClient, with a fake process list.

Skipped when FastAPI (or its test client's httpx) is not installed; the same behaviour is covered
at the core level in test_core.py.
"""
import importlib.util
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from conftest import DASHBOARD, SECRET_PROMPT, SID_A, native_claude, tool_use, user_prompt  # noqa: E402


@pytest.fixture
def api(home, tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("agent_runs_plugin_api_under_test", DASHBOARD / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    core = module._sibling("runs_core.py")
    procs = module._sibling("procs.py")
    claude = module._sibling("adapters/claude.py")

    adapter = claude.ClaudeAdapter(home=home.root)
    adapter.default_dir = home.claude
    source = procs.StaticSource([native_claude(args=["-p", SECRET_PROMPT])],
                                environ={4242: {"HERMES_SESSION_ID": "local-chat-9", "HERMES_CRON_SESSION": ""}})
    store = core.RunStore(tmp_path / "data" / "runs.json")
    monkeypatch.setattr(module, "_backend", lambda: (core, core.Backend(source, [adapter], store, {})))

    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/agent-runs")
    client = TestClient(app)
    client.plugin_api = module  # the loaded plugin_api module, so tests can patch it directly
    return client


def test_runs_route(api, home):
    home.session_file(4242, SID_A, entrypoint="sdk-cli")
    home.transcript(SID_A, [user_prompt(SECRET_PROMPT), tool_use("Bash", {"command": "pytest -q"})])
    for scope in ("hermes", "all", None, "bogus"):
        r = api.get("/api/plugins/agent-runs/runs", params={"scope": scope} if scope else None)
        assert r.status_code == 200
        body = r.json()
        assert [run["id"] for run in body["runs"]] == [f"claude:{SID_A}"]
        assert body["scope"] == ("all" if scope == "all" else "hermes")
        assert SECRET_PROMPT not in r.text


def test_events_route_with_cursor(api, home):
    home.session_file(4242, SID_A)
    path = home.transcript(SID_A, [user_prompt("go"), tool_use("Read", {"file_path": "/work/demo-app/x"})])
    first = api.get(f"/api/plugins/agent-runs/runs/claude:{SID_A}/events")
    assert first.status_code == 200
    body = first.json()
    assert [e["kind"] for e in body["events"]] == ["prompt", "tool"] and body["done"] is False
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(tool_use("Bash", {"command": "ls"})) + "\n")
    nxt = api.get(f"/api/plugins/agent-runs/runs/claude%3A{SID_A}/events",
                  params={"cursor": body["cursor"], "limit": 50}).json()
    assert [e["detail"] for e in nxt["events"]] == ["ls"]


def test_events_route_rejects_unknown_ids_and_bad_limits(api, home):
    home.session_file(4242, SID_A)
    for bad in ("claude:00000000-0000-4000-8000-000000000000", "nope", "..%2F..%2Fetc%2Fpasswd"):
        r = api.get(f"/api/plugins/agent-runs/runs/{bad}/events")
        assert r.status_code == 404, bad
    assert api.get(f"/api/plugins/agent-runs/runs/claude:{SID_A}/events", params={"limit": 0}).status_code == 422
    assert api.get(f"/api/plugins/agent-runs/runs/claude:{SID_A}/events", params={"limit": 5000}).status_code == 422


def test_status_route(api):
    body = api.get("/api/plugins/agent-runs/status").json()
    assert body["plugin"] == "agent-runs" and body["version"] == "0.1.0"
    assert body["counts"]["total"] == 1


def test_route_errors_are_500_with_the_reason(api, monkeypatch):
    def broken():
        raise RuntimeError("invented")

    # the routes look `_backend` up in plugin_api's module globals on every request
    monkeypatch.setattr(api.plugin_api, "_backend", broken)
    r = api.get("/api/plugins/agent-runs/status")
    assert r.status_code == 500 and "RuntimeError: invented" in r.json()["detail"]
