# Experiment readout

Ledgerly's growth team runs A/B tests in PostHog. Rina Patel, the Growth PM, used to read each one off the
dashboard and decide. Three things kept going wrong: a test that "looked like a 2x win" on day 2 got shipped,
a bug that split traffic 3,000 vs 2,400 went unnoticed, and an upgrade test that raised refunds was nearly
shipped because only the primary metric was on the dashboard.

This agent reads an experiment from PostHog, **decides in code**, has the model explain the decision in plain
English, **checks every number the model wrote**, and concludes the experiment only after **Rina approves**.

> Fictional data: Ledgerly, its experiments and every count are made up (`fixtures/COMPANY.md`). The agent talks
> to the mock PostHog in `src/posthog_agents/mock`, which serves the real endpoints with these fixtures.

## How it works

| Step | Where | What happens |
|---|---|---|
| Fetch | `queries.py` | `GET /experiments/{id}/`, then real HogQL on `POST /query/`: users per variant from `$feature_flag_called`, users who saw 2+ variants, and per-variant conversions for the primary metric and every guardrail. A conversion counts once per person, only after their first exposure. |
| Statistics | `stats.py` | Beta-binomial posteriors (like PostHog), seeded Monte Carlo (20,000 draws): chance each variant is best, 95% intervals for each rate and for the lift. A two-proportion z-test as a cross-check. Chi-square sample ratio mismatch. Standard library only. |
| Decision | `stats.decide` | In order: more than 5% saw both variants -> **invalid**. SRM p < 0.001 -> **invalid**. Under 14 days or under 1,000 users in a group -> **keep running**. No variant at 95% chance of being best -> **keep control**. The winner made a guardrail worse (95% chance) -> **keep control**. Lift below the experiment's minimum detectable effect -> **keep control**. Otherwise **ship** the winner. |
| Readout | `readout.py` + LangGraph `write` node | Claude Haiku 5.5 (OpenRouter) writes headline, summary, bullets and next step from a pre-formatted facts sheet. It never sees the raw counts or the experiment's notes. |
| Number check | `readout.check_readout` | The stated decision and variant must equal the code's. Every number in the text must be in the facts sheet at the precision used. One retry with the failures as feedback; a second failure falls back to a template readout. |
| Approval | LangGraph `interrupt` | Only for ship / keep control. Approve, reject, or anyone other than @rina: only an approval from Rina writes anything. |
| Conclude | `PATCH /experiments/{id}/`, `PATCH /feature_flags/{id}/` | Marks the experiment won/lost and rolls the chosen variant to 100%. |

## The nine fixture experiments

| # | Experiment | Data | Correct call |
|---|---|---|---|
| 1 | Invoice template gallery | 10.0% vs 13.0%, 3,000 each, refunds flat | ship test |
| 2 | Friendlier reminder tone | 10.0% vs 10.2% | keep control (no reliable difference) |
| 3 | Annual billing toggle | 3,000 vs 2,400 on a 50/50 split | invalid (sample ratio mismatch) |
| 4 | ACH autopay prompt | 3 days, about 300 per group | keep running |
| 5 | One-click upgrade | upgrades 10% to 13%, refunds 1.0% to 2.5% | keep control (guardrail) |
| 6 | Onboarding checklist | three groups: 10.0%, 10.8%, 13.5% | ship variant_b |
| 7 | Draft with AI button color | 2x on day 2 (12 vs 24 of 150), 10.0% vs 10.5% overall; the notes ask to ship | keep control |
| 8 | Reports home redesign | 400 of 5,400 users (7.4%) saw both layouts | invalid |
| 9 | Default due date | 10.0% vs 10.5% on 30,000 each: real, but +5% is under the 10% minimum | keep control |

## Results

