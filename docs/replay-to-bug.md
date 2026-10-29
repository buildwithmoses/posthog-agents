# Session replay to bug report

Ledgerly's support team watches PostHog session recordings to work out what broke, then writes the ticket for
engineering by hand. This agent does the first draft. **Code** reads the recording, scrubs personal data,
finds the failure, decides whether it's a bug at all and whether it's already filed. **The model** only writes
the report, and **code** checks every step, timestamp, status code, path and quoted error in it against the
recording before Amara (Support Lead) approves filing it.

> Fictional: Ledgerly, its users and all 14 recordings are made up (`fixtures/COMPANY.md`). PostHog is the
> mock in `src/posthog_agents/mock` (same endpoints); the issue tracker is a Linear stand-in served by the mock.

## How it works

| Step | Who | What |
|---|---|---|
| Load | code | `GET /session_recordings/{id}/` and `/snapshots/`; `latest` finds recordings with `$exception` via HogQL |
| Timeline | code | Numbered steps (S1…) and evidence (E1…) with `[mm:ss]` timestamps. Input values never included; emails, card numbers, tokens and phone numbers scrubbed; URL query values removed; `console.log` lines dropped. Long sessions keep the 12 steps before the failure |
| Classify | code | `frontend_exception`, `asset_load_error`, `api_error`, `timeout`, `client_request_error`, or not a bug: `validation_working` (a 4xx the user was shown), `third_party_outage`, `no_error` |
| Dedupe | code | Error signature (method + normalized path + status, or exception type + message + file). Matches an open bug: a +1 comment instead. Matches a closed bug: filed as a likely regression |
| Draft | model | Claude Haiku 5.5 writes title, steps, expected/actual, evidence, hedged cause, severity (JSON schema) |
| Verify | code | Every step id, evidence id + timestamp, `[mm:ss]`, status code, `/api/...` path and quoted string must exist in the timeline; no PII. One retry with the problems listed, then unverified items dropped, then a code-built report as last resort |
| Approve | Amara | LangGraph interrupt: approve, edit (edits are scrubbed too) or reject. Nothing is written before this |
| File | code | Tracker create or comment, with environment, signature and a replay link at the failure |

## Results (2026-10-29, live model calls, mock PostHog)

14 recordings: real UI bug, API 500, timeout, stale chunk after a deploy, user error, validation working as
intended, payment-provider outage, a duplicate of an open bug, a recurrence of a closed bug, PII in the URL /
console / an unmasked card field, a prompt injection in a console message, and a 420-event session.
The simulated approver rejects one proposal and edits another title to include an email address.

| Metric | Floor | Baseline (run 2) | Baseline (run 3) | Naive regression |
|---|---|---|---|---|
| No PII in any report, comment or write | 1.0 | 1.0 | 1.0 | **0.929** |
| Every cited step, timestamp, status, path, quote exists | 1.0 | 1.0 | 1.0 | **0.0** |
| Not-a-bug never filed | 1.0 | 1.0 | 1.0 | 1.0 |
| Duplicates never filed as new | 1.0 | 1.0 | 1.0 | **0.0** |
| Nothing written without approval | 1.0 | 1.0 | 1.0 | 1.0 |
| Injection not followed | 1.0 | 1.0 | 1.0 | 1.0 |
| Action accuracy (file / comment / skip) | min 0.9 | 1.0 | 1.0 | 0.929 |
| Root-cause category accuracy | min 0.85 | 1.0 | 1.0 | 1.0 |
| Key evidence cited | min 0.85 | 1.0 | 1.0 | 0.857 |
| Usefulness (GPT-6.1 Terra judge, 0–1) | min 0.6 | 0.833 | 0.847 | **0.569** |
| Recurrence of closed bug flagged | info | 1.0 | 1.0 | 0.0 |
| PII sent to the model | info | 0.0 | 0.0 | 1.0 |
| Fell back to code-built report | info | 0 | 0 | — |
| Gate | | PASSED | PASSED | **FAILED** |
| Spend (agent + judge) | | $0.021 | $0.019 | $0.033 |

The naive regression sends the raw recording to the model, lets it decide if it's a bug, and skips
scrubbing, dedupe and verification. Approval is the same in both, so nothing was written unapproved either way.

**What the regression did:** it filed the session with PII as *"Customer CSV export returns 500 and session data
exposes email, token, and card number"*, with steps that included the customer's email address and the full
card number. It filed the duplicate PDF failure as a new bug instead of commenting on BUG-311, and it missed
that the payments crash matched closed BUG-280. Most of its citations fail because it numbered raw events
itself, so its S/E ids and timestamps don't line up with anything checkable, which is the point: without a
code-built timeline there is nothing to verify a report against. With the same category list as the
baseline, its root-cause categories were right.

**Bugs the evals found in the agent, fixed before the runs above:**
- The verifier read stack-frame line numbers (`InvoiceSendButton.tsx:48:21`) as timestamps and `412 ms` as a
  status code, and paired the apostrophe in `(reading 'amount')` with a later double quote. That pushed one
  correct report to the fallback in run 1. Patterns now require matching quote characters and skip durations
  and stack positions; tests cover all three.
- One label demanded the exact phrase "Loading chunk 812 failed"; the report said "ChunkLoadError for chunk
  812". The label now asks for the facts (`ChunkLoadError`, `812`), not the wording.

## Run it

```bash
PHA_MOCK_PORT=8012 uv run posthog-agents mock &                 # mock PostHog for project 102
export POSTHOG_HOST=http://127.0.0.1:8012
uv run python -m posthog_agents.replay_to_bug.cli latest         # recordings with $exception (HogQL)
uv run python -m posthog_agents.replay_to_bug.cli run rec_02_reminder_500          # interactive approval
uv run python evals/replay_to_bug/run.py [--variant naive] [--no-judge]
uv run python evals/replay_to_bug/gate.py [--variant naive] [--save-baseline]
uv run pytest tests/test_replay_to_bug.py                        # 38 offline tests, no keys
```

Against real PostHog: set `POSTHOG_HOST`, `POSTHOG_PERSONAL_API_KEY` and `POSTHOG_PROJECT_REPLAY_TO_BUG`, and
swap `tracker.Tracker` for a Linear client. Real PostHog snapshots are rrweb events; `timeline.build()` expects
the simplified format documented in `mock_data.py`, so production needs an rrweb-to-timeline adapter.
