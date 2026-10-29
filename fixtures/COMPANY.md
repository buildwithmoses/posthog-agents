# Ledgerly (fictional)

Everything here is made up: the company, its customers, people, events and numbers. Domains end in `.example`.

Ledgerly sells **invoicing and accounts-receivable software to small and mid-size B2B companies**: create
invoices, send payment reminders, accept card and ACH payments, and sync to accounting tools. 70 employees,
fully remote. About 1,800 paying accounts on three plans: Starter ($49/mo), Growth ($199/mo) and Scale
($799/mo, annual contracts, named account owner). Ledgerly has an AI feature, **"Draft with AI"**, that writes
invoice line items and payment-reminder emails, using LLMs through an API.

Ledgerly instruments everything with **PostHog**: product analytics events, session replay, feature flags,
experiments and LLM analytics (`$ai_generation` events).

## Key events (all agents use these names)

| Event | Meaning |
|---|---|
| `$pageview`, `$autocapture`, `$exception` | standard PostHog events |
| `invoice_created`, `invoice_sent`, `payment_received` | core value actions |
| `reminder_sent`, `integration_synced`, `report_viewed` | secondary usage |
| `ai_draft_requested`, `ai_draft_accepted`, `ai_draft_rejected` | Draft with AI feature |
| `$ai_generation` | LLM call (properties: `$ai_model`, `$ai_provider`, `$ai_input_tokens`, `$ai_output_tokens`, `$ai_total_cost_usd`, `$ai_latency`, `$ai_is_error`, `$ai_trace_id`) |
| `checkout_started`, `checkout_completed` | upgrade flow (used in experiments) |

Every event carries `$groups.company` (the account id) and person properties `email`, `plan`, `role`.

## People (approvers)

- **Jordan Pike** (@jordan) VP Customer Success. Owns churn alerts and what gets sent to customers.
- **Amara Okeke** (@amara) Support Lead. Owns ticket enrichment and bug reports filed to engineering.
- **Theo Lindqvist** (@theo) Head of AI. Owns Draft with AI cost, latency and quality.
- **Rina Patel** (@rina) Growth PM. Runs experiments; ships a winner only after review.

## Standing rule

**"Agents read PostHog and draft. Anything that reaches a customer, files a bug, changes a feature flag or
declares an experiment winner waits for a named person. Numbers come from queries, never from the model."**
