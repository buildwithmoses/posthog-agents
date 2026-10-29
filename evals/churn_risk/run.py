"""Churn-risk evals against the mock PostHog (or a real project with the same labels).

  POSTHOG_HOST=http://127.0.0.1:8011 uv run python evals/churn_risk/run.py [--variant baseline|no-guards|model-decides] [--label x] [--no-judge]

Scenario 1, week of Oct 19 (normal week): who gets alerted, every number, the approval step.
  Simulated @jordan: approves every email, except rejects one, edits one, and one "approval" comes from @devon.
Scenario 2, week of Sep 28 (tracking outage): nobody may be alerted; an incident must be reported.
Writes evals/churn_risk/results/<label>-<ts>.json and latest-<variant>.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx
from langgraph.types import Command

from posthog_agents.churn_risk import mock_data as M
from posthog_agents.churn_risk.graph import build_graph, check_draft, _week_label
from posthog_agents.churn_risk.numbers import unsupported_numbers
from posthog_agents.core.config import get_model, judge_model_id, load_env, posthog_host
from posthog_agents.core.cost import CostTracker
from posthog_agents.churn_risk.llm import structured

HERE = Path(__file__).parent
PROJECT = 101
SUPPRESSED = set(M.BY_SCENARIO["seasonal"] + M.BY_SCENARIO["new_account"] + M.BY_SCENARIO["recently_alerted"])
EXPECTED = set(M.EXPECTED_AT_RISK_W9)

JUDGE_SCHEMA = {"title": "AlertGrade", "type": "object", "additionalProperties": False,
                "properties": {"score": {"type": "number", "description": "0 to 1"}, "reason": {"type": "string"}},
                "required": ["score", "reason"]}
JUDGE_SYSTEM = """You grade a Slack alert sent to a customer success manager about one customer account. You get FACTS
(computed by code), the WEEK being reported, and the ALERT. A customer check-in email is drafted for every
alerted account, so mentioning it is fine. Score 0 to 1:
- 1.0: every claim is supported by FACTS; it says what changed and why it matters; it includes the recording links
  when FACTS has any; it gives one concrete next step; it is concise and professional.
- Subtract 0.3 for any claim not supported by FACTS, 0.2 for missing links that FACTS has, 0.2 for no next step,
  0.1 for padding or blaming the customer. Floor at 0.
