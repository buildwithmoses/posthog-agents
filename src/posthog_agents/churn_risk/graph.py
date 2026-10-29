"""Churn-risk agent (LangGraph).

gather (HogQL) -> score (code) -> [tracking incident? report it, alert nobody] -> evidence (recordings)
-> draft (model writes words from facts) -> check (code: every number, forbidden words) -> notify owners (Slack)
-> approve (interrupt: @jordan approves, edits or rejects each customer email) -> send + annotate (approved only)

Variants:
  baseline       as above
  no-guards      regression: same thresholds, but no tracking-incident, ramping, seasonal or re-alert checks
  model-decides  regression: the model reads raw weekly numbers and decides who is at risk
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..core.config import get_model, posthog_host
from ..core.posthog import PostHog
from . import queries
from .accounts import load_accounts
from .llm import structured
from .numbers import unsupported_numbers
from .scoring import score, weekly

SLUG = "churn-risk"
FORBIDDEN_IN_EMAIL = ("churn", "risk", "score", "replay", "recording", "tracking", "monitor", "analytics", "posthog", "alert")

DRAFT_SCHEMA = {
    "title": "ChurnDrafts", "type": "object", "additionalProperties": False,
    "properties": {
        "slack_alert": {"type": "string", "description": "Slack DM to the account owner, under 110 words."},
        "email_subject": {"type": "string"},
        "email_body": {"type": "string", "description": "Short, warm check-in email to the customer's admin, under 120 words."},
    },
    "required": ["slack_alert", "email_subject", "email_body"],
}

DRAFT_SYSTEM = """You write for Ledgerly's customer success team. You get FACTS about one customer account that code
has already judged at risk this week. Write two things:

1. slack_alert: a Slack DM to the internal account owner. Say what changed (use the signals), give the evidence
   links if there are any, and suggest one concrete next step. Under 110 words. Plain text, no headings.
2. A customer check-in email (email_subject, email_body) to the customer's admin, under 120 words. Friendly and
   helpful: offer a short call or help with their invoicing setup. It must NOT mention usage numbers, monitoring,
   analytics, recordings, risk or churn, and must not guilt the customer. No numbers at all in the email.

