"""Code decides: anomaly detection, minimum-volume rules, intended-change recognition, cause, remediation.

Input: raw query rows from queries.collect(). Output: facts (numbers, each with a display string), incidents,
expected changes and an optional flag proposal. The model never sees anything but this output.
"""

from __future__ import annotations

import re
from statistics import median

RULES = {
    "min_calls": 30,            # per model/provider group, in the window, for any rate or percentile alert
    "min_accepted": 20,         # drafts accepted in the window, for cost-per-accepted alerts
    "cost_ratio": 1.5,          # window cost (or cost per call) vs baseline median
    "min_cost_delta_per_day": 0.25,  # USD; scaled by window length
    "p95_ratio": 1.5, "p95_min_s": 4.0,
    "err_rate_min": 0.05, "err_ratio": 3.0, "min_errors": 5,
    "cpa_ratio": 1.4, "accept_drop": 0.25,   # cost per accepted draft; relative drop in acceptance rate
    "bloat_ratio": 1.5,          # span input tokens per call vs baseline
    "volume_ratio": 1.5,
    "new_model_share": 0.5,      # a model with >= this share now and ~none in baseline = model switch
}
NO_MIN = {**RULES, "min_calls": 0, "min_accepted": 0, "min_errors": 0, "min_cost_delta_per_day": 0.0}


def _sum(rows, key):
    return sum(float(r.get(key) or 0) for r in rows)


def _totals(gen_rows: list[dict]) -> dict:
    calls = _sum(gen_rows, "calls")
    return {"calls": calls, "cost": _sum(gen_rows, "cost"), "errors": _sum(gen_rows, "errors"),
            "cost_per_call": _sum(gen_rows, "cost") / calls if calls else 0.0}


def _key(r):
    return (r["model"], r["provider"])


def _ratio(a, b):
    return a / b if b else (float("inf") if a else 1.0)


def fmt_usd(x):
    return f"${x:,.2f}" if x >= 1 else f"${x:,.4f}"


def fmt_pct(x):
    return f"{x * 100:.1f}%"


def fmt_x(x):
    return "new" if x == float("inf") else f"{x:.1f}x"


def fmt_s(x):
    return f"{x:.1f}s"


def fmt_int(x):
    return f"{int(round(x)):,}"


def mentions_model(text: str, model: str) -> bool:
    t = text.lower()
    short = re.sub(r"^claude-|^gpt-", "", model.lower())
    return model.lower() in t or short in t


