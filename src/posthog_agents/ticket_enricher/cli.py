"""uv run python -m posthog_agents.ticket_enricher.cli T01 [--approve] [--variant fuzzy]

Runs one fixture ticket against the mock (or real PostHog if POSTHOG_HOST is set), prints the internal note,
then the reply draft. The reply is only sent with --approve (approver @amara)."""

from __future__ import annotations

import argparse

from .mock_data import TICKETS
from .runner import ensure_mock, run_ticket


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ticket")
    ap.add_argument("--approve", action="store_true")
    ap.add_argument("--variant", default="baseline")
    a = ap.parse_args()
    ensure_mock()
    from .graph import build_graph, ticket_input
    t = next(x for x in TICKETS if x["id"] == a.ticket)
    s = run_ticket(build_graph(variant=a.variant), ticket_input(t), f"cli-{t['id']}", {"approved": a.approve, "approver": "amara"})
    print(f"status: {s['resolution']['status']}  person: {s['resolution'].get('person_id')}\n")
    print(s["note"])
    print("\n--- reply draft (waits for @amara) ---\n" + s.get("reply_draft", ""))
    print(f"\nreply sent: {s.get('reply_sent')}")


if __name__ == "__main__":
    main()
