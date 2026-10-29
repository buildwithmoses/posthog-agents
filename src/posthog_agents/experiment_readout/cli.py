"""uv run python -m posthog_agents.experiment_readout.cli <experiment_id> [--variant v] [--approve|--reject] [--approver rina]

Prints the readout. If the decision concludes the experiment, asks for approval (or takes --approve/--reject).
Needs the mock (PHA_MOCK_PORT=8015 uv run posthog-agents mock, POSTHOG_HOST=http://127.0.0.1:8015) or a real PostHog.
"""

from __future__ import annotations

import argparse
import json
import uuid

from ..core.config import load_env
from ..core.cost import CostTracker
from .graph import VARIANTS, build_graph, resume, run_until_approval
from .mock_data import NOW


def main(argv: list[str] | None = None) -> int:
    load_env()
    p = argparse.ArgumentParser()
    p.add_argument("experiment_id", type=int)
    p.add_argument("--variant", default="baseline", choices=VARIANTS)
    p.add_argument("--now", default=NOW)
    p.add_argument("--approve", action="store_true")
    p.add_argument("--reject", action="store_true")
    p.add_argument("--approver", default="rina")
    a = p.parse_args(argv)
    cost = CostTracker()
    g = build_graph(variant=a.variant)
    thread = str(uuid.uuid4())
    state, ask = run_until_approval(g, a.experiment_id, a.now, thread, [cost])
    r = state["readout"]
    print(f"\n# {r['headline']}\n\n{r['summary']}\n")
    for d in r["details"]:
        print(f"- {d}")
    print(f"\nNext: {r['next_step']}\n(code decision: {state['decision']['decision']} {state['decision'].get('variant') or ''}; "
          f"template used: {state.get('used_template')})")
    if ask:
        if a.approve or a.reject:
            answer = {"approved": a.approve and not a.reject, "approver": a.approver}
        else:
            yn = input(f"\n{ask['ask']} [y/N] ").strip().lower()
            answer = {"approved": yn == "y", "approver": a.approver}
        final = resume(g, thread, answer, [cost])
        print("concluded:", json.dumps(final.get("concluded")))
    print("spend:", json.dumps(cost.summary()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
