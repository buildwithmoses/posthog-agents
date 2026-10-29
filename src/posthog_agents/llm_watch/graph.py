"""LLM watcher graph (LangGraph).

collect (HogQL) -> analyze (code) -> write (model, from facts only; code number check, one retry, then a template)
  -> approve (interrupt: @theo approves or rejects the proposed flag change) -> apply (PATCH only if approved)

Variants:
  baseline       as above
  no-min-volume  same, but the minimum-volume rules are off (regression)
  model-decides  the model reads the aggregated rows and decides incidents and causes itself; no number check (regression)
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from ..core.config import get_model
from ..core.posthog import PostHog
from . import analyze as A
from . import numbers, queries

APPROVER = "theo"

WRITER_SYSTEM = """You write Ledgerly's LLM-cost and reliability alert for Theo (Head of AI) about the "Draft with AI" feature.
You get FACTS computed by code from PostHog. Rules:
- Use only numbers that appear in FACTS, written exactly as their display strings ("$1.80", "4.4x", "9.7%", "16.1s", "5,180").
  Never compute, round, add or estimate a number yourself. If you want a number that isn't there, describe it in words.
- Lead with what happened and how big, then the likely cause with its evidence (span, provider, error message, traces),
  then the proposed fix if there is one (it needs Theo's approval; never say it has been applied).
- Expected changes (annotated on purpose) are not incidents: mention them in one line as expected.
- Only `expected_changes` are expected changes. Other annotations in the window are context: mention one only if it
  plausibly relates to an incident, and never call it an expected change.
- Mention approval only when there is a proposed fix. Don't invent next steps that need approval.
- If there are no incidents, write one or two lines saying the window looks normal.
- Plain text, short sentences, at most 110 words. No headings, no emoji."""


def display_only(x: Any) -> Any:
    if isinstance(x, dict):
        if set(x) == {"value", "display"}:
            return x["display"]
        return {k: display_only(v) for k, v in x.items() if k != "has_baseline"}
    if isinstance(x, list):
        return [display_only(v) for v in x]
    return x


def template(a: dict) -> str:
    """Fallback when the model's text fails the number check twice: built from facts, nothing invented."""
    f = a["facts"]
    lines = []
    if not a["incidents"]:
        lines.append(f"Draft with AI looks normal for the last {f['window']['hours']} hours: {f['totals']['calls']['display']} calls, "
                     f"{f['totals']['cost']['display']} spent ({f['totals']['cost_ratio']['display']} of the usual).")
    for i in a["incidents"]:
        ev = display_only(i.get("evidence", {}))
        lines.append(f"{i['kind'].replace('_', ' ')}: cause {i['cause']}. Evidence: " + ", ".join(f"{k} {v}" for k, v in ev.items()) + ".")
    for e in a["expected_changes"]:
        lines.append(f"Expected: {e['model']} switch annotated by {e['by']} (cost {e['cost_ratio']['display']} of usual).")
    if a.get("proposal"):
        lines.append(f"Proposed (needs @theo): {a['proposal']['reason']}")
    return "\n".join(lines)


class Decided(BaseModel):
    """Regression variant: the model's own judgement."""
    class Incident(BaseModel):
        kind: str = Field(description="One of: cost_spike, latency_spike, error_spike, cost_per_accepted")
        cause: str = Field(description="One of: prompt_bloat:<span>, provider_latency:<provider>, error_cluster:<error message>, model_switch:<model>, acceptance_drop, volume, unknown")
    incidents: list[Incident]
    expected_changes: list[str] = Field(description="e.g. model_switch:<model> for changes an annotation says were intended")
    alert: str


class S(TypedDict, total=False):
    as_of: str
    hours: int
    variant: str
    raw: dict
    analysis: dict
    alert: str
    writer: dict
    decision: dict
    applied: dict | None


def build_graph(ph: PostHog | None = None, model=None, checkpointer=None):
    ph = ph or PostHog("llm-watch")

    def collect(s: S) -> S:
        return {"raw": queries.collect(ph, datetime.fromisoformat(s["as_of"]), s["hours"])}

    def analyze_node(s: S) -> S:
        rules = A.NO_MIN if s.get("variant") == "no-min-volume" else A.RULES
        return {"analysis": A.analyze(s["raw"], rules)}

    def write(s: S) -> S:
        llm = model or get_model(max_tokens=1200)
        a = s["analysis"]
        if s.get("variant") == "model-decides":
            raw = s["raw"]
            payload = {"window": raw["window"], "current": raw["current"], "baseline_windows": raw["baseline"],
                       "annotations": raw["annotations"]}
            msg = [SystemMessage("You watch Ledgerly's LLM feature in PostHog. Decide from the raw query rows whether anything is "
                                 "wrong in the current window compared with the baseline windows, name the cause, and write a short alert for Theo."),
                   HumanMessage(json.dumps(payload, default=str)[:60000])]
            out: Decided | None = None
            for _ in range(2):  # a malformed answer gets one retry; a second failure counts as "no incidents found"
                try:
                    out = llm.with_structured_output(Decided, method="json_schema").invoke(msg)
                    break
                except Exception:  # noqa: BLE001
                    continue
            if out is None:
                out = Decided(incidents=[], expected_changes=[], alert="(the model returned no valid decision)")
            inc = [{"kind": i.kind, "cause": i.cause, "evidence": {}} for i in out.incidents]
            a = {**a, "incidents": inc, "expected_changes": [{"kind": "model_switch", "model": e.split(":", 1)[-1], "annotation": "", "by": None, "cost_ratio": {"value": 0, "display": ""}} for e in out.expected_changes]}
            a["proposal"] = A.propose(inc, raw.get("flags", [])) if all(i.get("evidence") for i in inc) else None
            return {"analysis": a, "alert": out.alert, "writer": {"mode": "model_decides", "bad_numbers": numbers.check(out.alert, a)}}
        facts = json.dumps({"facts": display_only(a["facts"]), "incidents": display_only(a["incidents"]),
                            "expected_changes": display_only(a["expected_changes"]),
                            "proposed_fix": a["proposal"]["reason"] if a.get("proposal") else None}, default=str)
        msgs = [SystemMessage(WRITER_SYSTEM), HumanMessage(f"FACTS:\n{facts}")]
        tries = []
        for attempt in range(2):
            text = llm.invoke(msgs).content
            text = text if isinstance(text, str) else "".join(p.get("text", "") for p in text if isinstance(p, dict))
            bad = numbers.check(text, a)
            tries.append({"text": text, "bad_numbers": bad})
            if not bad:
                return {"alert": text.strip(), "writer": {"mode": "first_pass" if attempt == 0 else "retry", "tries": tries}}
            msgs += [HumanMessage(f"These numbers are not in FACTS: {bad}. Rewrite using only the display strings in FACTS.")]
        return {"alert": template(a), "writer": {"mode": "template", "tries": tries}}

    def approve(s: S) -> S:
        p = s["analysis"].get("proposal")
        if not p:
            return {"decision": {"approved": False, "reason": "no proposal"}}
        answer = interrupt({"ask": f"@{APPROVER}: apply this flag change?", "flag": p["flag_key"], "reason": p["reason"], "patch": p["patch"]})
        answer = answer if isinstance(answer, dict) else {"approved": bool(answer)}
        return {"decision": answer}

    def apply(s: S) -> S:
        p, d = s["analysis"].get("proposal"), s.get("decision") or {}
        # Code enforces who may approve; the model has no path to this call.
        if not p or not d.get("approved") or str(d.get("approver", "")).lstrip("@").lower() != APPROVER:
            return {"applied": None}
        r = ph.http.patch(f"/api/projects/{ph.project}/feature_flags/{p['flag_id']}/", json=p["patch"])
        r.raise_for_status()
        return {"applied": {"flag": p["flag_key"], "by": APPROVER}}

    g = StateGraph(S)
    for name, fn in [("collect", collect), ("analyze", analyze_node), ("write", write), ("approve", approve), ("apply", apply)]:
        g.add_node(name, fn)
    g.add_edge(START, "collect"); g.add_edge("collect", "analyze"); g.add_edge("analyze", "write")
    g.add_edge("write", "approve"); g.add_edge("approve", "apply"); g.add_edge("apply", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver())