def analyze(raw: dict, rules: dict | None = None) -> dict:
    R = rules or RULES
    hours = raw["window"]["hours"]
    scale = hours / 24
    cur, base = raw["current"], raw["baseline"]
    tot = _totals(cur["gen"])
    btots = [_totals(b["gen"]) for b in base]
    b = {k: median([t[k] for t in btots]) for k in ("calls", "cost", "errors", "cost_per_call")}

    facts: dict = {"window": raw["window"], "totals": {}, "groups": [], "baseline_days": len(base)}
    def F(value, display):
        return {"value": value, "display": display}

    facts["totals"] = {
        "calls": F(tot["calls"], fmt_int(tot["calls"])), "baseline_calls": F(b["calls"], fmt_int(b["calls"])),
        "cost": F(tot["cost"], fmt_usd(tot["cost"])), "baseline_cost": F(b["cost"], fmt_usd(b["cost"])),
        "cost_ratio": F(_ratio(tot["cost"], b["cost"]), fmt_x(_ratio(tot["cost"], b["cost"]))),
        "cost_per_call": F(tot["cost_per_call"], fmt_usd(tot["cost_per_call"])),
        "baseline_cost_per_call": F(b["cost_per_call"], fmt_usd(b["cost_per_call"])),
    }
    # Drafts and cost per accepted draft
    d = cur["drafts"][0] if cur["drafts"] else {"requested": 0, "accepted": 0}
    bd = [x["drafts"][0] for x in base if x["drafts"]]
    acc_rate = d["accepted"] / d["requested"] if d["requested"] else 0.0
    b_acc_rate = median([x["accepted"] / x["requested"] for x in bd if x["requested"]] or [0.0])
    cpa = tot["cost"] / d["accepted"] if d["accepted"] else 0.0
    b_cpa = median([t["cost"] / x["accepted"] for t, x in zip(btots, bd) if x["accepted"]] or [0.0])
    facts["drafts"] = {
        "requested": F(d["requested"], fmt_int(d["requested"])), "accepted": F(d["accepted"], fmt_int(d["accepted"])),
        "acceptance_rate": F(acc_rate, fmt_pct(acc_rate)), "baseline_acceptance_rate": F(b_acc_rate, fmt_pct(b_acc_rate)),
        "cost_per_accepted": F(cpa, fmt_usd(cpa)), "baseline_cost_per_accepted": F(b_cpa, fmt_usd(b_cpa)),
        "cost_per_accepted_ratio": F(_ratio(cpa, b_cpa), fmt_x(_ratio(cpa, b_cpa))),
    }

    # Per model/provider group vs baseline medians
    bgroups: dict = {}
    for day in base:
        for r in day["gen"]:
            bgroups.setdefault(_key(r), []).append(r)
    groups = []
    for r in cur["gen"]:
        hist = bgroups.get(_key(r), [])
        bp95 = median([h["p95"] for h in hist]) if hist else 0.0
        berr = median([h["errors"] / h["calls"] for h in hist if h["calls"]]) if hist else 0.0
        bcalls = median([h["calls"] for h in hist] + [0] * (len(base) - len(hist))) if base else 0.0
        err_rate = r["errors"] / r["calls"] if r["calls"] else 0.0
        groups.append({
            "model": r["model"], "provider": r["provider"], "has_baseline": bool(hist),
            "calls": F(r["calls"], fmt_int(r["calls"])), "baseline_calls": F(bcalls, fmt_int(bcalls)),
            "cost": F(r["cost"], fmt_usd(r["cost"])),
            "p50": F(r["p50"], fmt_s(r["p50"])), "p95": F(r["p95"], fmt_s(r["p95"])), "baseline_p95": F(bp95, fmt_s(bp95)),
            "p95_ratio": F(_ratio(r["p95"], bp95), fmt_x(_ratio(r["p95"], bp95))),
            "error_rate": F(err_rate, fmt_pct(err_rate)), "baseline_error_rate": F(berr, fmt_pct(berr)),
            "errors": F(r["errors"], fmt_int(r["errors"])),
            "in_tokens_per_call": F(r["in_tokens"] or 0, fmt_int(r["in_tokens"] or 0)),
            "out_tokens_per_call": F(r["out_tokens"] or 0, fmt_int(r["out_tokens"] or 0)),
        })
    facts["groups"] = groups

    # Model mix and intended changes
    model_calls: dict = {}
    for r in cur["gen"]:
        model_calls[r["model"]] = model_calls.get(r["model"], 0) + r["calls"]
    bmodel: dict = {}
    for day in base:
        for r in day["gen"]:
            bmodel[r["model"]] = bmodel.get(r["model"], 0) + r["calls"]
    btotal = sum(bmodel.values()) or 1
    new_models = [m for m, c in model_calls.items() if tot["calls"] and c / tot["calls"] >= R["new_model_share"] and bmodel.get(m, 0) / btotal < 0.05]
    win_start, win_end = raw["window"]["start"], raw["window"]["end"]
    def in_window(a):
        dm = a.get("date_marker", "").replace("T", " ").replace("Z", "")[:19]
        return win_start <= dm < win_end
    notes = [a for a in raw.get("annotations", []) if in_window(a)]
    expected, switches = [], []
    for m in new_models:
        ann = next((a for a in notes if mentions_model(a.get("content", ""), m)), None)
        (expected if ann else switches).append({"model": m, "annotation": ann})
    facts["model_mix"] = {m: F(c / tot["calls"], fmt_pct(c / tot["calls"])) for m, c in sorted(model_calls.items())} if tot["calls"] else {}
    facts["annotations_in_window"] = [{"content": a["content"], "by": (a.get("created_by") or {}).get("first_name"), "at": a["date_marker"]} for a in notes]

    incidents: list[dict] = []
    # Cost spike (total cost and cost per call), with cause decomposition
    cost_delta_min = R["min_cost_delta_per_day"] * scale
    cost_r = _ratio(tot["cost"], b["cost"])
    cpc_r = _ratio(tot["cost_per_call"], b["cost_per_call"])
    if tot["calls"] >= R["min_calls"] and (cost_r >= R["cost_ratio"] or cpc_r >= R["cost_ratio"]) and tot["cost"] - b["cost"] >= cost_delta_min:
        cause, evidence = None, {}
        if switches or expected:
            m = (switches or expected)[0]["model"]
            cause = f"model_switch:{m}"
            evidence = {"model": m, "share": facts["model_mix"][m]}
        else:
            bspan: dict = {}
            for day in base:
                for s in day["spans"]:
                    bspan.setdefault(s["span"], []).append(s["in_tokens"])
            bloats = []
            for s in cur["spans"]:
                bt = median(bspan.get(s["span"], [0]))
                if bt and s["in_tokens"] / bt >= R["bloat_ratio"]:
                    bloats.append((s["in_tokens"] / bt, s, bt))
            if bloats:
                ratio, s, bt = max(bloats, key=lambda x: x[0])
                cause = f"prompt_bloat:{s['span']}"
                evidence = {"span": s["span"], "in_tokens_per_call": F(s["in_tokens"], fmt_int(s["in_tokens"])),
                            "baseline_in_tokens_per_call": F(bt, fmt_int(bt)), "ratio": F(ratio, fmt_x(ratio))}
            elif _ratio(tot["calls"], b["calls"]) >= R["volume_ratio"]:
                cause = "volume"
            else:
                cause = "unknown"
        inc = {"kind": "cost_spike", "cause": cause, "evidence": evidence,
               "top_traces": [{"trace": t["trace"], "cost": F(t["cost"], fmt_usd(t["cost"])), "max_in_tokens": F(t["max_in_tokens"] or 0, fmt_int(t["max_in_tokens"] or 0))} for t in cur["traces"][:3]]}
        if cause.startswith("model_switch:") and expected:
            pass  # explained by an annotated, intended change: reported as expected, not as an incident
        else:
            incidents.append(inc)

    expected_models = {e["model"] for e in expected}
    for g in groups:
        if g["model"] in expected_models:
            continue  # latency or cost differences on an intentionally switched model are not incidents
        calls = g["calls"]["value"]
        if calls < R["min_calls"] or not g["has_baseline"]:
            continue  # too little volume, or a model with no history (handled as a model switch above)
        if g["p95_ratio"]["value"] >= R["p95_ratio"] and g["p95"]["value"] >= R["p95_min_s"]:
            incidents.append({"kind": "latency_spike", "cause": f"provider_latency:{g['provider']}",
                              "evidence": {"model": g["model"], "provider": g["provider"], "p95": g["p95"], "baseline_p95": g["baseline_p95"], "p95_ratio": g["p95_ratio"], "calls": g["calls"]}})
    # Errors: per group rate rule, cause = dominant error message
    err_groups = [g for g in groups if g["has_baseline"] and g["calls"]["value"] >= R["min_calls"] and g["errors"]["value"] >= R["min_errors"]
                  and g["error_rate"]["value"] >= R["err_rate_min"] and g["error_rate"]["value"] >= R["err_ratio"] * max(g["baseline_error_rate"]["value"], 0.001)]
    if err_groups:
        g = max(err_groups, key=lambda x: x["error_rate"]["value"])
        clusters = [e for e in cur["errors"] if e["provider"] == g["provider"] and e["model"] == g["model"]]
        top = clusters[0] if clusters else {"error": "unknown", "n": 0}
        share = top["n"] / g["errors"]["value"] if g["errors"]["value"] else 0
        incidents.append({"kind": "error_spike", "cause": f"error_cluster:{top['error']}",
                          "evidence": {"model": g["model"], "provider": g["provider"], "error_rate": g["error_rate"], "baseline_error_rate": g["baseline_error_rate"],
                                       "errors": g["errors"], "calls": g["calls"], "top_error": top["error"], "top_error_count": F(top["n"], fmt_int(top["n"])),
                                       "top_error_share": F(share, fmt_pct(share))}})
    # Cost per accepted draft: only its own incident when acceptance fell (otherwise a cost spike explains it)
    cpa_r = facts["drafts"]["cost_per_accepted_ratio"]["value"]
    acc_drop = 1 - (acc_rate / b_acc_rate) if b_acc_rate else 0.0
    if d["accepted"] >= R["min_accepted"] and cpa_r >= R["cpa_ratio"] and acc_drop >= R["accept_drop"] and not any(i["kind"] == "cost_spike" for i in incidents):
        incidents.append({"kind": "cost_per_accepted", "cause": "acceptance_drop",
                          "evidence": {"acceptance_rate": facts["drafts"]["acceptance_rate"], "baseline_acceptance_rate": facts["drafts"]["baseline_acceptance_rate"],
                                       "cost_per_accepted": facts["drafts"]["cost_per_accepted"], "baseline_cost_per_accepted": facts["drafts"]["baseline_cost_per_accepted"],
                                       "cost_per_call": facts["totals"]["cost_per_call"], "baseline_cost_per_call": facts["totals"]["baseline_cost_per_call"]}})

    expected_changes = [{"kind": "model_switch", "model": e["model"], "annotation": e["annotation"]["content"],
                         "by": (e["annotation"].get("created_by") or {}).get("first_name"),
                         "cost_ratio": facts["totals"]["cost_ratio"]} for e in expected]
    return {"facts": facts, "incidents": incidents, "expected_changes": expected_changes, "proposal": propose(incidents, raw.get("flags", []))}


