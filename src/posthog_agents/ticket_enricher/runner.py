"""Run one ticket end to end (used by the CLI and the evals). Starts an in-process mock PostHog when
POSTHOG_HOST is empty, so `uv run python -m posthog_agents.ticket_enricher.cli T01` needs nothing else."""

from __future__ import annotations

import os
import socket
import threading
import time

import httpx
from langgraph.types import Command

from ..core.config import load_env


def ensure_mock(port: int = 8013) -> str:
    load_env()
    if os.getenv("POSTHOG_HOST"):
        return os.environ["POSTHOG_HOST"]
    host = f"http://127.0.0.1:{port}"
    with socket.socket() as s:
        busy = s.connect_ex(("127.0.0.1", port)) == 0
    if not busy:
        import uvicorn

        from ..mock.server import app
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(50):
            try:
                httpx.get(f"{host}/_log", timeout=1); break
            except Exception:
                time.sleep(0.1)
    os.environ["POSTHOG_HOST"] = host
    return host


def run_ticket(graph, ticket_input: dict, thread: str, decision: dict | None) -> dict:
    cfg = {"configurable": {"thread_id": thread}}
    out = graph.invoke(ticket_input, cfg)
    paused = bool(out.get("__interrupt__"))
    if paused:
        graph.invoke(Command(resume=decision or {"approved": False}), cfg)
    vals = dict(graph.get_state(cfg).values)
    vals["_paused_for_approval"] = paused
    return vals
