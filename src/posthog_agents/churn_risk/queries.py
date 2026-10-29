"""Every number the agent uses comes from these HogQL queries (real PostHog HogQL; the mock runs the same text).

The account is the company group key: properties.$groups.company.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from ..core.posthog import PostHog

CORE = ("invoice_created", "invoice_sent", "payment_received")
FEATURES = ("reminder_sent", "integration_synced", "report_viewed", "ai_draft_requested")
TRACKED = CORE + FEATURES + ("$exception", "$rageclick")
HISTORY_WEEKS = 8   # weeks before the scored week (4 baseline weeks + room to skip incident weeks)

USAGE_HOGQL = """
SELECT properties.$groups.company AS account, distinct_id, toStartOfDay(timestamp) AS day, event, count() AS n
FROM events
WHERE timestamp >= {start} AND timestamp < {end}
  AND event IN ('invoice_created', 'invoice_sent', 'payment_received', 'reminder_sent', 'integration_synced',
                'report_viewed', 'ai_draft_requested', '$exception', '$rageclick')
GROUP BY account, distinct_id, day, event
""".strip()

FIRST_SEEN_HOGQL = """
SELECT properties.$groups.company AS account, min(timestamp) AS first_seen
FROM events
WHERE timestamp < {end}
GROUP BY account
""".strip()

EVIDENCE_HOGQL = """
SELECT properties.$session_id AS session_id, distinct_id,
       countIf(event = '$exception') AS errors, countIf(event = '$rageclick') AS rage_clicks
FROM events
WHERE properties.$groups.company = {account} AND timestamp >= {start} AND timestamp < {end}
  AND event IN ('$exception', '$rageclick')
GROUP BY session_id, distinct_id
ORDER BY errors + rage_clicks DESC, session_id
LIMIT 10
""".strip()


def week_bounds(week_start: date) -> tuple[str, str]:
    s = datetime.combine(week_start, datetime.min.time())
    return s.strftime("%Y-%m-%d %H:%M:%S"), (s + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")


def fetch_usage(ph: PostHog, week_start: date) -> list[dict]:
    start = datetime.combine(week_start - timedelta(weeks=HISTORY_WEEKS), datetime.min.time())
    _, end = week_bounds(week_start)
    return ph.rows(USAGE_HOGQL, {"start": start.strftime("%Y-%m-%d %H:%M:%S"), "end": end})


def fetch_first_seen(ph: PostHog, week_start: date) -> dict[str, str]:
    _, end = week_bounds(week_start)
    return {r["account"]: str(r["first_seen"])[:10] for r in ph.rows(FIRST_SEEN_HOGQL, {"end": end}) if r["account"]}


def fetch_annotations(ph: PostHog) -> list[dict]:
    return ph._get("/annotations/").get("results", [])


def fetch_evidence(ph: PostHog, account: str, week_start: date, host_for_links: str, limit: int = 3) -> list[dict]:
    start, end = week_bounds(week_start)
    out = []
    for r in ph.rows(EVIDENCE_HOGQL, {"account": account, "start": start, "end": end}):
        if not r["session_id"]:
            continue
        try:
            rec = ph.recording(r["session_id"])     # only link recordings that exist
        except Exception:  # noqa: BLE001
            continue
        out.append({"session_id": r["session_id"], "user": r["distinct_id"], "errors": int(r["errors"]),
                    "rage_clicks": int(r["rage_clicks"]), "duration_s": rec.get("recording_duration"),
                    "url": f"{host_for_links}/project/{ph.project}/replay/{r['session_id']}"})
        if len(out) >= limit:
            break
    return out