def propose(incidents: list[dict], flags: list[dict]) -> dict | None:
    """A feature-flag change that would contain the incident. Proposed only; applied after @theo approves."""
    by_key = {f["key"]: f for f in flags}
    for inc in incidents:
        kind, cause = inc["kind"], inc["cause"]
        if kind in ("error_spike", "latency_spike"):
            bad = inc["evidence"]["provider"]
            f = by_key.get("draft-ai-provider-routing")
            if not f:
                continue
            payload = dict(f["filters"]["payloads"]["true"])
            other = "amazon-bedrock" if bad == "anthropic" else "anthropic"
            payload.update({"primary": other, "secondary": None, "bedrock_share": 1.0 if other == "amazon-bedrock" else 0.0})
            return {"flag_id": f["id"], "flag_key": f["key"], "reason": f"Route all Draft with AI traffic away from {bad} ({kind}).",
                    "patch": {"filters": {**f["filters"], "payloads": {"true": payload}}}}
        if kind == "cost_spike" and cause.startswith("model_switch:"):
            f = by_key.get("draft-ai-model")
            if not f:
                continue
            payload = dict(f["filters"]["payloads"]["true"])
            payload["model"] = "claude-haiku-5.5"
            return {"flag_id": f["id"], "flag_key": f["key"], "reason": f"Unannounced switch to {cause.split(':', 1)[1]}: set Draft with AI back to claude-haiku-5.5.",
                    "patch": {"filters": {**f["filters"], "payloads": {"true": payload}}}}
    return None
