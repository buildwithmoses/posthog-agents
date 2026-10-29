"""Thin PostHog API client: the same calls work against posthog.com and the mock.

Real endpoints used (https://posthog.com/docs/api):
  POST /api/projects/{id}/query/                 {"query": {"kind": "HogQLQuery", "query": "..."}}
  GET  /api/projects/{id}/session_recordings/    and /{recording_id}/snapshots
  GET  /api/projects/{id}/persons/?distinct_id=  and /api/projects/{id}/experiments/{id}/
  POST /api/projects/{id}/annotations/           (writes go through the agent's approval step first)
Reads retry on 429/5xx; writes never retry.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .config import load_env, posthog_host, posthog_key, project_id


class PostHog:
    def __init__(self, slug: str, host: str | None = None, key: str | None = None, timeout: float = 30.0):
        load_env()
        self.project = project_id(slug)
        self.http = httpx.Client(base_url=host or posthog_host(), timeout=timeout,
                                 headers={"Authorization": f"Bearer {key or posthog_key()}"})

    def _get(self, path: str, **params) -> Any:
        for attempt in range(4):
            r = self.http.get(f"/api/projects/{self.project}{path}", params=params or None)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(1.5 * (attempt + 1)); continue
            r.raise_for_status(); return r.json()

    def _post(self, path: str, body: dict, *, retry: bool = False) -> Any:
        for attempt in range(4 if retry else 1):
            r = self.http.post(f"/api/projects/{self.project}{path}", json=body)
            if retry and r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(1.5 * (attempt + 1)); continue
            r.raise_for_status(); return r.json()

    def hogql(self, query: str, values: dict | None = None) -> dict:
        """Run a HogQL query. Returns {"columns": [...], "results": [[...], ...]}. Queries are read-only, so retried."""
        q: dict = {"kind": "HogQLQuery", "query": query}
        if values:
            q["values"] = values
        return self._post("/query/", {"query": q}, retry=True)

    def rows(self, query: str, values: dict | None = None) -> list[dict]:
        r = self.hogql(query, values)
        return [dict(zip(r["columns"], row)) for row in r["results"]]

    def recordings(self, **params) -> list[dict]:
        return self._get("/session_recordings/", **params).get("results", [])

    def recording(self, recording_id: str) -> dict:
        return self._get(f"/session_recordings/{recording_id}/")

    def snapshots(self, recording_id: str) -> dict:
        return self._get(f"/session_recordings/{recording_id}/snapshots/")

    def person(self, distinct_id: str) -> dict | None:
        res = self._get("/persons/", distinct_id=distinct_id).get("results", [])
        return res[0] if res else None

    def experiment(self, experiment_id: int) -> dict:
        return self._get(f"/experiments/{experiment_id}/")

    def annotate(self, content: str, date_marker: str) -> dict:
        return self._post("/annotations/", {"content": content, "date_marker": date_marker, "scope": "project"})
