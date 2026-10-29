"""Issue tracker client. Locally it's the mock's /api/projects/102/bugs stand-in; in production this class is
replaced by a Linear (or GitHub Issues) client with the same three methods. Writes never retry."""

from __future__ import annotations

import httpx

from ..core.config import load_env, posthog_host, posthog_key, project_id


class Tracker:
    def __init__(self, host: str | None = None):
        load_env()
        self.base = f"{host or posthog_host()}/api/projects/{project_id('replay-to-bug')}/bugs"
        self.http = httpx.Client(timeout=20, headers={"Authorization": f"Bearer {posthog_key()}"})

    def bugs(self) -> list[dict]:
        r = self.http.get(self.base); r.raise_for_status(); return r.json()["results"]

    def create(self, bug: dict) -> dict:
        r = self.http.post(self.base, json=bug); r.raise_for_status(); return r.json()

    def comment(self, bug_id: str, body: str) -> dict:
        r = self.http.post(f"{self.base}/{bug_id}/comments", json={"body": body}); r.raise_for_status(); return r.json()
