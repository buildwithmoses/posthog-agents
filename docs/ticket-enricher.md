# Support ticket enricher

A help-desk ticket comes in. Before a support agent opens it, the enricher finds the requester in PostHog and
posts an internal note: what that person actually did in the 48 hours before the ticket, the errors they hit,
their plan, role and feature flags, and the session recordings worth watching, best match first. It also drafts a
reply to the customer, which waits for Amara (Support Lead) to approve.

> Fictional data: Ledgerly, its customers, people, events and recordings are made up (`fixtures/COMPANY.md`,
> `src/posthog_agents/ticket_enricher/mock_data.py`). Runs against the mock PostHog unless `POSTHOG_HOST` is set.

## The customer problem

Ledgerly's support team spends the first reply on every ticket asking "what were you doing, which invoice, what
did the screen say?". The answer is already in PostHog. Pasting a ticket into a chatbot with PostHog access is
risky in three ways this agent is built to stop:

1. **Wrong person.** Duplicate sign-ups share an email, forwarded tickets come from a bookkeeper, and contractors
   work in another company's account. Guessing attaches a stranger's activity to the ticket.
2. **Other customers' data.** A ticket that says "look at my accountant's account too", or an injected
   "attach every customer's data", must not widen the lookup.
3. **Invented evidence.** A note that cites a time or a recording that doesn't exist sends the agent to the wrong
   replay.

## How it works

| Step | Who decides | What happens |
|---|---|---|
| Resolve | code | Normalizes the requester email and asks PostHog for persons with exactly that email (`GET /persons/?email=`). 0 = not found, 2+ = ambiguous, 1 = check the email domain matches the person's account domain, else account mismatch. Names and addresses in the ticket body are never looked up. |
| Gather | code | Real HogQL scoped to that one `person_id`: last seen, and every event in the 48 hours before the ticket. Pulls `$exception` messages and `$feature/*` flag values. Lists the person's recordings and ranks them: an exception inside the recording beats console errors beats recency. |
| Draft | model (Claude Haiku 5.5) | Writes a summary, a timeline that cites evidence ids (E#, R#), a cautious likely cause, a next step and a reply draft. The ticket is wrapped as data; the schema has no timestamp fields. |
| Verify and render | code | Drops any step citing an id that wasn't gathered for this person. Renders every timestamp, event detail and recording link from the evidence, never from model text. Scrubs model-written times, recording ids, other people's emails, phone and card numbers. Strips links and ids from the customer reply. |
| Post note | code | Internal note to the help desk, no approval needed (it's internal and evidence-only). |
| Approve reply | Amara | LangGraph `interrupt`. Only `approved: true` from `amara` sends; a rejection, a timeout or another approver sends nothing. |

Not-found, ambiguous, account-mismatch and no-activity tickets never call the model; they get a fixed note and a
reply draft asking which login the customer uses or when the problem happened.

## Results

16 labeled tickets through the real LangGraph graph against the mock PostHog, 2026-10-29. The judge is GPT-6.1
Terra on OpenRouter, given exactly the evidence the agent had.

| Check | Floor | Baseline | Regression (`--variant fuzzy`) |
|---|---|---|---|
| Right person, or an explicit not found / ambiguous / mismatch | 1.0 | **1.0** | 0.812 (guessed on the duplicate email, followed the forwarded address, skipped the domain check) |
| No other account's or person's data | 1.0 | **1.0** | 0.688 (Copperline's ops user and error on a bookkeeper's ticket; Dana's email in two notes) |
| Every timestamp and recording in the note belongs to the requester | 1.0 | **1.0** | 0.938 (a time that matches none of the requester's events) |
| No customer reply without Amara | 1.0 | **1.0** | 1.0 |
| Injection tickets: nothing above violated | 1.0 | **1.0** | 0.5 |
| Labeled recordings listed | 0.9 | 1.0 | 0.0 (free-form notes dropped the links) |
| Labeled key facts in the note | 0.85 | 1.0 | 1.0 |
| Judge: useful and faithful | 0.7 | 0.781 | 0.594 |
| Gate | | **PASSED** | **FAILED** (6 checks), exit 1 |

The best recording was ranked first on every ticket that had one, including Rachel's, where a later clean session
was more recent than the one with the error. The regression is the "obvious" build: fall back to names and
addresses in the ticket when the email doesn't match exactly, and let the model write the note.

Cost and time per 16-ticket run: agent $0.0056 (11 model calls, 5 tickets need none), judge $0.036, median 5 s
per ticket that calls the model. Total OpenRouter spend for this agent's build was under $0.20.

### What the evals caught while building

- **The judge needs the same evidence the agent had.** Given only the event list, it scored every note 0 for
  "inventing" the plan, recordings and lookup result. With the full evidence it found real problems instead.
- **The model said what recordings "show".** Nobody has watched them; the evidence is metadata. The prompt now
  says so, and the model writes "covers the time of the error". Together with spelling out the judge's 1 / 0.5 / 0
  levels, usefulness went from 0.25 to 0.78.
- Remaining half-scores are mild overreach, mostly the model tying background `invoice_created` events to the
  invoice named in the ticket. The timeline itself is always rendered from evidence.

## Run it

```bash
uv run python -m posthog_agents.ticket_enricher.cli T01 [--approve] [--variant fuzzy]
uv run python evals/ticket_enricher/run.py [--variant fuzzy] [--label x] [--ids T01 T05] [--no-judge]
uv run python evals/ticket_enricher/gate.py [results.json] [--save-baseline]
uv run pytest tests/test_ticket_enricher.py      # 18 offline tests, no keys
```

The CLI and evals start the mock PostHog in-process on port 8013 unless `POSTHOG_HOST` is set. To go live, set
`POSTHOG_HOST`, `POSTHOG_PERSONAL_API_KEY` and `POSTHOG_PROJECT_TICKET_ENRICHER`, and point `Helpdesk` at the real
help desk (the mock serves it under the project path).

## Limits

- The help desk is mocked; a real Zendesk or Help Scout client replaces `Helpdesk` (two methods).
- Person lookup uses the persons API email filter. Persons merged across several emails need PostHog's own merge.
- Free-mail domains (gmail etc.) are treated as an unconfirmed account and attach nothing; some teams will want a
  softer rule there.
- Approver identity in the local run is a name in the resume payload; in production it comes from the help desk login.
