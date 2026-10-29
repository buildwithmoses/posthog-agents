"""Every number the watcher uses comes from these HogQL queries (real HogQL; the mock runs them on DuckDB).

Percentiles are computed over a subquery (`quantile(0.95)(lat)`) so the same text is valid HogQL and
translates cleanly in the mock.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..core.posthog import PostHog

GEN = """SELECT model, provider, count() AS calls, sum(cost) AS cost, quantile(0.5)(lat) AS p50, quantile(0.95)(lat) AS p95,
  countIf(is_error) AS errors, avg(tin) AS in_tokens, avg(tout) AS out_tokens
FROM (SELECT properties.$ai_model AS model, properties.$ai_provider AS provider, toFloat(properties.$ai_total_cost_usd) AS cost,
        toFloat(properties.$ai_latency) AS lat, properties.$ai_is_error = 'true' AS is_error,
        toFloat(properties.$ai_input_tokens) AS tin, toFloat(properties.$ai_output_tokens) AS tout
      FROM events WHERE event = '$ai_generation' AND timestamp >= {start} AND timestamp < {end})
GROUP BY model, provider ORDER BY model, provider"""

SPANS = """SELECT span, count() AS calls, avg(tin) AS in_tokens, sum(cost) AS cost
FROM (SELECT properties.$ai_span_name AS span, toFloat(properties.$ai_input_tokens) AS tin, toFloat(properties.$ai_total_cost_usd) AS cost
      FROM events WHERE event = '$ai_generation' AND properties.$ai_is_error != 'true' AND timestamp >= {start} AND timestamp < {end})
GROUP BY span ORDER BY span"""

ERRORS = """SELECT error, provider, model, count() AS n
FROM (SELECT properties.$ai_error AS error, properties.$ai_provider AS provider, properties.$ai_model AS model
      FROM events WHERE event = '$ai_generation' AND properties.$ai_is_error = 'true' AND timestamp >= {start} AND timestamp < {end})
GROUP BY error, provider, model ORDER BY n DESC, error LIMIT 10"""

TRACES = """SELECT trace, sum(cost) AS cost, count() AS calls, max(tin) AS max_in_tokens
FROM (SELECT properties.$ai_trace_id AS trace, toFloat(properties.$ai_total_cost_usd) AS cost, toFloat(properties.$ai_input_tokens) AS tin
      FROM events WHERE event = '$ai_generation' AND timestamp >= {start} AND timestamp < {end})
GROUP BY trace ORDER BY cost DESC, trace LIMIT 5"""

DRAFTS = """SELECT countIf(event = 'ai_draft_requested') AS requested, countIf(event = 'ai_draft_accepted') AS accepted
FROM events WHERE event IN ('ai_draft_requested', 'ai_draft_accepted') AND timestamp >= {start} AND timestamp < {end}"""

BASELINE_DAYS = 7


def _fmt(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


def windows(as_of: datetime, hours: int) -> tuple[tuple[str, str], list[tuple[str, str]]]:
    """The window under test, and the same clock window on each of the previous 7 days."""
    cur = (_fmt(as_of - timedelta(hours=hours)), _fmt(as_of))
    base = [(_fmt(as_of - timedelta(days=d, hours=hours)), _fmt(as_of - timedelta(days=d))) for d in range(1, BASELINE_DAYS + 1)]
    return cur, base


def collect(ph: PostHog, as_of: datetime, hours: int) -> dict:
    """Run every query for the window and its 7 baseline windows. Returns raw rows (no judgement)."""
    cur, base = windows(as_of, hours)

    def q(text: str, w: tuple[str, str]) -> list[dict]:
        return ph.rows(text, {"start": w[0], "end": w[1]})

    raw = {
        "window": {"start": cur[0], "end": cur[1], "hours": hours},
        "current": {"gen": q(GEN, cur), "spans": q(SPANS, cur), "errors": q(ERRORS, cur), "traces": q(TRACES, cur), "drafts": q(DRAFTS, cur)},
        "baseline": [{"start": w[0], "gen": q(GEN, w), "spans": q(SPANS, w), "drafts": q(DRAFTS, w)} for w in base],
        "annotations": ph._get("/annotations/").get("results", []),
        "flags": ph._get("/feature_flags/").get("results", []),
        "queries": {"gen": GEN, "spans": SPANS, "errors": ERRORS, "traces": TRACES, "drafts": DRAFTS},
    }
    return raw
