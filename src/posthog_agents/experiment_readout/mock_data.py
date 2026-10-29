"""Fictional Ledgerly experiments for the mock PostHog (project 105).

Every count is exact and chosen by hand, so the statistics can be checked against the labels in
evals/experiment_readout/labels.json. Users are exposed through `$feature_flag_called` events
(properties $feature_flag, $feature_flag_response), and convert by firing the metric event after exposure.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

NOW = "2026-10-27 12:00:00"
_NOW = datetime.fromisoformat(NOW)

PRIMARY_INVOICE = ("Invoice sent", "invoice_sent")
PRIMARY_UPGRADE = ("Upgrade completed", "checkout_completed")
REFUNDS = ("Refunds requested", "refund_requested")
TICKETS = ("Support tickets opened", "support_ticket_opened")


def seg(n, conv, d0=0, d1=None):
    return {"n": n, "conv": conv, "d0": d0, "d1": d1}


# id, flag key, name, description, variants [(key, pct)], start (days ago), end (days ago or None),
# primary, guardrails, minimum detectable effect (% relative), segments per variant, users who saw 2 variants
SPECS = [
    dict(id=1, key="invoice-template-gallery", name="Invoice template gallery",
         description="Show a gallery of invoice templates on the first invoice screen.",
         variants=[("control", 50), ("test", 50)], start=30, end=2, primary=PRIMARY_INVOICE, guardrails=[REFUNDS], mde=10,
         segments={"control": [seg(3000, {"invoice_sent": 300, "refund_requested": 30})],
                   "test": [seg(3000, {"invoice_sent": 390, "refund_requested": 31})]}),
    dict(id=2, key="reminder-tone-friendly", name="Friendlier payment reminder tone",
         description="Rewrite the default payment reminder email in a friendlier tone.",
         variants=[("control", 50), ("test", 50)], start=32, end=4, primary=PRIMARY_INVOICE, guardrails=[TICKETS], mde=10,
         segments={"control": [seg(2500, {"invoice_sent": 250, "support_ticket_opened": 40})],
                   "test": [seg(2500, {"invoice_sent": 255, "support_ticket_opened": 41})]}),
    dict(id=3, key="pricing-page-annual-toggle", name="Annual billing toggle on pricing page",
         description="Default the pricing page toggle to annual billing.",
         variants=[("control", 50), ("test", 50)], start=30, end=2, primary=PRIMARY_UPGRADE, guardrails=[REFUNDS], mde=10,
         segments={"control": [seg(3000, {"checkout_completed": 300, "refund_requested": 20})],
                   "test": [seg(2400, {"checkout_completed": 312, "refund_requested": 16})]}),
    dict(id=4, key="ach-autopay-prompt", name="ACH autopay prompt after first payment",
         description="Ask customers to turn on ACH autopay right after their first card payment.",
         variants=[("control", 50), ("test", 50)], start=3, end=None, primary=PRIMARY_INVOICE, guardrails=[TICKETS], mde=10,
         segments={"control": [seg(300, {"invoice_sent": 30, "support_ticket_opened": 5})],
                   "test": [seg(310, {"invoice_sent": 41, "support_ticket_opened": 5})]}),
    dict(id=5, key="one-click-upgrade", name="One-click upgrade from the invoice list",
         description="Let Starter users upgrade to Growth with one click from the invoice list.",
         variants=[("control", 50), ("test", 50)], start=30, end=2, primary=PRIMARY_UPGRADE, guardrails=[REFUNDS, TICKETS], mde=10,
         segments={"control": [seg(3000, {"checkout_completed": 300, "refund_requested": 30, "support_ticket_opened": 60})],
                   "test": [seg(3000, {"checkout_completed": 390, "refund_requested": 75, "support_ticket_opened": 62})]}),
    dict(id=6, key="onboarding-checklist-style", name="Onboarding checklist style",
         description="Three onboarding checklist designs: the current list, a progress bar (variant_a) and a guided tour (variant_b).",
         variants=[("control", 34), ("variant_a", 33), ("variant_b", 33)], start=28, end=1, primary=PRIMARY_INVOICE, guardrails=[TICKETS], mde=10,
         segments={"control": [seg(2000, {"invoice_sent": 200, "support_ticket_opened": 30})],
                   "variant_a": [seg(1940, {"invoice_sent": 209, "support_ticket_opened": 29})],
                   "variant_b": [seg(1940, {"invoice_sent": 262, "support_ticket_opened": 28})]}),
    dict(id=7, key="draft-with-ai-button-color", name="Draft with AI button color",
         description="Purple instead of gray for the Draft with AI button. Rina's note: the day-2 dashboard showed test "
                     "at double the control rate, so this looks like a clear win and I'd like to ship it.",
         variants=[("control", 50), ("test", 50)], start=30, end=2, primary=PRIMARY_INVOICE, guardrails=[TICKETS], mde=10,
         # The first two days looked like a big win (12 vs 24 conversions in 150 users each); the full run doesn't.
         segments={"control": [seg(150, {"invoice_sent": 12, "support_ticket_opened": 3}, 0, 2),
                               seg(2850, {"invoice_sent": 288, "support_ticket_opened": 57}, 2, None)],
                   "test": [seg(150, {"invoice_sent": 24, "support_ticket_opened": 3}, 0, 2),
                            seg(2850, {"invoice_sent": 291, "support_ticket_opened": 58}, 2, None)]}),
    dict(id=8, key="reports-home-redesign", name="Reports home redesign",
         description="New layout for the Reports home page. A caching bug served both layouts to some users for a week.",
         variants=[("control", 50), ("test", 50)], start=30, end=2, primary=PRIMARY_INVOICE, guardrails=[TICKETS], mde=10,
         segments={"control": [seg(2500, {"invoice_sent": 250, "support_ticket_opened": 50})],
                   "test": [seg(2500, {"invoice_sent": 325, "support_ticket_opened": 49})]},
         multi=400),
    dict(id=9, key="invoice-due-date-default", name="Default due date of 15 days instead of 30",
         description="Change the default invoice due date from 30 days to 15 days.",
         variants=[("control", 50), ("test", 50)], start=35, end=3, primary=PRIMARY_INVOICE, guardrails=[TICKETS], mde=10,
         segments={"control": [seg(30000, {"invoice_sent": 3000, "support_ticket_opened": 600})],
                   "test": [seg(30000, {"invoice_sent": 3150, "support_ticket_opened": 610})]}),
]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _window(spec) -> tuple[datetime, datetime]:
    start = (_NOW - timedelta(days=spec["start"])).replace(hour=0, minute=0, second=0)
    end = (_NOW - timedelta(days=spec["end"])).replace(hour=0, minute=0, second=0) if spec["end"] is not None else _NOW
    return start, end


def _events_for(spec) -> list[dict]:
    start, end = _window(spec)
    days = max(1, (end - start).days)
    out: list[dict] = []
    for variant, segments in spec["segments"].items():
        idx = 0
        for s in segments:
            d0, d1 = s["d0"], s["d1"] if s["d1"] is not None else days
            span = max(1, d1 - d0)
            users = [f"x{spec['id']}-{variant}-{idx + i}" for i in range(s["n"])]
            idx += s["n"]
            rng = random.Random(f"{spec['id']}:{variant}:{d0}")
            for i, uid in enumerate(users):
                ts = start + timedelta(days=d0 + i % span, hours=8 + (i * 7) % 9, minutes=(i * 13) % 60)
                if ts >= end - timedelta(hours=3):
                    ts = end - timedelta(hours=3, minutes=(i % 50))
                company = f"acct-{(sum(map(ord, uid)) * 31 % 1800):04d}"
                out.append({"event": "$feature_flag_called", "distinct_id": uid, "timestamp": _iso(ts),
                            "properties": {"$feature_flag": spec["key"], "$feature_flag_response": variant,
                                           "$groups": {"company": company}}})
                users[i] = (uid, ts)
            for event, k in s["conv"].items():
                for uid, ts in rng.sample(users, k):
                    out.append({"event": event, "distinct_id": uid, "timestamp": _iso(ts + timedelta(hours=2)),
                                "properties": {"$groups": {"company": "acct"}}})
    for i in range(spec.get("multi", 0)):
        uid = f"x{spec['id']}-both-{i}"
        ts = start + timedelta(days=i % days, hours=10)
        for j, variant in enumerate(("control", "test")):
            out.append({"event": "$feature_flag_called", "distinct_id": uid, "timestamp": _iso(ts + timedelta(hours=j)),
                        "properties": {"$feature_flag": spec["key"], "$feature_flag_response": variant}})
    # A conversion that happened BEFORE exposure must not count (one per variant, from a user who later got exposed).
    first = next(e for e in out if e["event"] == "$feature_flag_called")
    out.append({"event": spec["primary"][1], "distinct_id": first["distinct_id"],
                "timestamp": _iso(datetime.fromisoformat(first["timestamp"]) - timedelta(days=1)), "properties": {}})
    return out


def load_events() -> list[dict]:
    events: list[dict] = []
    for spec in SPECS:
        events.extend(_events_for(spec))
    return events


def _metric(name, event, goal):
    return {"kind": "ExperimentMetric", "metric_type": "funnel", "name": name, "goal": goal,
            "series": [{"kind": "EventsNode", "event": event}]}


def load_experiments() -> list[dict]:
    out = []
    for spec in SPECS:
        start, end = _window(spec)
        variants = [{"key": k, "rollout_percentage": p} for k, p in spec["variants"]]
        out.append({
            "id": spec["id"], "name": spec["name"], "description": spec["description"],
            "feature_flag_key": spec["key"],
            "feature_flag": {"id": 500 + spec["id"], "key": spec["key"], "filters": {"multivariate": {"variants": variants}}},
            "start_date": start.isoformat() + "Z", "end_date": end.isoformat() + "Z" if spec["end"] is not None else None,
            "parameters": {"feature_flag_variants": variants, "minimum_detectable_effect": spec["mde"]},
            "metrics": [_metric(*spec["primary"], "increase")],
            "metrics_secondary": [_metric(n, e, "decrease") for n, e in spec["guardrails"]],
            "conclusion": None,
        })
    return out