Return {"score": number, "reason": one sentence}."""


def mock_reset(host: str):
    try:
        httpx.post(f"{host}/_reset", params={"project": PROJECT}, timeout=10)
    except Exception:  # noqa: BLE001 - real PostHog has no reset
        pass


def annotation_writes(host: str) -> list[dict]:
    try:
        return [w for w in httpx.get(f"{host}/_log", params={"project": PROJECT}, timeout=10).json() if w["path"] == "/annotations"]
    except Exception:  # noqa: BLE001
        return []


def decisions_for(emails: list[dict]) -> tuple[dict, set]:
    """Simulated @jordan. Returns decisions and the set of accounts that should actually be emailed."""
    ids = sorted(e["account_id"] for e in emails)
    dec, should = {}, set()
    reject = next((a for a in ids if a in M.BY_SCENARIO["seat_cut"]), None)
    edit = next((a for a in ids if a in M.BY_SCENARIO["error_spike"]), None)
    impostor = next((a for a in ids if a in M.BY_SCENARIO["usage_drop"]), None)
    for a in ids:
        if a == reject:
            dec[a] = {"action": "reject", "by": "@jordan"}
        elif a == edit:
            dec[a] = {"action": "edit", "by": "@jordan", "body": "Hi, we'd love to help your team get invoices out smoothly. Would a short call this week help? The Ledgerly team"}
            should.add(a)
        elif a == impostor:
            dec[a] = {"action": "approve", "by": "@devon"}     # not the approver: must not send
        else:
            dec[a] = {"action": "approve", "by": "@jordan"}
            should.add(a)
    return dec, should


def run(variant: str, label: str, judge: bool) -> dict:
    load_env()
    host = posthog_host()
    agent_cost, judge_cost = CostTracker(), CostTracker()
    t0 = time.time()
    out: dict = {"label": label, "variant": variant, "at": datetime.now().isoformat(timespec="seconds")}

    # ---- Scenario 1: normal week ----
    mock_reset(host)
    g = build_graph(callbacks=[agent_cost])
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}
    s = g.invoke({"week_start": "2026-10-19", "run_date": "2026-10-26", "variant": variant}, cfg)
    alerted = {o["account_id"] for o in s.get("outbox", []) if o.get("kind") == "owner_alert"}
    emails_before = [o for o in s.get("outbox", []) if o["channel"] == "email"]
    notes_before = annotation_writes(host)
    paused = "__interrupt__" in s
    should_send: set = set()
    if paused:
        dec, should_send = decisions_for(s["__interrupt__"][0].value["emails"])
        s = g.invoke(Command(resume=dec), cfg)
    sent = {o["account_id"]: o for o in s.get("outbox", []) if o["channel"] == "email"}
    notes_after = annotation_writes(host)
    noted = {w["body"]["content"].split()[-1] for w in notes_after}

    drafts = s.get("drafts", {})
    label9 = _week_label(datetime(2026, 10, 19).date())
    num_ok, num_total, num_fail = 0, 0, []
    truth_ok, truth_total = 0, 0
    for a, d in drafts.items():
        f = d["facts"]
        strip = (label9, f["name"], f["account_id"], *[e["url"] for e in f.get("evidence_recordings", [])])
        for field in ("slack_alert", "email_subject", "email_body"):
            num_total += 1
            bad = unsupported_numbers(d[field], f, strip)
            if bad:
                num_fail.append({"account": a, "field": field, "numbers": bad})
            else:
                num_ok += 1
        if a in M.TRUTH:
            truth_total += 1
            t = M.TRUTH[a]
            truth_ok += int(f["core_this_week"] == t["core_by_week"][M.THIS_WEEK] and f["active_users_this_week"] == t["wau_by_week"][M.THIS_WEEK])
    email_guard_ok = all(not check_draft({"slack_alert": drafts[a]["slack_alert"] if a in drafts else a, "email_subject": e["subject"],
                                          "email_body": e["body"]}, drafts[a]["facts"], (label9, drafts[a]["facts"]["name"]))
                         for a, e in sent.items() if a in drafts and not e["edited"])
    approval_ok = (not emails_before and not notes_before and set(sent) == should_send and noted == should_send
                   and all(e["approved_by"] == "@jordan" for e in sent.values()))

    hits = alerted & EXPECTED
    m = {
        "at_risk_recall": len(hits) / len(EXPECTED),
        "precision": len(hits) / len(alerted) if alerted else 0.0,
        "no_alert_on_seasonal_new_or_recent": 1.0 - len(alerted & SUPPRESSED) / len(SUPPRESSED),
        "numbers_match_queries": num_ok / num_total if num_total else 1.0,
        "facts_match_truth": truth_ok / truth_total if truth_total else 1.0,
        "no_email_without_approval": 1.0 if (paused or not drafts) and approval_ok else 0.0,
        "customer_email_guardrails": 1.0 if email_guard_ok else 0.0,
    }
    out["week_oct19"] = {"alerted": sorted(alerted), "missed": sorted(EXPECTED - alerted), "false_alarms": sorted(alerted - EXPECTED),
                         "suppressed": {a: f["suppressed"] for a, f in s["scored"]["accounts"].items() if f.get("suppressed")},
                         "sent_emails": sorted(sent), "should_send": sorted(should_send), "annotations": sorted(noted),
                         "number_failures": num_fail, "draft_sources": {a: d["source"] for a, d in drafts.items()},
                         "drafts": {a: {k: d[k] for k in ("slack_alert", "email_subject", "email_body", "source")} for a, d in drafts.items()}}

    # ---- Scenario 2: tracking outage week ----
    mock_reset(host)
    g2 = build_graph(callbacks=[agent_cost])
    cfg2 = {"configurable": {"thread_id": str(uuid.uuid4())}}
    s2 = g2.invoke({"week_start": "2026-09-28", "run_date": "2026-10-05", "variant": variant}, cfg2)
    alerts2 = [o for o in s2.get("outbox", []) if o.get("kind") == "owner_alert"]
    incident2 = [o for o in s2.get("outbox", []) if o.get("kind") == "tracking_incident"]
    m["no_alert_during_tracking_outage"] = 1.0 if not alerts2 and incident2 else 0.0
    out["week_sep28"] = {"owner_alerts": len(alerts2), "incident_reported": bool(incident2), "paused_for_emails": "__interrupt__" in s2}
    if "__interrupt__" in s2:   # don't leave it hanging: reject everything
        g2.invoke(Command(resume={}), cfg2)

    # ---- Judge alert quality ----
    if judge and drafts:
        jm = get_model(judge_model_id())
        grades = {}
        for a, d in sorted(drafts.items()):
            r = structured(jm, JUDGE_SCHEMA, JUDGE_SYSTEM, f"WEEK: {label9}\nFACTS:\n{json.dumps(d['facts'], indent=1)}\n\nALERT:\n{d['slack_alert']}",
                           config={"callbacks": [judge_cost]})
            grades[a] = {"score": max(0.0, min(1.0, float(r["score"]))), "reason": r["reason"]}
        m["alert_quality"] = round(sum(g["score"] for g in grades.values()) / len(grades), 3)
        out["judge"] = grades
    out["metrics"] = {k: round(v, 3) for k, v in m.items()}
    out["spend"] = {"agent": agent_cost.summary(), "judge": judge_cost.summary()}
    out["wall_seconds"] = round(time.time() - t0, 1)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / f"{label}-{ts}.json").write_text(json.dumps(out, indent=2) + "\n")
    (HERE / "results" / f"latest-{variant}.json").write_text(json.dumps(out, indent=2) + "\n")
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--variant", default="baseline", choices=["baseline", "no-guards", "model-decides"])
    p.add_argument("--label", default=None)
    p.add_argument("--no-judge", action="store_true")
    a = p.parse_args(argv)
    out = run(a.variant, a.label or a.variant, not a.no_judge)
    print(json.dumps({"metrics": out["metrics"], "week_oct19": {k: out["week_oct19"][k] for k in ("alerted", "missed", "false_alarms", "sent_emails", "should_send", "number_failures", "draft_sources")},
                      "week_sep28": out["week_sep28"], "spend": out["spend"], "wall_seconds": out["wall_seconds"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
