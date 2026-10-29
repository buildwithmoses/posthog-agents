# Churn risk agent (PostHog)

Every week this agent reads PostHog, finds Ledgerly customers whose usage is slipping, alerts the account
owner in Slack with session recordings as evidence, and drafts a check-in email that waits for a person.
**Code decides who is at risk. The model only writes the words, and code checks every number it writes.**

> Fictional company and data: Ledgerly ([`../fixtures/COMPANY.md`](../fixtures/COMPANY.md)). 40 accounts, 10 weeks,
> about 14,500 events and 3,400 recordings, generated deterministically in
> [`churn_risk/mock_data.py`](../src/posthog_agents/churn_risk/mock_data.py) and served by the mock PostHog.

## The customer problem

Ledgerly's customer success team (VP: Jordan Pike) learns about churn when the cancellation arrives. They tried a
weekly "usage dropped" dashboard and stopped trusting it:

1. **It cried wolf.** The week the web SDK broke, every account "dropped" at once and 38 alerts went out.
2. **It flagged accounts that were fine:** brand-new customers still setting up, and seasonal businesses that
   close for harvest every October.
3. **It nagged.** The same account got alerted every week until someone muted the channel.
4. **It gave no reason to act.** "Usage down 40%" doesn't say what happened. A recording of a user hitting an
   error three times does.

## How it works

```
gather (HogQL) -> score (code) -> tracking incident?  yes -> report it to #cs-leads, alert nobody
                                  no  -> evidence (recordings) -> draft (model) -> check (code)
                                         -> Slack alert to owner -> approve (interrupt: @jordan) -> send + annotate
```

| Step | What decides | Detail |
|---|---|---|
| Gather | HogQL | Usage per account, user, day and event for the scored week plus 8 before it; first-seen per account; annotations |
| Score | Code ([`scoring.py`](../src/posthog_agents/churn_risk/scoring.py)) | Each account against its own trailing 4-week baseline, with incident weeks excluded |
| Evidence | Code + HogQL | Up to 3 sessions from that account's users with `$exception` or `$rageclick`, kept only if the recording exists |
| Draft | Model (Claude Haiku 5.5) | Slack alert and customer email, from a FACTS JSON only |
| Check | Code ([`numbers.py`](../src/posthog_agents/churn_risk/numbers.py)) | Every number must be in FACTS; the email may not mention churn, risk, recordings, analytics or any number. One retry, then a template |
| Approve | Person | LangGraph `interrupt`: @jordan approves, edits or rejects each email. Approvals from anyone else are ignored |
| Send | Code | Only approved emails are sent; each writes a `churn-risk: alerted <account>` PostHog annotation |

### Rules (all in code)

| Signal | At risk when |
|---|---|
| Usage drop | invoice_created + invoice_sent + payment_received <= 60% of the 4-week baseline |
| Champion left | a user who did >= 40% of baseline core actions did nothing this week |
| Seat cut | active users / seats < 50% and active users <= 70% of baseline |
| Error spike | `$exception` + `$rageclick` >= max(10, 3x baseline) and core actions <= 80% of baseline |

| Suppression | Never alert when |
|---|---|
| Tracking incident | >= 60% of established accounts are below 60% of their own baseline the same week. Reported to #cs-leads instead. That week is also left out of later baselines |
| Ramping | the account was first seen < 28 days before the week |
| Seasonal | a `seasonal:<account> ... until YYYY-MM-DD` annotation covers the week |
| Recently alerted | a `churn-risk: alerted <account>` annotation in the last 14 days |

### HogQL used

