# PostHog Agents: five agents for one SaaS company that runs on PostHog

Five LangGraph agents for **Ledgerly**, a fictional invoicing company that instruments everything with
PostHog: product analytics, session replay, feature flags, experiments and LLM analytics. Each agent reads
PostHog through the real API and HogQL, decides in code, and waits for a named person before anything
reaches a customer, files a bug, changes a flag or declares a winner.

> "Agents read PostHog and draft. Anything that reaches a customer, files a bug, changes a feature flag or
> declares an experiment winner waits for a named person. Numbers come from queries, never from the model."

> **Everything is fictional.** Ledgerly, its accounts, people, events, recordings and experiments are made up
> ([`fixtures/COMPANY.md`](fixtures/COMPANY.md)). The agents run against a **mock PostHog** that serves the
> same endpoints and runs HogQL on DuckDB. Point `POSTHOG_HOST` and `POSTHOG_PERSONAL_API_KEY` at a real
> project to go live.

## The five agents

| Agent | What it does | Who approves | What the gate blocked |
|---|---|---|---|
| [Churn risk](docs/churn-risk.md) | Scores each account against its own 4-week baseline (usage drop, champion left, seat cut, error spike), links session recordings as evidence, and stays quiet for tracking outages, new accounts, seasonal dips and recent alerts | Jordan (CS), before any customer email | No guards: 38 alerts during a tracking outage. Model decides: silent through the outage, never reported it |
| [Replay to bug](docs/replay-to-bug.md) | Turns a session recording into a verified bug report: PII scrubbed first, every step, timestamp, status code and error checked against the timeline, duplicates become +1 comments | Amara (Support), before filing | Raw recording to the model: filed a customer's email and full card number, and a duplicate bug |
| [Ticket enricher](docs/ticket-enricher.md) | Finds the requester by exact email, pulls their last 48 hours of events, errors, flags and best recordings, and writes an internal note whose every citation belongs to that person | Amara, before any customer reply | Fuzzy lookup: another company's user in a bookkeeper's ticket, half the injected instructions followed |
| [AI feature monitor](docs/llm-watch.md) | Watches `$ai_generation` cost, latency and errors per model, finds the cause (prompt bloat, provider latency, error cluster, acceptance drop), ignores low volume and announced model switches, proposes a flag change | Theo (AI), before the flag PATCH | Model decides: invented numbers in half its alerts, missed a planned switch, false alarms at 3am |
| [Experiment readout](docs/experiment-readout.md) | Computes Bayesian and frequentist stats, sample-ratio mismatch, mixed exposure, guardrails and minimum lift in code, decides ship / keep running / keep control / invalid, and writes a plain-English readout | Rina (Growth), before concluding and rolling out | No checks: shipped two invalid experiments and one that raised refunds. Model decides: wrote its own numbers in 9 of 10 readouts |

## Results

All five ran together against one mock PostHog on 2026-10-29. Every hard floor held and every gate passed;
each agent's regression versions were run separately and blocked.

| Agent | Cases | Hard floors (all 1.0) | Quality | Model + judge cost per run |
|---|---|---|---|---|
| Churn risk | 40 accounts, 10 weeks | at-risk recall, no outage alerts, no seasonal/new/recent alerts, numbers match queries, no email without approval | precision 1.0, alert quality 0.867 | $0.007 + $0.012 |
| Replay to bug | 14 recordings | no PII, citations verified, not-a-bug never filed, no duplicate filed, approval, injection resisted | root cause 1.0, usefulness 0.84 | $0.020 total |
| Ticket enricher | 16 tickets | right person or explicit not-found, no cross-account data, valid citations, approval, injection | recording recall 1.0, usefulness 0.781 | $0.006 + $0.033 |
| AI feature monitor | 8 scenarios | numbers, spike recall, no false alerts, correct cause, no unapproved flag change | usefulness 0.886 | $0.004 + $0.010 |
| Experiment readout | 9 experiments | correct decision, invalid caught, numbers match, approval | clarity 0.844 | $0.004 + $0.010 |

120 offline tests pass with no keys. Total OpenRouter spend for the whole build was under $1.

### Findings worth knowing

- **The AI grader preferred the unchecked readouts.** In the experiment agent, the version where the model
  decided scored higher on clarity (0.889 against 0.822) while writing its own numbers into 9 of 10 readouts.
  That's why decisions and numbers are hard floors in code, not judged.
- **"Everyone dropped" is not churn.** Without the outage rule, the churn agent sent 38 alerts in a week when
  tracking broke. The model-decides version went silent instead and never told anyone tracking was down.
- **Verifiers need their own tests.** The replay verifier first read stack-trace line numbers as timestamps and
  "412 ms" as a status code, and sent a correct report to the fallback. Both are now unit tests.

## How it's built

```
fixtures/COMPANY.md              the fictional company, its events and approvers
src/posthog_agents/core/         config (OpenRouter models), PostHog API client, cost tracking, gates
src/posthog_agents/mock/         mock PostHog: real endpoints, HogQL translated to DuckDB, one project per agent
src/posthog_agents/<agent>/      queries, code rules, LangGraph graph with an approval interrupt, cli, mock_data
evals/<agent>/                   run.py, gate.py, baseline.json, results/
docs/<agent>.md                  the customer problem, how it works, HogQL used, full results
```

- **Real API, real HogQL.** The client calls `/api/projects/:id/query/` with HogQLQuery, plus
  `session_recordings`, `persons`, `experiments` and `annotations`. Reads retry on 429/5xx; writes never retry.
- **Code decides, the model writes.** Claude Haiku 5.5 on OpenRouter writes from a facts sheet. A code number
  check rejects any number not in the facts, with one retry and then a template.
- **A person approves.** Every consequential write is behind a LangGraph interrupt that checks who approved.
- **Evals.** Labeled fixtures, code checks for every hard floor, and GPT-6.1 Terra as an independent judge
  for usefulness only.

## Run it

```bash
uv sync
uv run posthog-agents mock &                      # mock PostHog on :8010 (skip if using real PostHog)
export POSTHOG_HOST=http://127.0.0.1:8010
uv run pytest -q                                  # 120 offline tests, no keys
uv run python evals/<agent>/run.py && uv run python evals/<agent>/gate.py
uv run python -m posthog_agents.churn_risk.cli --week 2026-10-19
```

Going live needs: real project ids (`POSTHOG_PROJECT_<AGENT>`), an rrweb-to-timeline adapter for real
replay snapshots, and real Slack, help-desk and Linear clients in place of the recorded outboxes.

Sibling builds: [elevenlabs-agents](https://github.com/buildwithmoses/elevenlabs-agents),
[n8n-agents](https://github.com/buildwithmoses/n8n-agents),
[langchain-agents](https://github.com/buildwithmoses/langchain-agents),
[openrouter-agents](https://github.com/buildwithmoses/openrouter-agents).
