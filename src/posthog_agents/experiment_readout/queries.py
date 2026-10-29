"""HogQL for experiment readouts. These are real HogQL queries for POST /api/projects/{id}/query/.

Exposure = the person's `$feature_flag_called` events for this flag inside the experiment window.
A person who saw more than one variant is excluded from every group and counted separately.
A conversion counts once per person, only if it happened after that person's first exposure and before the
window ends (so conversions from before the experiment never count).
"""

from __future__ import annotations

from datetime import datetime

from ..core.posthog import PostHog

_EXPOSED = """
    SELECT person_id,
           min(timestamp) AS first_seen,
           min(properties.$feature_flag_response) AS variant,
           count(DISTINCT properties.$feature_flag_response) AS n_variants
    FROM events
    WHERE event = '$feature_flag_called'
      AND properties.$feature_flag = {flag}
      AND timestamp >= {start} AND timestamp < {end}
    GROUP BY person_id
"""

EXPOSURES = f"""
SELECT variant, count() AS users
FROM ({_EXPOSED}) AS exposed
WHERE n_variants = 1
GROUP BY variant
ORDER BY variant
"""

MULTI_EXPOSED = f"""
SELECT count() AS users
FROM ({_EXPOSED}) AS exposed
WHERE n_variants > 1
"""

CONVERSIONS = f"""
SELECT exposed.variant AS variant, count(DISTINCT exposed.person_id) AS converted
FROM ({_EXPOSED}) AS exposed
JOIN (SELECT person_id, timestamp FROM events WHERE event = {{metric_event}} AND timestamp < {{end}}) AS conv
  ON conv.person_id = exposed.person_id
WHERE exposed.n_variants = 1 AND conv.timestamp >= exposed.first_seen
GROUP BY variant
ORDER BY variant
"""


def _ts(iso: str | None, fallback: str) -> str:
    if not iso:
        return fallback
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")


def fetch(ph: PostHog, experiment_id: int, now: str) -> dict:
    """Everything the readout needs, straight from PostHog: the experiment, users per variant, people who saw
    more than one variant, and conversions per variant for the primary metric and every guardrail."""
    exp = ph.experiment(experiment_id)
    start = _ts(exp.get("start_date"), now)
    end = _ts(exp.get("end_date"), now)
    vals = {"flag": exp["feature_flag_key"], "start": start, "end": end}
    users = {r["variant"]: int(r["users"]) for r in ph.rows(EXPOSURES, vals)}
    multi = int((ph.rows(MULTI_EXPOSED, vals) or [{"users": 0}])[0]["users"])
    planned = {v["key"]: float(v["rollout_percentage"]) for v in exp["parameters"]["feature_flag_variants"]}
    for k in planned:
        users.setdefault(k, 0)

    def conv(event: str) -> dict[str, int]:
        got = {r["variant"]: int(r["converted"]) for r in ph.rows(CONVERSIONS, {**vals, "metric_event": event})}
        return {k: got.get(k, 0) for k in planned}

    metrics = []
    for m, goal in [(m, "increase") for m in exp.get("metrics", [])] + [(m, "decrease") for m in exp.get("metrics_secondary", [])]:
        event = m["series"][0]["event"]
        metrics.append({"name": m.get("name") or event, "event": event, "goal": m.get("goal", goal), "conversions": conv(event)})
    runtime_days = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 86400
    return {"experiment": exp, "window": {"start": start, "end": end}, "running": exp.get("end_date") is None,
            "planned_pct": planned, "users": users, "multi_exposed": multi, "metrics": metrics,
            "runtime_days": runtime_days, "min_lift": float(exp["parameters"].get("minimum_detectable_effect", 0)) / 100}