```sql
-- usage (one query for all accounts, 9 weeks)
SELECT properties.$groups.company AS account, distinct_id, toStartOfDay(timestamp) AS day, event, count() AS n
FROM events
WHERE timestamp >= {start} AND timestamp < {end}
  AND event IN ('invoice_created', 'invoice_sent', 'payment_received', 'reminder_sent', 'integration_synced',
                'report_viewed', 'ai_draft_requested', '$exception', '$rageclick')
GROUP BY account, distinct_id, day, event

-- first seen
SELECT properties.$groups.company AS account, min(timestamp) AS first_seen FROM events WHERE timestamp < {end} GROUP BY account

-- evidence sessions for one account
SELECT properties.$session_id AS session_id, distinct_id,
       countIf(event = '$exception') AS errors, countIf(event = '$rageclick') AS rage_clicks
FROM events
WHERE properties.$groups.company = {account} AND timestamp >= {start} AND timestamp < {end}
  AND event IN ('$exception', '$rageclick')
GROUP BY session_id, distinct_id ORDER BY errors + rage_clicks DESC, session_id LIMIT 10
```

Seats, plan and owner come from the CRM roster ([`accounts.py`](../src/posthog_agents/churn_risk/accounts.py)), not
PostHog. Mock note: the shared mock translates one level of `properties.x`, so this project's mock pre-translates
`properties.$groups.company` before running the unchanged HogQL.

## Results

Two weeks per run: **Oct 19–25** (9 truly at-risk accounts, 2 seasonal, 2 new, 1 alerted on Oct 14, 2 with a
harmless ~22% dip) and **Sep 28–Oct 4** (tracking outage). Simulated @jordan approves every email except: one
rejected, one edited, and one "approval" sent by @devon (must not count). Judge: GPT-6.1 Terra.

| Metric | Floor | Baseline | Baseline, repeat | No guards | Model decides |
|---|---|---|---|---|---|
| At-risk recall | 1.0 | 1.0 | 1.0 | **0.778** | 1.0 |
| No alert in the tracking-outage week (and incident reported) | 1.0 | 1.0 | 1.0 | **0.0** (38 alerts) | **0.0** (no alerts, no incident report) |
| No alert on seasonal, new or recently alerted | 1.0 | 1.0 | 1.0 | **0.0** | **0.0** |
| Every number matches the queries | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| Facts match the generator's truth | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| No customer email without @jordan | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| Customer email guardrails | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| Precision | min 0.8 | 1.0 | 1.0 | **0.583** | **0.643** |
| Alert quality (judge) | min 0.7 | 0.933 | 0.956 | 0.933 | 0.893 |
| Gate | | PASSED | PASSED | FAILED | FAILED |
| Spend (agent + judge) | | $0.007 + $0.014 | $0.008 + $0.012 | $0.035 + $0.017 | $0.014 + $0.019 |

What the regressions show:
- **No guards** (same thresholds, no incident/ramp/seasonal/re-alert checks) sent 38 alerts in the outage week and
  flagged both new accounts, both seasonal accounts and the account alerted on Oct 14. It also **missed both
  error-spike accounts**: the outage week sat in their baseline, so the real drop looked small.
- **Model decides** (Haiku reads the raw weekly numbers) found all 9 real cases but flagged the same 5 that should
  stay quiet. In the outage week it alerted nobody, and also told nobody tracking was broken.

Every draft in the baseline runs passed the number check on the first try. One eval-harness fix along the way:
the judge first marked every alert down for mentioning "Oct 19–25" because it wasn't shown the week; it now is.

## Run it

```bash
PHA_MOCK_PORT=8011 uv run posthog-agents mock &                      # mock PostHog (or set POSTHOG_HOST + key)
export POSTHOG_HOST=http://127.0.0.1:8011
uv run python -m posthog_agents.churn_risk.cli --week 2026-10-19      # you approve the emails
uv run python evals/churn_risk/run.py [--variant no-guards|model-decides]
uv run python evals/churn_risk/gate.py [evals/churn_risk/results/latest-<variant>.json]
uv run pytest -q tests/test_churn_risk.py                              # 16 offline tests, no keys
```

Limits: the Slack DM and customer email are recorded, not sent (no Slack or email credentials). Week boundaries
are Monday–Sunday in UTC. Against real PostHog, check the `$groups.company` key and the `seasonal:` annotation
convention match how your team uses them.
