"""Pull what the requester actually did, with real HogQL, scoped to ONE person id.

Every query filters on the resolved person_id; there is no code path that queries by name, account or
anything taken from the ticket text. ScopedPostHog records every person id it was asked about, so evals
can prove nothing else was read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

WINDOW_HOURS = 48
MAX_EVENTS = 40
BORING = {"$pageview", "$pageleave", "$autocapture"}

EVENTS_Q = """
SELECT uuid, event, timestamp, properties
FROM events
WHERE person_id = {person_id} AND timestamp >= {start} AND timestamp <= {end}
ORDER BY timestamp ASC
LIMIT 1000
"""
LAST_SEEN_Q = "SELECT max(timestamp) AS last_seen, count(*) AS total FROM events WHERE person_id = {person_id}"


class ScopedPostHog:
    """Wraps core.posthog.PostHog and logs the person ids every query was scoped to."""

    def __init__(self, ph):
        self.ph = ph
        self.queried_person_ids: list[str] = []

    def rows(self, query: str, person_id: str, **values) -> list[dict]:
        self.queried_person_ids.append(person_id)
        return self.ph.rows(query, {"person_id": person_id, **values})

    def recordings_for(self, person_uuid: str) -> list[dict]:
        self.queried_person_ids.append(person_uuid)
        return [r for r in self.ph.recordings(person_uuid=person_uuid)
                if (r.get("person") or {}).get("uuid") == person_uuid]


def _ts(v) -> datetime:
    return datetime.fromisoformat(str(v).replace("T", " ").replace("Z", "")[:19])


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


@dataclass
class Evidence:
    person_id: str
    plan: str = ""
    role: str = ""
    account: str = ""
    window: tuple[str, str] = ("", "")
    events: list[dict] = field(default_factory=list)       # {"id": "E1", "uuid", "event", "at", "detail", "session"}
    recordings: list[dict] = field(default_factory=list)   # {"id": "R1", "recording_id", "start", "end", "errors", "link", "why"}
    exceptions: list[dict] = field(default_factory=list)
    flags: dict[str, str] = field(default_factory=dict)
    last_seen: str | None = None
    total_events: int = 0

    def by_id(self) -> dict[str, dict]:
        return {**{e["id"]: e for e in self.events}, **{r["id"]: r for r in self.recordings}}


def _detail(event: str, p: dict) -> str:
    if event == "$exception":
        return p.get("$exception_message") or p.get("$exception_type") or "exception"
    if event == "$feature_flag_called":
        return f"flag {p.get('$feature_flag')} = {p.get('$feature_flag_response')}"
    keep = {k: v for k, v in p.items() if not k.startswith("$") and k not in ("email", "name")}
    url = p.get("$current_url", "")
    path = url.split(".example", 1)[-1] if url else ""
    bits = [f"{k}={v}" for k, v in list(keep.items())[:3]]
    return " ".join([path, *bits]).strip()


def gather(sph: ScopedPostHog, person: dict, created_at: str, host: str, project: int) -> Evidence:
    pid = person["uuid"]
    props = person.get("properties") or {}
    end = _ts(created_at)
    start = end - timedelta(hours=WINDOW_HOURS)
    ev = Evidence(pid, plan=props.get("plan", ""), role=props.get("role", ""), account=props.get("company", ""),
                  window=(_fmt(start), _fmt(end)))
    seen = sph.rows(LAST_SEEN_Q, pid)
    if seen and seen[0]["last_seen"]:
        ev.last_seen, ev.total_events = _fmt(_ts(seen[0]["last_seen"])), int(seen[0]["total"])
    rows = sph.rows(EVENTS_Q, pid, start=start.strftime("%Y-%m-%d %H:%M:%S"), end=end.strftime("%Y-%m-%d %H:%M:%S"))
    parsed = []
    for r in rows:
        p = r["properties"] if isinstance(r["properties"], dict) else json.loads(r["properties"] or "{}")
        for k, v in p.items():
            if k.startswith("$feature/"):
                ev.flags[k.split("/", 1)[1]] = str(v)
        parsed.append({"uuid": r["uuid"], "event": r["event"], "at_dt": _ts(r["timestamp"]), "props": p})
    # Keep every exception and every non-boring event; fill the rest with the most recent pageviews.
    key = [x for x in parsed if x["event"] not in BORING]
    rest = [x for x in parsed if x["event"] in BORING]
    chosen = key[-MAX_EVENTS:]
    chosen += rest[-max(0, MAX_EVENTS - len(chosen)):] if len(chosen) < MAX_EVENTS else []
    chosen.sort(key=lambda x: x["at_dt"])
    for i, x in enumerate(chosen, 1):
        item = {"id": f"E{i}", "uuid": x["uuid"], "event": x["event"], "at": _fmt(x["at_dt"]),
                "detail": _detail(x["event"], x["props"]), "session": x["props"].get("$session_id")}
        ev.events.append(item)
        if x["event"] == "$exception":
            ev.exceptions.append(item)
    ev.recordings = rank_recordings(sph.recordings_for(pid), parsed, start, end, host, project)
    return ev


def rank_recordings(recs: list[dict], events: list[dict], start: datetime, end: datetime, host: str, project: int, top: int = 3) -> list[dict]:
    """Score each recording in the window: an exception inside it beats console errors beats recency."""
    exc_times = [e["at_dt"] for e in events if e["event"] == "$exception"]
    scored = []
    for r in recs:
        rs, re_ = _ts(r["start_time"]), _ts(r["end_time"])
        if re_ < start or rs > end:
            continue
        inside = sum(1 for t in exc_times if rs <= t <= re_)
        recency = 1 - (end - re_).total_seconds() / (end - start).total_seconds()
        score = inside * 100 + min(int(r.get("console_error_count") or 0), 10) * 5 + recency
        why = (f"contains {inside} exception(s)" if inside else
               f"{r.get('console_error_count')} console error(s)" if r.get("console_error_count") else "most recent session in the window")
        scored.append((score, r, why))
    scored.sort(key=lambda t: -t[0])
    out = []
    for i, (score, r, why) in enumerate(scored[:top], 1):
        out.append({"id": f"R{i}", "recording_id": r["id"], "start": _fmt(_ts(r["start_time"])), "end": _fmt(_ts(r["end_time"])),
                    "errors": int(r.get("console_error_count") or 0), "why": why, "score": round(score, 3),
                    "link": f"{host}/project/{project}/replay/{r['id']}"})
    return out
