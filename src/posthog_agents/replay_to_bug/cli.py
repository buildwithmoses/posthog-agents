"""uv run python -m posthog_agents.replay_to_bug.cli run REC_ID [--variant naive] [--auto approve|reject]
   uv run python -m posthog_agents.replay_to_bug.cli latest [--days 7]      # recordings with $exception, via HogQL"""

from __future__ import annotations

import argparse
import json

from ..core.cost import CostTracker
from ..core.posthog import PostHog
from .graph import SLUG, build_graph, latest_with_errors, run_one


def ask(payload: dict) -> dict:
    print(json.dumps(payload["draft"], indent=2))
    a = input(f"\n@{payload['approver']}: {payload['action']} this? [a]pprove / [r]eject / [e]dit title: ").strip().lower()
    if a.startswith("e"):
        return {"decision": "edit", "edits": {"title": input("New title: ")}, "approver": "amara"}
    return {"decision": "approve" if a.startswith("a") else "reject", "approver": "amara"}


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("recording_id"); r.add_argument("--variant", default="baseline")
    r.add_argument("--auto", choices=["approve", "reject"])
    lt = sub.add_parser("latest"); lt.add_argument("--days", type=int, default=7)
    a = p.parse_args()
    if a.cmd == "latest":
        print("\n".join(latest_with_errors(PostHog(SLUG), a.days)))
        return
    cost = CostTracker()
    g = build_graph(a.variant, callbacks=[cost])
    decide = (lambda pl: {"decision": a.auto, "approver": "amara"}) if a.auto else ask
    out = run_one(g, a.recording_id, decide)
    print(json.dumps({"category": out.get("category"), "action": out.get("action"), "result": out.get("result"),
                      "attempts": out.get("attempts"), "fallback": out.get("used_fallback"), **cost.summary()}, indent=2))


if __name__ == "__main__":
    main()