Rules: use only numbers that appear in FACTS, written the same way. Refer to the week exactly as WEEK says.
Never invent reasons, numbers, people or links. Sign the email "The Ledgerly team"."""

DECIDE_SCHEMA = {
    "title": "AtRisk", "type": "object", "additionalProperties": False,
    "properties": {"at_risk": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                   "properties": {"account_id": {"type": "string"}, "reason": {"type": "string"}},
                   "required": ["account_id", "reason"]}}},
    "required": ["at_risk"],
}


def _merge_outbox_ref(left, right):
    return (left or []) + (right or [])


class State(TypedDict, total=False):
    week_start: str
    run_date: str
    variant: str
    scored: dict
    incident: dict | None
    at_risk: list[str]
    evidence: dict
    drafts: dict
    outbox: Annotated[list, _merge_outbox_ref]
    annotations: list
    decisions: dict
    log: list


def _week_label(ws: date) -> str:
    we = ws + timedelta(days=6)
    return f"{ws.strftime('%b')} {ws.day}–{we.day}" if ws.month == we.month else f"{ws.strftime('%b')} {ws.day}–{we.strftime('%b')} {we.day}"


def build_graph(*, model=None, ph: PostHog | None = None, checkpointer=None, callbacks: list | None = None):
    ph = ph or PostHog(SLUG)
    host = os.getenv("POSTHOG_UI_HOST", posthog_host())
    _model = {"m": model}

    def m():
        if _model["m"] is None:
            _model["m"] = get_model()
        return _model["m"]

    cfg = {"callbacks": callbacks} if callbacks else None

    def gather_and_score(s: State) -> State:
        ws = date.fromisoformat(s["week_start"])
        rd = date.fromisoformat(s.get("run_date") or (ws + timedelta(days=7)).isoformat())
        rows = queries.fetch_usage(ph, ws)
        first_seen = queries.fetch_first_seen(ph, ws)
        notes = queries.fetch_annotations(ph)
        roster = load_accounts()
        variant = s.get("variant", "baseline")
        scored = score(rows, first_seen, roster, notes, ws, rd, guards=(variant == "baseline"))
        at_risk = sorted(a for a, f in scored["accounts"].items() if f["at_risk"])
        if variant == "model-decides":
            at_risk = _model_decides(rows, first_seen, roster, ws, scored)
            for a in at_risk:
                scored["accounts"][a]["at_risk"] = True
        return {"scored": scored, "incident": scored["incident"], "at_risk": at_risk,
                "log": [f"queried {len(rows)} usage rows, {len(first_seen)} accounts; at risk: {len(at_risk)}"]}

    def _model_decides(rows, first_seen, roster, ws, scored) -> list[str]:
        acc = weekly(rows, ws)
        table = []
        for aid, weeks in sorted(acc.items()):
            r = roster.get(aid, {})
            table.append({"account_id": aid, "seats": r.get("seats"), "first_seen": first_seen.get(aid),
                          "core_actions_by_week_oldest_first": [weeks.get(o, {"core": 0})["core"] for o in range(-4, 1)],
                          "active_users_by_week": [len(weeks.get(o, {"users": set()})["users"]) for o in range(-4, 1)],
                          "errors_by_week": [weeks.get(o, {"friction": 0})["friction"] for o in range(-4, 1)]})
        out = structured(m(), DECIDE_SCHEMA,
                         "You are a customer success analyst. Decide which accounts are at risk of churning this week "
                         "(the last number in each list is this week). Return their ids and a one-line reason.",
                         json.dumps(table), config=cfg)
        valid = set(acc)
        return sorted({x["account_id"] for x in out["at_risk"] if x["account_id"] in valid})

    def route(s: State) -> str:
        if s.get("incident"):
            return "incident"
        return "evidence" if s.get("at_risk") else END

    def report_incident(s: State) -> State:
        inc = s["incident"]
        msg = (f"Tracking incident, week of {_week_label(date.fromisoformat(s['week_start']))}: most accounts dropped at once, "
               f"so no churn alerts were sent. Check the PostHog SDK and ingestion before trusting this week's numbers.")
        return {"outbox": [{"channel": "slack", "to": "#cs-leads", "kind": "tracking_incident", "text": msg, "incident": inc}]}

    def evidence(s: State) -> State:
        ws = date.fromisoformat(s["week_start"])
        with ThreadPoolExecutor(4) as pool:
            ev = dict(zip(s["at_risk"], pool.map(lambda a: queries.fetch_evidence(ph, a, ws, host), s["at_risk"])))
        return {"evidence": ev}

    def draft(s: State) -> State:
        label = _week_label(date.fromisoformat(s["week_start"]))

        def one(aid: str) -> dict:
            f = {k: v for k, v in s["scored"]["accounts"][aid].items() if k not in ("at_risk", "suppressed")}
            ev = s["evidence"].get(aid, [])
            facts = {**f, "evidence_recordings": [{"url": e["url"], "errors": e["errors"], "rage_clicks": e["rage_clicks"]} for e in ev]}
            strip = (label, f["name"], f["account_id"], *[e["url"] for e in ev])
            user = f"WEEK: {label}\nFACTS:\n{json.dumps(facts, indent=1)}"
            problems: list[str] = []
            for attempt in range(2):
                d = structured(m(), DRAFT_SCHEMA, DRAFT_SYSTEM, user + (f"\n\nYour last draft had problems: {problems}. Fix them." if problems else ""), config=cfg)
                problems = check_draft(d, facts, strip)
                if not problems:
                    return {**d, "account_id": aid, "owner": f["owner"], "source": "model" if attempt == 0 else "model_retry", "facts": facts}
            return {**fallback(facts, label), "account_id": aid, "owner": f["owner"], "source": "fallback_template",
                    "rejected_problems": problems, "facts": facts}

        with ThreadPoolExecutor(4) as pool:
            drafts = {d["account_id"]: d for d in pool.map(one, s["at_risk"])}
        return {"drafts": drafts}

    def notify_owners(s: State) -> State:
        return {"outbox": [{"channel": "slack", "to": d["owner"], "kind": "owner_alert", "account_id": a, "text": d["slack_alert"]}
                           for a, d in sorted(s["drafts"].items())]}

    def approve(s: State) -> State:
        emails = [{"account_id": a, "account": d["facts"]["name"], "owner": d["owner"], "subject": d["email_subject"],
                   "body": d["email_body"]} for a, d in sorted(s["drafts"].items())]
        decisions = interrupt({"approver": "@jordan", "action": "approve, edit or reject each customer email", "emails": emails})
        return {"decisions": decisions or {}}

    def send(s: State) -> State:
        out, notes = [], []
        for a, d in sorted(s["drafts"].items()):
            dec = (s.get("decisions") or {}).get(a) or {}
            action = dec.get("action")
            if action not in ("approve", "edit"):
                continue
            if dec.get("by") != "@jordan":
                continue                        # only the named approver counts
            body = dec.get("body") if action == "edit" and dec.get("body") else d["email_body"]
            out.append({"channel": "email", "kind": "customer_checkin", "account_id": a, "subject": dec.get("subject") or d["email_subject"],
                        "body": body, "approved_by": dec["by"], "edited": action == "edit"})
            res = ph.annotate(f"churn-risk: alerted {a}", f"{s['week_start']}T00:00:00Z")
            notes.append({"account_id": a, "annotation_id": res.get("id")})
        return {"outbox": out, "annotations": notes}

    g = StateGraph(State)
    g.add_node("gather_and_score", gather_and_score)
    g.add_node("incident", report_incident)
    g.add_node("evidence", evidence)
    g.add_node("draft", draft)
    g.add_node("notify_owners", notify_owners)
    g.add_node("approve", approve)
    g.add_node("send", send)
    g.add_edge(START, "gather_and_score")
    g.add_conditional_edges("gather_and_score", route, {"incident": "incident", "evidence": "evidence", END: END})
    g.add_edge("incident", END)
    g.add_edge("evidence", "draft")
    g.add_edge("draft", "notify_owners")
    g.add_edge("notify_owners", "approve")
    g.add_edge("approve", "send")
    g.add_edge("send", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver())



def check_draft(d: dict, facts: dict, strip: tuple) -> list[str]:
    problems = []
    for field in ("slack_alert", "email_subject", "email_body"):
        bad = unsupported_numbers(d.get(field, ""), facts, strip)
        if bad:
            problems.append(f"{field} has numbers not in FACTS: {bad}")
    email = (d.get("email_subject", "") + " " + d.get("email_body", "")).lower()
    hits = [w for w in FORBIDDEN_IN_EMAIL if w in email]
    if hits:
        problems.append(f"email mentions {hits}")
    if any(ch.isdigit() for ch in d.get("email_body", "")):
        problems.append("email body contains numbers")
    if facts["name"] not in d.get("slack_alert", "") and facts["account_id"] not in d.get("slack_alert", ""):
        problems.append("slack_alert does not name the account")
    return problems


def fallback(facts: dict, label: str) -> dict:
    links = "\n".join(e["url"] for e in facts.get("evidence_recordings", []))
    alert = f"{facts['name']} looks at risk for {label}. " + " ".join(facts["signals"]) + (f"\nRecordings:\n{links}" if links else "") \
        + "\nSuggested next step: reach out to their admin this week."
    body = ("Hi there,\n\nWe wanted to check in and see how invoicing with Ledgerly is going for your team. If anything is "
            "getting in the way, we'd be glad to help on a short call or over email.\n\nThe Ledgerly team")
    return {"slack_alert": alert, "email_subject": "Checking in from Ledgerly", "email_body": body}