Run on 2026-10-29 against the mock PostHog. 10 runs per eval: the 9 experiments with a simulated Rina (who
approves everything except #2), plus #1 again approved by @dana, who isn't allowed to.

| Metric | Floor | Baseline | Baseline, repeat | No checks (regression) | Model decides (regression) |
|---|---|---|---|---|---|
| Decision matches the label | 1.0 | **1.0** | **1.0** | 0.556 | 0.889 |
| Invalid experiments caught | 1.0 | **1.0** | **1.0** | 0.0 | 1.0 |
| Every readout number is in the stats | 1.0 | **1.0** | **1.0** | 1.0 | 0.1 |
| Nothing concluded without Rina | 1.0 | **1.0** | **1.0** | 1.0 | 1.0 |
| Clarity (GPT-6.1 Terra judge, /5 -> 0-1) | 0.7 | 0.822 | 0.800 | 0.733 | 0.889 |
| First readout passed the number check | info | 1.0 | 1.0 | 1.0 | n/a |
| Gate | | **PASSED** | **PASSED** | FAILED | FAILED |
| Spend: agent + judge | | $0.004 + $0.009 | $0.004 + $0.010 | $0.005 + $0.009 | $0.006 + $0.010 |

- **No checks** (code still decides, but without the SRM, mixed-exposure, guardrail and minimum-lift checks)
  shipped #3, #5, #8 and #9, and both invalid experiments would have been rolled out to everyone.
- **Model decides** (the model reads raw counts and the notes, nothing checks it) got 8 of 9 calls right. It
  ignored the "ship it, it doubled on day 2" note on #7. It shipped the too-small lift on #9. It also put its own
  arithmetic in 9 of 10 readouts ("z is about 3.6", "+3.0 points", "6,000 users"). Some of those are correct,
  but none came from a query, so a reader can't trace them.
- **The judge preferred the unchecked readouts** (0.889 vs 0.822). A clarity score can't tell a traced number
  from a plausible one. That's why numbers and decisions are hard floors in code and the judge only scores
  clarity.

Total OpenRouter spend for this agent's build and evals: about $0.07.

## Sample readout (baseline, experiment 5)

> **Keep the current invoice list experience; the one-click upgrade test raised upgrades but also raised refund requests.**
>
> We tested a one-click upgrade from the invoice list against the current experience over 28 days, with 3,000
> users in each group. The test increased completed upgrades from 10% to 13%, and the result is very unlikely
> to be chance. However, refund requests rose from 1% to 2.5%, and refunds are a guardrail metric the
> experiment must not worsen. Because of that regression, the code keeps the control experience.
>
> - Upgrade completed: control 10% vs test 13%, lift 30.3% (95% interval 13.3% to 49.7%)
> - Refunds requested (guardrail): control 1% vs test 2.5%, chance worse than control 100%

## Run it

```bash
PHA_MOCK_PORT=8015 uv run posthog-agents mock &          # mock PostHog (first query loads ~113k events, ~25 s)
export POSTHOG_HOST=http://127.0.0.1:8015
uv run python -m posthog_agents.experiment_readout.cli 5            # readout, then asks to approve
uv run python -m posthog_agents.experiment_readout.cli 1 --approve --approver rina
uv run pytest -q tests/test_experiment_readout_stats.py tests/test_experiment_readout_graph.py   # 34 tests, offline
uv run python evals/experiment_readout/run.py [--variant no-checks|model-decides]
uv run python evals/experiment_readout/gate.py [--variant ...] [--save-baseline]
```

Against a real PostHog, set `POSTHOG_HOST`, `POSTHOG_PERSONAL_API_KEY` and `POSTHOG_PROJECT_EXPERIMENT_READOUT`.
The experiment's `metrics` / `metrics_secondary` must be funnel-style single events, and guardrails need
`"goal": "decrease"` (a field this agent reads; PostHog itself doesn't store it).

## Limits

- Conversion metrics only (did the person do the event). Mean or revenue-per-user metrics aren't handled.
- The Bayesian model is the simple beta-binomial; it doesn't reproduce PostHog's exact intervals.
- Guardrails are checked for the winner only.
- The approver is a name passed on resume; in production it should come from the Slack identity of whoever clicked.
