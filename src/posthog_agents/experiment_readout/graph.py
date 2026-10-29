"""Experiment readout as a LangGraph graph.

    fetch (HogQL) -> analyze (stats + decision in code) -> write (model) -> check (code) --fail--> write (once more)
                                                                           \\--fail twice--> template
    -> approval [interrupt: Rina approves or rejects, only for ship / keep_control] -> conclude (PATCH, only if approved)

Variants:
  baseline        the above
  no-checks       regression: code decides, but with no SRM, mixed-exposure, guardrail or minimum-lift checks
  model-decides   regression: the model reads raw counts plus the experiment notes, decides, and nothing checks it
"""

from __future__ import annotations

import time
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..core.config import get_model
from ..core.posthog import PostHog
from . import readout as R
from .analysis import analyze, decision_dict
from .queries import fetch

APPROVER = "rina"
VARIANTS = ("baseline", "no-checks", "model-decides")

_TRANSIENT = {"APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError", "TimeoutError",
              "ConnectionError", "ReadTimeout", "ConnectTimeout", "RemoteProtocolError", "ServiceUnavailableError"}


class State(TypedDict, total=False):
    experiment_id: int
    variant: str
    now: str
    data: dict
    facts: dict
    decision: dict
    readout: dict
    attempts: int
    feedback: list[str]
    check_problems: list[str]
    first_try_passed: bool
    used_template: bool
    approval: dict
    concluded: dict | None


def structured(model, system: str, user: str, config=None) -> R.Readout:
    runnable = model.with_structured_output(R.Readout, method="json_schema")
    for attempt in range(4):
        try:
            out = runnable.invoke([SystemMessage(system), HumanMessage(user)], config=config)
            if out is not None:
                return out
        except Exception as exc:  # noqa: BLE001
            names = {c.__name__ for c in type(exc).__mro__}
            status = getattr(exc, "status_code", None)
            if attempt < 3 and (names & _TRANSIENT or (isinstance(status, int) and (status == 429 or status >= 500))):
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise RuntimeError("model returned no readout")


def build_graph(*, ph: PostHog | None = None, model=None, checkpointer=None, variant: str = "baseline"):
    if variant not in VARIANTS:
        raise ValueError(f"variant must be one of {VARIANTS}")
    ph = ph or PostHog("experiment-readout")
    _model = {"m": model}

    def llm():
        if _model["m"] is None:
            _model["m"] = get_model()
        return _model["m"]

    def n_fetch(s: State, config=None) -> State:
        return {"data": fetch(ph, s["experiment_id"], s["now"]), "attempts": 0, "feedback": []}

    def n_analyze(s: State) -> State:
        a = analyze(s["data"], checks_enabled=(variant != "no-checks"))
        return {"facts": a["facts"], "decision": decision_dict(a["decision"])}

    def n_write(s: State, config=None) -> State:
        if variant == "model-decides":
            r = structured(llm(), R.NAIVE_SYSTEM, R.naive_prompt(s["data"]), config)
        else:
            r = structured(llm(), R.SYSTEM, R.user_prompt(s["facts"], s.get("feedback")), config)
        return {"readout": r.model_dump(), "attempts": s.get("attempts", 0) + 1}

    def n_check(s: State) -> State:
        r = R.Readout(**s["readout"])
        if variant == "model-decides":   # the regression trusts the model: its own decision, no number check
            return {"check_problems": [], "first_try_passed": True, "used_template": False,
                    "decision": {**s["decision"], "decision": r.decision, "variant": r.variant, "reason": "model_decided"}}
        problems = R.check_readout(r, s["facts"])
        out: State = {"check_problems": problems}
        if s.get("attempts", 0) == 1:
            out["first_try_passed"] = not problems
        if problems and s.get("attempts", 0) >= 2:
            out["readout"] = R.template_readout(s["facts"]).model_dump()
            out["used_template"] = True
            out["check_problems"] = []
        elif not problems:
            out["used_template"] = False
        else:
            out["feedback"] = problems
        return out

    def after_check(s: State) -> str:
        return "write" if s.get("check_problems") else "approval"

    def n_approval(s: State) -> State:
        d = s["decision"]
        if d["decision"] not in ("ship", "keep_control"):
            return {"approval": {"required": False}}
        answer = interrupt({"type": "conclude_experiment", "experiment_id": s["experiment_id"],
                            "experiment": s["data"]["experiment"]["name"], "decision": d["decision"],
                            "variant": d["variant"], "readout": s["readout"],
                            "ask": f"@{APPROVER}: approve concluding this experiment and rolling out {d['variant']}?"})
        answer = answer if isinstance(answer, dict) else {}
        return {"approval": {"required": True, "approved": answer.get("approved") is True,
                             "approver": str(answer.get("approver", "")).lower().lstrip("@"), "note": answer.get("note", "")}}

    def n_conclude(s: State) -> State:
        ap, d = s.get("approval") or {}, s["decision"]
        if not (ap.get("required") and ap.get("approved") and ap.get("approver") == APPROVER):
            return {"concluded": None}
        exp = s["data"]["experiment"]
        variants = [v["key"] for v in exp["parameters"]["feature_flag_variants"]]
        rollout = [{"key": k, "rollout_percentage": 100 if k == d["variant"] else 0} for k in variants]
        base = f"/api/projects/{ph.project}"
        r1 = ph.http.patch(f"{base}/experiments/{exp['id']}/", json={
            "end_date": exp.get("end_date") or s["now"].replace(" ", "T") + "Z",
            "conclusion": "won" if d["decision"] == "ship" else "lost",
            "conclusion_comment": f"{s['readout']['headline']} Approved by @{APPROVER}."})
        r2 = ph.http.patch(f"{base}/feature_flags/{exp['feature_flag']['id']}/",
                           json={"filters": {"multivariate": {"variants": rollout}}})
        r1.raise_for_status(); r2.raise_for_status()
        return {"concluded": {"experiment_id": exp["id"], "rolled_out": d["variant"], "approver": APPROVER}}

    g = StateGraph(State)
    for name, fn in [("fetch", n_fetch), ("analyze", n_analyze), ("write", n_write), ("check", n_check),
                     ("approval", n_approval), ("conclude", n_conclude)]:
        g.add_node(name, fn)
    g.add_edge(START, "fetch")
    g.add_edge("fetch", "analyze")
    g.add_edge("analyze", "write")
    g.add_edge("write", "check")
    g.add_conditional_edges("check", after_check, {"write": "write", "approval": "approval"})
    g.add_edge("approval", "conclude")
    g.add_edge("conclude", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver())


def run_until_approval(graph, experiment_id: int, now: str, thread: str, callbacks: list | None = None) -> tuple[dict, Any]:
    """Run to the approval interrupt (or the end). Returns (state, interrupt_payload_or_None)."""
    cfg: dict[str, Any] = {"configurable": {"thread_id": thread}}
    if callbacks:
        cfg["callbacks"] = callbacks
    res = graph.invoke({"experiment_id": experiment_id, "now": now}, cfg)
    ints = res.get("__interrupt__") or []
    return res, (ints[0].value if ints else None)


def resume(graph, thread: str, answer: dict, callbacks: list | None = None) -> dict:
    from langgraph.types import Command

    cfg: dict[str, Any] = {"configurable": {"thread_id": thread}}
    if callbacks:
        cfg["callbacks"] = callbacks
    return graph.invoke(Command(resume=answer), cfg)
