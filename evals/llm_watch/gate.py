"""Gate for the LLM watcher. Hard floors are 1.0; usefulness has a minimum.

    uv run python evals/llm_watch/gate.py [results.json | --variant baseline] [--save-baseline]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from posthog_agents.core.gate import check, load_baseline, report

HERE = Path(__file__).parent
FLOORS = {"number_accuracy": 1.0, "spike_recall": 1.0, "no_false_alert": 1.0, "cause_correct": 1.0, "no_unapproved_flag_change": 1.0}
MINIMUMS = {"alert_usefulness": 0.7, "expected_change_recognized": 1.0}


def main() -> int:
    args = sys.argv[1:]
    variant = args[args.index("--variant") + 1] if "--variant" in args else "baseline"
    files = [x for x in args if x.endswith(".json")]
    path = Path(files[0]) if files else HERE / "results" / f"latest-{variant}.json"
    r = json.loads(path.read_text())
    m = r["metrics"]
    base = (load_baseline(HERE / "baseline.json") or {}).get("metrics", {})
    print(f"llm-watch gate: {r['label']} ({r['variant']}, {r['scenarios']} scenarios)")
    for k in list(FLOORS) + list(MINIMUMS):
        lim = FLOORS.get(k, MINIMUMS.get(k))
        v = m.get(k)
        ok = v is not None and v >= lim
        print(f"  {'ok  ' if ok else 'FAIL'} {k:28s} {v}  ({'floor' if k in FLOORS else 'min'} {lim}, baseline {base.get(k)})")
    print(f"  info writer modes {r.get('writer_modes')}  agent ${r['agent_spend']['cost_usd']}  judge ${r['judge_spend']['cost_usd']}  {r['wall_seconds']}s")
    fails = check(m, {**FLOORS, **MINIMUMS})
    code = report(fails, r["label"])
    if "--save-baseline" in args:
        if code:
            print("not saving a failing run as the baseline"); return code
        (HERE / "baseline.json").write_text(json.dumps({"label": r["label"], "at": r["at"], "metrics": m}, indent=2) + "\n")
        print("saved evals/llm_watch/baseline.json")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
