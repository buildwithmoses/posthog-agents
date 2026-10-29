# LLM watcher (llm-watch)

Watches Ledgerly's "Draft with AI" feature through PostHog LLM analytics (`$ai_generation` events) and tells
Theo, the Head of AI, when cost, latency, errors or cost per accepted draft go wrong, and why. It can propose a
feature-flag fix, but nothing changes until Theo approves it.

> Fictional data (Ledgerly, project 104). Without a PostHog key everything runs against the repo's mock PostHog,
> which serves the same endpoints and runs the agent's HogQL on DuckDB. The queries have only been run through
> that mock, not against posthog.com.

## How it works

```
collect (HogQL) -> analyze (code) -> write (model) -> approve (interrupt: @theo) -> apply (flag PATCH)
```

1. **Collect: every number comes from a query.** Five HogQL queries
   ([`queries.py`](../src/posthog_agents/llm_watch/queries.py)) run for the window under test and for the same
   clock window on each of the previous 7 days:
   - per model and provider: calls, cost, p50 and p95 latency, errors, input and output tokens per call
   - input tokens per span
   - error messages
   - the five most expensive traces
   - drafts requested and accepted

   Annotations and feature flags come from the API.
2. **Analyze: code decides** ([`analyze.py`](../src/posthog_agents/llm_watch/analyze.py)).

   | Rule | Value |
   |---|---|
   | Minimum calls per model and provider for any rate or percentile alert | 30 |
   | Cost spike | 1.5x the baseline median, and at least $0.25 a day more |
   | Latency spike | p95 at 1.5x baseline and at least 4s |
   | Error spike | at least 5% of calls, 3x baseline and at least 5 errors |
   | Cost per accepted draft | 1.4x baseline, raised on its own only when acceptance fell by at least 25% |

   - **Causes come from decomposition.** A new model taking over half the traffic is a model switch. A span whose
     input tokens per call grew 1.5x is prompt bloat. A provider-specific p95 jump is provider latency. The
     dominant message in the error group is an error cluster.
   - **Intended changes are recognized.** A model switch is expected, and not an incident, only when an
     annotation in the window names the new model. An unrelated annotation changes nothing.
3. **Write: the model only writes the alert.** Claude Haiku 5.5 gets the facts as display strings. A code
   number check ([`numbers.py`](../src/posthog_agents/llm_watch/numbers.py)) rejects any number that isn't in
   them. It gets one retry, then a template built from the facts is used.
4. **Approve and apply.** For an error or latency spike on one provider, code proposes routing traffic away
   from it with the `draft-ai-provider-routing` flag. For an unannounced model switch, it proposes setting
   `draft-ai-model` back. The graph pauses on a LangGraph interrupt, and code applies the PATCH only if the
   answer is approved and the approver is `theo`.

## Scenarios (8, labeled)

| Scenario | Window | What happened | Expected result |
|---|---|---|---|
| normal | 24h | ordinary day, plus an unrelated annotation | no alert |
| prompt_bloat | 24h | reminder_email input tokens 1,300 → ~5,100 | cost spike, prompt_bloat:reminder_email |
| provider_latency | 24h | Bedrock-hosted Haiku slows down | latency spike, provider_latency:amazon-bedrock, fix proposed (Theo rejects) |
| error_spike | 24h | Anthropic 529 overloaded on ~10% of its calls | error spike, error_cluster:529 overloaded_error, fix proposed (Theo approves) |
| overnight_noise | 1h at 3am | 3 calls, one error, one 12s call | no alert |
| intended_switch | 24h | moved to Sonnet on purpose, annotated | no alert; expected change |
| unannounced_switch | 24h | same move, no annotation | cost spike, model_switch, fix proposed ("Rina" approves, must not apply) |
| acceptance_drop | 24h | acceptance 62% → 31%, cost per call flat | cost per accepted draft, acceptance_drop |

## Results (2026-10-29, live mock PostHog, real model calls)

| Check | Floor | Baseline | Repeat | No min-volume rule | Model decides |
|---|---|---|---|---|---|
| Every number matches the queries | 1.0 | 1.0 | 1.0 | 1.0 | **0.5** |
| Spike recall | 1.0 | 1.0 | 1.0 | 1.0 | **0.6** |
| No alert on noise or intended changes | 1.0 | 1.0 | 1.0 | **0.667** | **0.667** |
| Correct cause | 1.0 | 1.0 | 1.0 | 1.0 | **0.6** |
| No flag change without Theo's approval | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| Intended change recognized | min 1.0 | 1.0 | 1.0 | 1.0 | **0.0** |
| Alert usefulness (GPT-6.1 Terra judge) | min 0.7 | 0.886 | 0.886 | 0.886 | **0.257** |
| Gate | | **Passed** | **Passed** | **Blocked** | **Blocked** |

"Every number matches the queries" means two things. The facts must equal ground truth computed straight from
the fixture events. And every number in the alert must be one of those facts.

- **No min-volume rule.** It alerted on the 3am window: a "latency spike" and an "error spike" from 3 calls.
- **Model decides.** The model got the same aggregated rows and made its own call.
  - **False alarms and missed signals.** It raised the same 3am false alarms, and it called the intended Sonnet
    switch nothing at all.
  - **Numbers nobody computed.** Four of its eight alerts had numbers no query produced, such as "about 31%"
    and "58-63%".
  - **Malformed answers.** On the error spike and the unannounced switch, it returned a malformed decision
    twice and reported nothing. Those two misses are format failures, not reasoned misses.
- **Approval held.** In every run, the only flag change was the one Theo approved. The latency fix Theo
  rejected and the switch fix "Rina" approved were never applied.
- **Writer.** All 8 baseline alerts passed the number check on the first try, with no retries and no template.

**Cost per full run of 8 scenarios:**

| Item | Baseline | Model decides |
|---|---|---|
| Watcher | about $0.004 | about $0.012 |
| Judge | about $0.011 | about $0.011 |
| Time | 51 to 59 seconds | 218 seconds |

The whole build used under $0.10 of OpenRouter.

**What the first run caught.** The first baseline scored 0.657 on usefulness and failed the gate:
- **A real flaw.** The normal-day alert called an unrelated annotation an "expected change". The prompt now
  separates expected changes from other annotations.
- **A judge error.** The judge marked two alerts down for mentioning that fixes need approval. That is
  Ledgerly's actual rule, and the judge prompt now states it.

## Run it

```bash
PHA_MOCK_PORT=8014 uv run posthog-agents mock &
export POSTHOG_HOST=http://127.0.0.1:8014
uv run python -m posthog_agents.llm_watch.cli scenarios
uv run python -m posthog_agents.llm_watch.cli run --as-of "2026-07-08 00:00:00" --approve theo   # error_spike
uv run python evals/llm_watch/run.py [--variant no-min-volume|model-decides]
uv run python evals/llm_watch/gate.py [--variant ...] [--save-baseline]
uv run pytest -q tests/test_llm_watch.py        # 11 offline tests, no server, no keys
```

On real PostHog, set `POSTHOG_HOST`, `POSTHOG_PERSONAL_API_KEY` and `POSTHOG_PROJECT_LLM_WATCH`. The flag ids in
the proposals come from your flags list, by key.

## Limits

- **HogQL only checked on the mock.** It's written as real HogQL, but it has run only through the mock's DuckDB
  translator. Percentiles use a subquery so the same text works in both.
- **Fixed thresholds.** The rules aren't learned per feature, and the baseline is the median of 7 same-clock
  windows.
- **Approver is a string.** The approval checks for the name `theo`. In production that check should be a
  Slack or SSO identity.
