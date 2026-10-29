"""Risk scoring in code. Each account is compared with its OWN trailing 4-week baseline.

Signals (any one makes an account at risk):
  usage_drop      core actions (invoice_created + invoice_sent + payment_received) <= 60% of baseline
  champion_left   a user who did >= 40% of the account's baseline core actions did none this week
  seat_cut        weekly active users / seats < 50% AND active users <= 70% of baseline
  error_spike     $exception + $rageclick >= max(10, 3x baseline) AND core actions <= 80% of baseline
Suppressions (never alert, but report why):
  tracking_incident   this week, >= 60% of established accounts are below 60% of their own baseline at once.
                      That's our instrumentation, not 40 customers churning together. Report it; alert nobody.
                      Incident weeks are also excluded from every baseline.
  ramping             account first seen < 28 days before the week starts
  seasonal            a PostHog annotation "seasonal:<account> ... until YYYY-MM-DD" covers the week
  recently_alerted    an annotation "churn-risk: alerted <account>" within the 14 days before this run
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta

CORE = {"invoice_created", "invoice_sent", "payment_received"}
FEATURES = {"reminder_sent", "integration_synced", "report_viewed", "ai_draft_requested"}
FRICTION = {"$exception", "$rageclick"}

THRESHOLDS = {"usage_drop": 0.60, "champion_share": 0.40, "seat_ratio": 0.50, "seat_drop": 0.70,
              "error_min": 10, "error_mult": 3.0, "error_core": 0.80, "incident_share": 0.60, "incident_ratio": 0.60,
              "ramp_days": 28, "realert_days": 14, "baseline_weeks": 4}


def _d(x) -> date:
    return x if isinstance(x, date) and not isinstance(x, datetime) else datetime.fromisoformat(str(x)[:19].replace("T", " ")).date()


def weekly(rows: list[dict], week_start: date) -> dict[str, dict[int, dict]]:
    """rows: account, distinct_id, day, event, n  ->  {account: {week_offset: {...}}}; offset 0 = scored week, -1 = week before."""
    acc: dict = defaultdict(lambda: defaultdict(lambda: {"core": 0, "friction": 0, "users": set(), "features": set(),
                                                         "core_by_user": defaultdict(int)}))
    for r in rows:
        if not r.get("account"):
            continue
        off = (_d(r["day"]) - week_start).days // 7
        w = acc[r["account"]][off]
        n = int(r["n"])
        ev = r["event"]
        w["users"].add(r["distinct_id"])
        if ev in CORE:
            w["core"] += n; w["core_by_user"][r["distinct_id"]] += n
        elif ev in FEATURES:
            w["features"].add(ev)
        elif ev in FRICTION:
            w["friction"] += n
    return acc


def incident_weeks(acc: dict, offsets: list[int]) -> set[int]:
    """A week where most established accounts fall far below their own previous 4 weeks at the same time."""
    out = set()
    for off in offsets:
        below, total = 0, 0
        for weeks in acc.values():
            prior = [weeks[o]["core"] for o in range(off - 4, off) if o in weeks and o not in out]
            if len(prior) < 3 or not sum(prior):
                continue
            total += 1
            if weeks.get(off, {"core": 0})["core"] < THRESHOLDS["incident_ratio"] * (sum(prior) / len(prior)):
                below += 1
        if total >= 5 and below / total >= THRESHOLDS["incident_share"]:
            out.add(off)
    return out


def _annotation_flags(annotations: list[dict], account: str, week_start: date, run_date: date) -> dict:
    flags = {"seasonal": None, "recently_alerted": None}
    week_end = week_start + timedelta(days=6)
    for a in annotations:
        text = a.get("content", "")
        marker = _d(a.get("date_marker", "1970-01-01"))
        if text.startswith(f"seasonal:{account}"):
            m = re.search(r"until (\d{4}-\d{2}-\d{2})", text)
            until = _d(m.group(1)) if m else marker + timedelta(days=14)
            if marker <= week_end and until >= week_start:
                flags["seasonal"] = text
        if text.strip() == f"churn-risk: alerted {account}" and 0 <= (run_date - marker).days <= THRESHOLDS["realert_days"]:
            flags["recently_alerted"] = marker.isoformat()
    return flags


def score(rows: list[dict], first_seen: dict[str, str], roster: dict[str, dict], annotations: list[dict],
          week_start: date, run_date: date, *, guards: bool = True) -> dict:
    """Returns {"incident": {...}|None, "accounts": {id: {...facts, at_risk, signals, suppressed}}}.
    guards=False is the regression variant: same thresholds, no incident/ramp/seasonal/re-alert suppression."""
    acc = weekly(rows, week_start)
    offsets = list(range(-7, 1))
    bad_weeks = incident_weeks(acc, offsets) if guards else set()
    incident = None
    if 0 in bad_weeks:
        below = sum(1 for w in acc.values() if 0 in w)
        incident = {"week_start": week_start.isoformat(), "accounts_checked": len(acc),
                    "reason": "Most accounts fell below 60% of their own baseline in the same week, which points to a tracking problem, not churn."}
    out = {}
    for aid, weeks in sorted(acc.items()):
        base_offs = [o for o in range(-1, -8, -1) if o not in bad_weeks and o in weeks][: THRESHOLDS["baseline_weeks"]]
        meta = roster.get(aid, {"name": aid, "plan": "unknown", "seats": None, "owner": None})
        this = weeks.get(0, {"core": 0, "friction": 0, "users": set(), "features": set(), "core_by_user": {}})
        n = len(base_offs) or 1
        core_base = sum(weeks[o]["core"] for o in base_offs) / n
        wau_base = sum(len(weeks[o]["users"]) for o in base_offs) / n
        fr_base = sum(weeks[o]["friction"] for o in base_offs) / n
        br_base = sum(len(weeks[o]["features"]) for o in base_offs) / n
        user_base = defaultdict(int)
        for o in base_offs:
            for u, c in weeks[o]["core_by_user"].items():
                user_base[u] += c
        facts = {"account_id": aid, "name": meta["name"], "plan": meta.get("plan"), "seats": meta.get("seats"),
                 "owner": meta.get("owner"), "baseline_weeks": len(base_offs),
                 "core_this_week": this["core"], "core_baseline": round(core_base, 1),
                 "core_change_pct": round(100 * (this["core"] - core_base) / core_base) if core_base else None,
                 "active_users_this_week": len(this["users"]), "active_users_baseline": round(wau_base, 1),
                 "errors_this_week": this["friction"], "errors_baseline": round(fr_base, 1),
                 "features_this_week": len(this["features"]), "features_baseline": round(br_base, 1)}
        signals = []
        if core_base and this["core"] <= THRESHOLDS["usage_drop"] * core_base:
            signals.append(f"Core invoicing actions fell to {this['core']} from a 4-week average of {facts['core_baseline']} ({facts['core_change_pct']}%).")
        total_base = sum(user_base.values())
        for u, c in sorted(user_base.items(), key=lambda x: -x[1])[:1]:
            share = c / total_base if total_base else 0
            if share >= THRESHOLDS["champion_share"] and this["core_by_user"].get(u, 0) == 0 and u not in this["users"]:
                facts["champion"] = {"user": u, "baseline_share_pct": round(100 * share), "baseline_core_per_week": round(c / n, 1)}
                signals.append(f"Their most active user ({u}, {facts['champion']['baseline_share_pct']}% of usage) did nothing this week.")
        seats = meta.get("seats")
        if seats and wau_base and len(this["users"]) / seats < THRESHOLDS["seat_ratio"] and len(this["users"]) <= THRESHOLDS["seat_drop"] * wau_base:
            signals.append(f"Only {len(this['users'])} of {seats} seats were active, down from {facts['active_users_baseline']}.")
        if this["friction"] >= max(THRESHOLDS["error_min"], THRESHOLDS["error_mult"] * fr_base) and core_base and this["core"] <= THRESHOLDS["error_core"] * core_base:
            signals.append(f"Errors and rage clicks jumped to {this['friction']} from {facts['errors_baseline']} while core actions fell {abs(facts['core_change_pct'])}%.")
        suppressed = None
        if guards:
            if incident:
                suppressed = "tracking_incident"
            else:
                fs = first_seen.get(aid)
                if fs and (week_start - _d(fs)).days < THRESHOLDS["ramp_days"]:
                    suppressed = "ramping"
                else:
                    flags = _annotation_flags(annotations, aid, week_start, run_date)
                    if flags["seasonal"]:
                        suppressed = "seasonal"
                    elif flags["recently_alerted"]:
                        suppressed = "recently_alerted"
        out[aid] = {**facts, "signals": signals, "at_risk": bool(signals) and suppressed is None,
                    "suppressed": suppressed if signals else None}
    return {"incident": incident, "incident_weeks": sorted(bad_weeks), "accounts": out}
