"""Ground truth straight from the fixture events (no HogQL), to prove the queries return the right numbers."""

from __future__ import annotations

from . import mock_data as m
from .queries import windows


def _q(vals, p):
    v = sorted(vals)
    if not v:
        return 0.0
    k = (len(v) - 1) * p
    f = int(k)
    c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


def window_truth(start: str, end: str) -> dict:
    groups: dict = {}
    req = acc = 0
    for e in m.load_events():
        if not (start <= e["timestamp"] < end):
            continue
        if e["event"] == "ai_draft_requested":
            req += 1
        elif e["event"] == "ai_draft_accepted":
            acc += 1
        elif e["event"] == "$ai_generation":
            p = e["properties"]
            g = groups.setdefault((p["$ai_model"], p["$ai_provider"]), {"calls": 0, "cost": 0.0, "errors": 0, "lat": [], "tin": []})
            g["calls"] += 1
            g["cost"] += p["$ai_total_cost_usd"]
            g["errors"] += int(p["$ai_is_error"])
            g["lat"].append(p["$ai_latency"])
            g["tin"].append(p["$ai_input_tokens"])
    out = {k: {"calls": g["calls"], "cost": g["cost"], "errors": g["errors"], "p95": _q(g["lat"], 0.95),
               "in_tokens": sum(g["tin"]) / len(g["tin"])} for k, g in groups.items()}
    return {"groups": out, "requested": req, "accepted": acc}


def verify(analysis: dict, as_of, hours: int) -> list[str]:
    """Compare the facts the agent computed from HogQL with ground truth for the window under test."""
    cur, _ = windows(as_of, hours)
    t = window_truth(*cur)
    errs = []
    close = lambda a, b, tol=1e-6: abs(a - b) <= tol * max(1.0, abs(b))  # noqa: E731
    seen = set()
    for g in analysis["facts"]["groups"]:
        k = (g["model"], g["provider"])
        seen.add(k)
        tg = t["groups"].get(k)
        if not tg:
            errs.append(f"{k}: not in truth"); continue
        for name, fact, truth in (("calls", g["calls"]["value"], tg["calls"]), ("cost", g["cost"]["value"], tg["cost"]),
                                  ("errors", g["errors"]["value"], tg["errors"]), ("p95", g["p95"]["value"], tg["p95"]),
                                  ("in_tokens", g["in_tokens_per_call"]["value"], tg["in_tokens"])):
            if not close(float(fact), float(truth), 1e-4):
                errs.append(f"{k} {name}: query {fact} != truth {truth}")
    missing = set(t["groups"]) - seen
    if missing:
        errs.append(f"groups missing from queries: {sorted(missing)}")
    d = analysis["facts"]["drafts"]
    if d["requested"]["value"] != t["requested"] or d["accepted"]["value"] != t["accepted"]:
        errs.append(f"drafts: query {d['requested']['value']}/{d['accepted']['value']} != truth {t['requested']}/{t['accepted']}")
    return errs
