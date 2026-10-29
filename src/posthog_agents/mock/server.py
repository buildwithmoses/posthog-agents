"""Mock PostHog: the subset of the PostHog API these agents use, with fictional data.

Each agent owns a project id (core.config.PROJECTS) and a module `posthog_agents.<agent>.mock_data` exposing:
    load_events() -> list[dict]            rows for the `events` table (event, distinct_id, timestamp, properties, ...)
    load_persons() -> list[dict]           optional
    load_recordings() -> list[dict]        optional: {"id", "distinct_id", "start_time", ..., "snapshots": [...]}
    load_experiments() -> list[dict]       optional
    handle(path, method, body) -> dict|None   optional override for anything else
HogQL runs on DuckDB after a small translation (hogql.py), so agents send real HogQL.
Writes (annotations etc.) are recorded and readable at GET /_log?project=<id>; POST /_reset clears them.
"""

from __future__ import annotations

import importlib
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..core.config import PROJECTS
from .hogql import Store

app = FastAPI(title="Mock PostHog")
SLUG = {v: k for k, v in PROJECTS.items()}
_stores: dict[int, Store] = {}
_writes: list[dict] = []


def store(pid: int) -> Store:
    if pid not in _stores:
        slug = SLUG.get(pid)
        if not slug:
            raise KeyError(pid)
        mod = importlib.import_module(f"posthog_agents.{slug.replace('-', '_')}.mock_data")
        _stores[pid] = Store.from_module(mod)
    return _stores[pid]


def err(code: int, detail: str) -> JSONResponse:
    return JSONResponse({"type": "invalid_request", "detail": detail}, status_code=code)


@app.get("/_log")
def log(project: int | None = None):
    return [w for w in _writes if project is None or w["project"] == project]


@app.post("/_reset")
def reset(project: int | None = None):
    for i in range(len(_writes) - 1, -1, -1):
        if project is None or _writes[i]["project"] == project:
            _writes.pop(i)
    for pid in list(_stores):
        if project is None or pid == project:
            _stores.pop(pid)
    return {"ok": True}


@app.api_route("/api/projects/{pid}/{rest:path}", methods=["GET", "POST", "PATCH"])
async def api(pid: int, rest: str, request: Request):
    if not request.headers.get("authorization", "").startswith("Bearer "):
        return err(401, "Authentication credentials were not provided.")
    try:
        s = store(pid)
    except KeyError:
        return err(404, f"Project {pid} not found.")
    body: Any = {}
    if request.method != "GET":
        try:
            body = await request.json()
        except Exception:
            body = {}
    path = "/" + rest.strip("/")
    params = dict(request.query_params)
    custom = s.handle(path, request.method, body, params)
    if custom is not None:
        if request.method != "GET":
            _writes.append({"project": pid, "method": request.method, "path": path, "body": body})
        return custom if not isinstance(custom, tuple) else JSONResponse(custom[1], status_code=custom[0])

    if path == "/query" and request.method == "POST":
        q = (body.get("query") or {})
        if q.get("kind") != "HogQLQuery":
            return err(400, "Only HogQLQuery is supported by the mock.")
        try:
            return s.query(q.get("query", ""), q.get("values") or {})
        except Exception as e:  # mirror PostHog's 400 for bad HogQL
            return err(400, f"HogQL error: {e}")
    if path == "/session_recordings" and request.method == "GET":
        return {"results": s.list_recordings(params), "has_next": False}
    if path.startswith("/session_recordings/"):
        parts = path.split("/")
        rec = s.recording(parts[2])
        if rec is None:
            return err(404, "Not found.")
        if len(parts) > 3 and parts[3] == "snapshots":
            return {"snapshots": rec.get("snapshots", [])}
        return {k: v for k, v in rec.items() if k != "snapshots"}
    if path == "/persons" and request.method == "GET":
        return {"results": s.persons(params.get("distinct_id"))}
    if path.startswith("/experiments/") and request.method == "GET":
        exp = s.experiment(int(path.split("/")[2]))
        return exp if exp else err(404, "Not found.")
    if request.method in ("POST", "PATCH"):
        _writes.append({"project": pid, "method": request.method, "path": path, "body": body})
        return {"id": len(_writes), **(body if isinstance(body, dict) else {})}
    return err(404, f"Mock has no route for {request.method} {path}")


def serve(port: int) -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
