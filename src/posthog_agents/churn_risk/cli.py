"""uv run python -m posthog_agents.churn_risk.cli --week 2026-10-19 [--variant baseline|no-guards|model-decides] [--auto approve|reject]

Without --auto, you are @jordan: each customer email is shown and you type a (approve), e (edit) or r (reject).
"""

from __future__ import annotations

import argparse
import json
import uuid

from langgraph.types import Command

from ..core.config import load_env
from ..core.cost import CostTracker
from .graph import build_graph


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--week", default="2026-10-19", help="Monday of the week to score")
    p.add_argument("--run-date", default=None)
    p.add_argument("--variant", default="baseline", choices=["baseline", "no-guards", "model-decides"])
    p.add_argument("--auto", choices=["approve", "reject"], default=None)
    a = p.parse_args(argv)
    load_env()
    cost = CostTracker()
    g = build_graph(callbacks=[cost])
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}
    state = g.invoke({"week_start": a.week, "run_date": a.run_date, "variant": a.variant}, cfg)
    for line in state.get("log", []):
        print(line)
    if state.get("incident"):
        print("TRACKING INCIDENT:", json.dumps(state["incident"]))
    for o in state.get("outbox", []):
        print(f"\n[{o['channel']} -> {o['to']}]\n{o['text']}")
    if "__interrupt__" in state:
        req = state["__interrupt__"][0].value
        decisions = {}
        for e in req["emails"]:
            print(f"\n--- email for {e['account']} (owner {e['owner']}) ---\nSubject: {e['subject']}\n\n{e['body']}")
            choice = a.auto or {"a": "approve", "e": "edit", "r": "reject"}.get(input("[a]pprove / [e]dit / [r]eject? ").strip()[:1], "reject")
            d = {"action": choice, "by": "@jordan"}
            if choice == "edit":
                d["body"] = input("New body (one line): ")
            decisions[e["account_id"]] = d
        state = g.invoke(Command(resume=decisions), cfg)
        sent = [o for o in state["outbox"] if o["channel"] == "email"]
        print(f"\nSent {len(sent)} approved email(s); wrote {len(state.get('annotations', []))} PostHog annotation(s).")
    print("spend:", cost.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
