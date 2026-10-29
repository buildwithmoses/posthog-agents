"""Experiment readout evals against the mock PostHog (or a real one with the same fixtures).

    uv run python evals/experiment_readout/run.py [--variant baseline|no-checks|model-decides] [--label x]

Runs all 9 labeled experiments through the graph. A simulated Rina answers every approval: she approves
everything except experiment 2 (rejects). One extra run sends an approval for experiment 1 from the wrong
person (@dana). Concluding writes are read back from the mock's /_log. A GPT-6.1 Terra judge scores clarity.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from posthog_agents.core.config import get_model, judge_model_id, load_env, posthog_host
from posthog_agents.core.cost import CostTracker
from posthog_agents.experiment_readout import readout as R
from posthog_agents.experiment_readout.analysis import analyze
from posthog_agents.experiment_readout.graph import build_graph, resume, run_until_approval
from posthog_agents.experiment_readout.mock_data import NOW

HERE = Path(__file__).resolve().parent
LABELS = {int(k): v for k, v in json.loads((HERE / "labels.json").read_text()).items() if not k.startswith("_")}
REJECT = {2}


class Clarity(BaseModel):
    score: int = Field(ge=1, le=5, description="1 = confusing or misleading, 5 = a PM can act on it immediately")
    reason: str


JUDGE = """You grade A/B test readouts written for a product team at an invoicing company.
Score 1-5 for clarity: is the decision obvious from the first line, is the reason explained in plain English a
product manager understands, are the key numbers there without jargon, and does nothing in it contradict the
decision. You are not grading whether the decision is right. Return a score and one sentence."""


def writes_for(log: list[dict], start: int, eid: int, flag_id: int) -> list[dict]:
    return [w for w in log[start:] if w["path"] in (f"/experiments/{eid}", f"/feature_flags/{flag_id}")]


def main() -> int:
    load_env()
    p = argparse.ArgumentParser()
    p.add_argument("--variant", default="baseline")
    p.add_argument("--label", default=None)
    p.add_argument("--no-judge", action="store_true")
    a = p.parse_args()
    label = a.label or a.variant
    host = posthog_host()
    http = httpx.Client(base_url=host, timeout=120)
    log = lambda: http.get("/_log", params={"project": 105}).json()  # noqa: E731

    agent_cost, judge_cost = CostTracker(), CostTracker()
    graph = build_graph(variant=a.variant)
    judge = get_model(judge_model_id()).with_structured_output(Clarity, method="json_schema")
    runs = [(eid, "rina", eid not in REJECT) for eid in sorted(LABELS)] + [(1, "dana", True)]
    rows = []
    t0 = time.time()
    for eid, approver, approve in runs:
        before = len(log())
        thread = str(uuid.uuid4())
        ts = time.time()
        state, ask = run_until_approval(graph, eid, NOW, thread, [agent_cost])
        if ask:
            state = resume(graph, thread, {"approved": approve, "approver": approver}, [agent_cost])
        exp = state["data"]["experiment"]
        writes = writes_for(log(), before, eid, exp["feature_flag"]["id"])
        truth = analyze(state["data"])                       # the full-check facts, whatever the variant
        r = R.Readout(**state["readout"])
        problems = R.check_readout(r, {**truth["facts"], "decision": state["decision"]["decision"],
                                       "decision_variant": state["decision"].get("variant")})
        label_row = LABELS[eid]
        decided = state["decision"]["decision"]
        variant = state["decision"].get("variant")
        authorized = bool(ask) and approve and approver == "rina"
        row = {
            "experiment": eid, "name": exp["name"], "approver": approver, "approved": approve,
            "expected": {"decision": label_row["decision"], "variant": label_row["variant"]},
            "decided": {"decision": decided, "variant": variant, "reason": state["decision"].get("reason")},
            "decision_correct": decided == label_row["decision"] and (variant or None) == (label_row["variant"] or None),
            "asked_for_approval": bool(ask), "writes": len(writes),
            "approval_respected": (len(writes) == 2) if authorized else (len(writes) == 0),
            "number_problems": [x for x in problems if x.startswith("numbers")],
            "first_try_passed": state.get("first_try_passed"), "used_template": state.get("used_template", False),
            "seconds": round(time.time() - ts, 1), "readout": state["readout"],
        }
        if not a.no_judge and approver == "rina":
            for attempt in range(3):
                try:
                    j = judge.invoke([("system", JUDGE), ("human", r.text())], {"callbacks": [judge_cost]})
                    row["clarity"] = {"score": j.score, "reason": j.reason}
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt == 2:
                        row["clarity"] = {"score": None, "reason": f"judge error: {e}"}
                    time.sleep(3)
        rows.append(row)
        flag = "ok " if row["decision_correct"] and row["approval_respected"] and not row["number_problems"] else "BAD"
        print(f"{flag} {eid} {approver:5} expected {label_row['decision']:<12} got {decided:<12} {str(variant):<10} "
              f"writes={len(writes)} numbers={'ok' if not row['number_problems'] else row['number_problems']}"
              f"{'' if 'clarity' not in row else ' clarity=' + str(row['clarity']['score'])}", flush=True)

    main_rows = [r for r in rows if r["approver"] == "rina"]
    invalid = [r for r in main_rows if r["expected"]["decision"] == "invalid"]
    scores = [r["clarity"]["score"] for r in main_rows if r.get("clarity", {}).get("score")]
    metrics = {
        "decision_correct": sum(r["decision_correct"] for r in main_rows) / len(main_rows),
        "invalid_caught": sum(r["decided"]["decision"] == "invalid" for r in invalid) / len(invalid),
        "numbers_match": sum(not r["number_problems"] for r in rows) / len(rows),
        "approval_respected": sum(r["approval_respected"] for r in rows) / len(rows),
        "clarity": round(statistics.mean(scores) / 5, 3) if scores else None,
        "first_try_passed": sum(bool(r["first_try_passed"]) for r in main_rows) / len(main_rows),
        "template_used": sum(bool(r["used_template"]) for r in main_rows) / len(main_rows),
    }
    out = {"label": label, "variant": a.variant, "at": datetime.now().isoformat(timespec="seconds"),
           "posthog": host, "metrics": metrics, "agent_spend": agent_cost.summary(), "judge_spend": judge_cost.summary(),
           "wall_seconds": round(time.time() - t0, 1), "runs": rows}
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    (HERE / "results" / f"{label}-{stamp}.json").write_text(json.dumps(out, indent=2))
    (HERE / "results" / f"latest-{a.variant}.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({"metrics": metrics, "agent_spend": out["agent_spend"], "judge_spend": out["judge_spend"],
                      "wall_seconds": out["wall_seconds"]}, indent=2))
    print(f"wrote evals/experiment_readout/results/{label}-{stamp}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
