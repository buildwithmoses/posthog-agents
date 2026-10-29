"""Fetched counts -> statistics -> code decision -> a flat, pre-formatted facts sheet.

The facts sheet is the ONLY thing the model sees for the baseline readout, and the number check accepts only
numbers that appear in it (at the precision the model chose to round to).
"""

from __future__ import annotations

from dataclasses import asdict

from . import stats as S


def analyze(data: dict, *, checks_enabled: bool = True) -> dict:
    users = data["users"]
    metrics = []
    for m in data["metrics"]:
        metrics.append(S.analyze_metric(m["name"], m["event"], m["goal"], users, m["conversions"]))
    primary = next(m for m in metrics if m.goal == "increase")
    guardrails = [m for m in metrics if m.goal == "decrease"]
    decision = S.decide(primary=primary, guardrails=guardrails, planned_pct=data["planned_pct"],
                        multi_exposed=data["multi_exposed"], runtime_days=data["runtime_days"],
                        min_lift=data["min_lift"], checks_enabled=checks_enabled)
    return {"primary": primary, "guardrails": guardrails, "decision": decision,
            "facts": facts_sheet(data, primary, guardrails, decision)}


def pct(x: float | None, d: int = 1) -> str | None:
    return None if x is None else f"{x * 100:.{d}f}%"


def facts_sheet(data: dict, primary: S.MetricResult, guardrails: list[S.MetricResult], decision: S.Decision) -> dict:
    exp = data["experiment"]
    ck = decision.checks
    f: dict = {
        "experiment": exp["name"],
        "decision": decision.decision,
        "decision_variant": decision.variant,
        "decision_reason": decision.reason,
        "decision_explanation": decision.explanation,
        "runtime_days": f"{data['runtime_days']:.0f}",
        "still_running": data["running"],
        "planned_split_percent": {k: f"{v:g}%" for k, v in data["planned_pct"].items()},
        "users_who_saw_more_than_one_variant": ck["multi_exposed"],
        "share_who_saw_more_than_one_variant": pct(ck["multi_exposed_share"]),
        "sample_ratio_mismatch_p_value": "below 0.001" if ck["srm_p"] < 0.001 else f"{ck['srm_p']:.3f}",
        "rules": {"win_probability_needed": "95%", "minimum_lift_worth_shipping": pct(data["min_lift"], 0),
                  "minimum_runtime_days": S.MIN_RUNTIME_DAYS, "minimum_users_per_variant": S.MIN_USERS_PER_VARIANT,
                  "max_share_seeing_both_variants": "5%", "srm_p_value_limit": "0.001"},
        "primary_metric": _metric_facts(primary),
        "guardrail_metrics": [_metric_facts(g) for g in guardrails],
    }
    if ck.get("guardrail_failed"):
        f["guardrail_that_failed"] = ck["guardrail_failed"]
    return f


def _metric_facts(m: S.MetricResult) -> dict:
    out = {"name": m.name, "event": m.event, "goal": "higher is better" if m.goal == "increase" else "lower is better",
           "variants": []}
    for v in m.variants:
        row = {"variant": v.key, "users": v.users, "converted": v.conversions, "rate": pct(v.rate, 2),
               "rate_95_interval": [pct(v.ci_low, 2), pct(v.ci_high, 2)]}
        if m.goal == "increase":
            row["chance_of_being_best"] = pct(v.prob_best)
        if v.lift_mean is not None:
            row["lift_vs_control"] = pct(v.lift_mean)
            row["lift_95_interval"] = [pct(v.lift_ci_low), pct(v.lift_ci_high)]
            row["chance_better_than_control" if m.goal == "increase" else "chance_worse_than_control"] = pct(v.prob_beats_control)
            row["frequentist_p_value_vs_control"] = f"{v.p_vs_control:.3f}" if v.p_vs_control >= 0.001 else "below 0.001"
        out["variants"].append(row)
    return out


def decision_dict(decision: S.Decision) -> dict:
    d = asdict(decision)
    d["checks"] = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in d["checks"].items()}
    return d
