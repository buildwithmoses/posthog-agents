"""uv run python -m posthog_agents.llm_watch.cli run --as-of "2026-07-08 00:00:00" [--hours 24] [--variant baseline]
                                                     [--approve theo | --reject]
   uv run python -m posthog_agents.llm_watch.cli scenarios"""

from __future__ import annotations

import argparse
import json
import uuid

from langgraph.types import Command

from ..core.cost import CostTracker
from . import mock_data
from .graph import build_graph


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="llm-watch")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--as-of", required=True)
    r.add_argument("--hours", type=int, default=24)
    r.add_argument("--variant", default="baseline", choices=["baseline", "no-min-volume", "model-decides"])
    r.add_argument("--approve", metavar="APPROVER")
    r.add_argument("--reject", action="store_true")
    sub.add_parser("scenarios")
    a = p.parse_args(argv)
    if a.cmd == "scenarios":
        for i, s in enumerate(mock_data.SCENARIOS):
            print(f"{s['id']:20s} as_of={mock_data.as_of(i)}  window={s['window_hours']}h  {s['notes']}")
        return 0
    cost = CostTracker()
    g = build_graph()
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}, "callbacks": [cost]}
    out = g.invoke({"as_of": a.as_of, "hours": a.hours, "variant": a.variant}, cfg)
    print(out["alert"], "\n")
    print("incidents:", json.dumps([(i["kind"], i["cause"]) for i in out["analysis"]["incidents"]]))
    if "__interrupt__" in out:
        ask = out["__interrupt__"][0].value
        print("PROPOSED:", ask["reason"], "(flag", ask["flag"] + ")")
        if a.approve or a.reject:
            out = g.invoke(Command(resume={"approved": bool(a.approve) and not a.reject, "approver": a.approve or ""}), cfg)
            print("applied:", out.get("applied"))
        else:
            print("Not applied. Re-run with --approve theo or --reject.")
    print("spend:", cost.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
