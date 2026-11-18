"""Agent Runs: backend routes, mounted by Hermes at /api/plugins/agent-runs/.

    GET /runs?scope=hermes|all               -> {runs, scope, default_scope, scanned_at, warnings, platform, hidden_embedded}
    GET /runs/{id}/events?cursor=&limit=     -> {id, events, cursor, done, reset, has_log, note}
    GET /status                              -> plugin version, psutil, platform, config dirs, counts

Everything is read-only: the routes list processes and read agent transcripts. The logic lives in
runs_core.py, procs.py and adapters/; this file only wires them to HTTP.
"""
from __future__ import annotations

import importlib.util
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()
_HERE = Path(__file__).parent
_lock = threading.Lock()
_modules: Dict[str, Any] = {}
_state: Dict[str, Any] = {}


def _sibling(rel: str):
    """A sibling module, loaded by path (Hermes imports this file without a package, so relative
    imports fail). It is re-executed only when its file changes, which keeps the per-file caches
    warm between requests while still picking up an edited module."""
    path = _HERE / rel
    stamp = path.stat().st_mtime_ns
    with _lock:
        cached = _modules.get(rel)
        if cached and cached[0] == stamp:
            return cached[1]
        name = "hermes_plugin_agent_runs_" + rel[: -len(".py")].replace("/", "_")
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _modules[rel] = (stamp, module)
        return module


def _backend():
    core = _sibling("runs_core.py")
    procs = _sibling("procs.py")
    claude = _sibling("adapters/claude.py")
    with _lock:
        # One process source and one run memory for the life of the backend; rebuilt only when
        # the module that defines them was reloaded.
        if _state.get("procs") is not procs:
            _state.update(procs=procs, source=procs.PsutilSource())
        if _state.get("core") is not core:
            _state.update(core=core, store=core.RunStore(core.data_dir() / "runs.json"))
        if _state.get("claude") is not claude:
            _state.update(claude=claude, adapter=claude.ClaudeAdapter())
        source, store, adapter = _state["source"], _state["store"], _state["adapter"]
    return core, core.Backend(source, [adapter], store, core.plugin_settings())


def _fail(e: Exception):
    raise HTTPException(500, f"{type(e).__name__}: {e}")


@router.get("/runs")
def runs(scope: Optional[str] = Query(None)):
    try:
        _, backend = _backend()
        return backend.runs_payload(scope)
    except HTTPException:
        raise
    except Exception as e:
        _fail(e)


@router.get("/runs/{run_id}/events")
def run_events(run_id: str, cursor: Optional[str] = Query(None), limit: int = Query(300, ge=1, le=2000)):
    try:
        core, backend = _backend()
        try:
            return backend.events_payload(run_id, cursor, limit)
        except core.UnknownRun:
            raise HTTPException(404, "No such run. It may have finished longer ago than the plugin keeps runs.")
    except HTTPException:
        raise
    except Exception as e:
        _fail(e)


@router.get("/status")
def status():
    try:
        _, backend = _backend()
        return backend.status_payload()
    except HTTPException:
        raise
    except Exception as e:
        _fail(e)
