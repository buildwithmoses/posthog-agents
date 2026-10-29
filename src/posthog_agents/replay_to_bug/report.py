"""The bug report the model writes, and the code that verifies it against the timeline.

verify() returns a list of problems. A report is accepted only with zero problems. Checks:
  - every steps_to_reproduce[].step_id exists, and every evidence[].evidence_id exists with the same timestamp
  - the primary failure is cited as evidence
  - every [mm:ss] timestamp, API path, HTTP status code and quoted string in any text field appears in the timeline
  - no PII (emails, card numbers, tokens, phone numbers) anywhere
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from . import timeline as T


class Step(BaseModel):
    step_id: str = Field(description="Id of a STEPS line, e.g. S3")
    text: str = Field(description="The step, in plain words, using only what that line says")


class Evidence(BaseModel):
    evidence_id: str = Field(description="Id of an EVIDENCE line, e.g. E2")
    t: str = Field(description="That line's timestamp, mm:ss, copied exactly")
    note: str = Field(description="Why it matters, in one short sentence")


class BugDraft(BaseModel):
    title: str = Field(description="Short, specific title: what breaks and where")
    summary: str = Field(description="Two sentences: what the user tried and what went wrong")
    steps_to_reproduce: list[Step]
    expected: str
    actual: str
    evidence: list[Evidence]
    suspected_cause: str = Field(description="One sentence, hedged ('likely'), based only on the evidence")
    severity: Literal["low", "medium", "high"]


TEXT_FIELDS = ("title", "summary", "expected", "actual", "suspected_cause")
TS = re.compile(r"(?<![\w:.])(\d{2}:\d{2})(?![\d:])")
STATUS = re.compile(r"(?<![\d.:/_\w])([45]\d\d)(?![\d.:]|\s?ms\b|\s?milliseconds)")
PATH = re.compile(r"(?<![\w.])(/(?:api|static)/[A-Za-z0-9_./:<>-]+)")
# Same quote character on both ends; single quotes only at word boundaries (so "(reading 'amount')" never pairs with a later ").
QUOTED = re.compile(r"\"([^\"]{8,})\"|“([^”]{8,})”|(?<!\w)'([^']{8,})'(?!\w)")


def all_text(d: dict) -> list[tuple[str, str]]:
    out = [(f, d.get(f) or "") for f in TEXT_FIELDS]
    out += [(f"step {s.get('step_id')}", s.get("text") or "") for s in d.get("steps_to_reproduce") or []]
    out += [(f"evidence {e.get('evidence_id')}", e.get("note") or "") for e in d.get("evidence") or []]
    return out


def verify(d: dict, tl: dict) -> list[str]:
    problems: list[str] = []
    steps = {s["id"]: s for s in tl["steps"]}
    ev = {e["id"]: e for e in tl["evidence"]}
    times = {s["t"] for s in tl["steps"]} | {e["t"] for e in tl["evidence"]}
    statuses = {str(e["status"]) for e in tl["evidence"] if "status" in e}
    corp = tl["corpus"]
    if not d.get("steps_to_reproduce"):
        problems.append("no steps_to_reproduce")
    for s in d.get("steps_to_reproduce") or []:
        if s.get("step_id") not in steps:
            problems.append(f"step {s.get('step_id')} does not exist")
    for e in d.get("evidence") or []:
        item = ev.get(e.get("evidence_id"))
        if item is None:
            problems.append(f"evidence {e.get('evidence_id')} does not exist")
        elif e.get("t") != item["t"]:
            problems.append(f"evidence {e.get('evidence_id')} has timestamp {e.get('t')}, the timeline says {item['t']}")
    if tl.get("primary") and tl["primary"]["id"] not in {e.get("evidence_id") for e in d.get("evidence") or []}:
        problems.append(f"the primary failure {tl['primary']['id']} is not cited as evidence")
    for where, text in all_text(d):
        for ts in TS.findall(text):
            if ts not in times:
                problems.append(f"{where}: timestamp {ts} is not in the timeline")
        for code in STATUS.findall(text):
            if code not in statuses:
                problems.append(f"{where}: status {code} is not in the timeline")
        for p in PATH.findall(text):
            p = p.rstrip(".,)")
            if p.lower() not in corp and T.norm_path(p).lower() not in corp.replace("<hash>", ""):
                if not _path_matches(p, corp):
                    problems.append(f"{where}: path {p} is not in the timeline")
        for q in (next(x for x in g if x) for g in QUOTED.findall(text)):
            if q.strip().lower().rstrip(".") not in corp:
                problems.append(f"{where}: quoted text \"{q}\" is not in the timeline")
        pii = T.find_pii(text)
        if pii:
            problems.append(f"{where}: contains personal data ({len(pii)} item(s))")
    return problems


def _path_matches(p: str, corp: str) -> bool:
    """Allow `/api/invoices/:id/reminders` when the timeline has `/api/invoices/inv_8812/reminders`."""
    pat = re.escape(p.lower()).replace(":id", "[^/\\s]+")
    return re.search(pat, corp) is not None


def fallback(tl: dict, category: str) -> dict:
    """A deterministic report built only from the timeline, used if the model can't produce a verified one."""
    p = tl["primary"]
    before = [s for s in tl["steps"] if s["ms"] <= p["ms"]][-6:] or tl["steps"][-6:]
    where = p.get("path") or "the page"
    what = {"frontend_exception": f"{p.get('error_type', 'Error')} in the app", "asset_load_error": "a page asset failed to load",
            "api_error": f"{p.get('method', '')} {where} returned {p.get('status')}", "timeout": f"{where} timed out",
            "client_request_error": f"{p.get('method', '')} {where} returned {p.get('status')}"}.get(category, "an error")
    return {
        "title": what[:1].upper() + what[1:],
        "summary": f"The session hit {what}. See the evidence below.",
        "steps_to_reproduce": [{"step_id": s["id"], "text": s["text"]} for s in before],
        "expected": "The action completes without an error.",
        "actual": p["detail"],
        "evidence": [{"evidence_id": p["id"], "t": p["t"], "note": "Primary failure."}],
        "suspected_cause": "Unknown; needs engineering investigation.",
        "severity": "medium",
        "fallback": True,
    }
