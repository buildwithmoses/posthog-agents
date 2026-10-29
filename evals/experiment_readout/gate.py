"""Gate for the experiment readout. Exit 1 blocks the change.

    uv run python evals/experiment_readout/gate.py [results.json] [--variant v] [--save-baseline]

Hard floors (a single miss fails): the decision matches the label on every experiment, every invalid experiment is
caught, every number in every readout is in the computed stats, and nothing is concluded without Rina's approval.
Clarity (judge) must stay at or above 0.7 and not drop more than 0.1 below the saved baseline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from posthog_agents.core.gate import check, load_baseline, report

HERE = Path(__file__).resolve().parent
FLOORS = {"decision_correct": 1.0, "invalid_caught": 1.0, "numbers_match": 1.0, "approval_respected": 1.0, "clarity": 0.7}


def main(argv: list[str]) -> int:
    variant = argv[argv.index("--variant") + 1] if "--variant" in argv else "baseline"
    paths = [x for x in argv if x.endswith(".json")]
    path = Path(paths[0]) if paths else HERE / "results" / f"latest-{variant}.json"
    r = json.loads(path.read_text())
    m = {k: v for k, v in r["metrics"].items() if v is not None}
    base = load_baseline(HERE / "baseline.json")
    print(f"experiment-readout gate: {r['label']} ({r['variant']}, {len(r['runs'])} runs)")
    for k, v in r["metrics"].items():
        floor = FLOORS.get(k)
        status = "info" if floor is None else ("ok  " if v is not None and v >= floor else "FAIL")
        print(f"  {status} {k:<20} {v}" + (f"  (floor {floor})" if floor is not None else ""))
    print(f"  spend: agent ${r['agent_spend']['cost_usd']:.4f}, judge ${r['judge_spend']['cost_usd']:.4f}, {r['wall_seconds']}s")
    fails = check(m, FLOORS, {"clarity": base["metrics"]["clarity"]} if base and base["metrics"].get("clarity") else None, max_drop=0.1)
    code = report(fails, r["label"])
    if "--save-baseline" in argv:
        if code:
            print("not saving a failing run as the baseline")
            return code
        (HERE / "baseline.json").write_text(json.dumps({"from": path.name, "metrics": r["metrics"]}, indent=2) + "\n")
        print("saved evals/experiment_readout/baseline.json")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
