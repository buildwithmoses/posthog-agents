"""The readout the product team reads: model-written from the facts sheet, then checked in code.

`check_readout` passes only if (1) the stated decision and variant equal the code's decision, and (2) every
number in the text appears in the facts sheet, at whatever precision the model rounded to.
`template_readout` is the deterministic fallback when the model fails the check twice.
"""

from __future__ import annotations

import json
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, Field

DECISIONS = ("ship", "keep_control", "keep_running", "invalid")


class Readout(BaseModel):
    headline: str = Field(description="One line, plain English, states the decision.")
    decision: Literal["ship", "keep_control", "keep_running", "invalid"]
    variant: str | None = Field(description="The variant to roll out for ship, 'control' for keep_control, else null.")
    summary: str = Field(description="3 to 5 plain sentences for a product team: what was tested, what happened, why this decision.")
    details: list[str] = Field(description="3 to 6 short bullet points with the key numbers.")
    next_step: str = Field(description="One sentence: what happens next.")

    def text(self) -> str:
        return "\n".join([self.headline, self.summary, *self.details, self.next_step])


SYSTEM = """You write A/B test readouts for Ledgerly's product team. Ledgerly is an invoicing app.

The statistics and the decision were already computed in code. You only explain them.
- State the decision exactly as given: `decision` must equal facts.decision and `variant` must equal facts.decision_variant.
- Use ONLY the facts JSON. Every number you write must be copied from it (you may round to fewer decimals, e.g.
  "13.00%" -> "13%"). Do not compute new numbers: no differences, no ratios, no counts that are not in the facts.
- No dates, and no numbers in names.
- Plain English for product managers: say "chance of being best", not "posterior". Explain the reason in
  facts.decision_explanation in your own words.
- If the decision is invalid or keep_running, do not call any variant a winner.
- Never recommend anything other than the code's decision."""

NAIVE_SYSTEM = """You write A/B test readouts for Ledgerly's product team and decide what to do.
Read the experiment and the raw counts, decide one of: ship (name the variant), keep_control, keep_running, invalid,
and explain it in plain English with the key numbers."""


_NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> list[str]:
    return [m.group(0) for m in _NUM.finditer(text)]


def allowed_numbers(facts: dict) -> set[float]:
    found: set[float] = {0.0, 100.0}

    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)
        elif isinstance(x, bool) or x is None:
            return
        elif isinstance(x, (int, float)):
            found.add(float(x))
        else:
            for n in numbers_in(str(x)):
                found.add(float(n.replace(",", "")))

    walk(facts)
    variants = facts.get("primary_metric", {}).get("variants", [])
    found.add(float(len(variants)))
    found.add(float(len(facts.get("guardrail_metrics", []))))
    return found


def _decimals(s: str) -> int:
    return len(s.split(".")[1]) if "." in s else 0


def number_ok(token: str, allowed: set[float]) -> bool:
    """True when some allowed value, rounded to the token's precision, equals the token. Sign-insensitive, so
    "a 2.5% drop" matches a lift of -2.5%."""
    x = abs(float(token.replace(",", "")))
    d = _decimals(token)
    q = Decimal(1).scaleb(-d)
    for a in allowed:
        a = abs(a)
        half_up = float(Decimal(repr(a)).quantize(q, rounding=ROUND_HALF_UP))
        if abs(half_up - x) < 1e-9 or abs(round(a, d) - x) < 1e-9:
            return True
    return False


def check_readout(r: Readout, facts: dict) -> list[str]:
    problems = []
    if r.decision != facts["decision"]:
        problems.append(f"decision is {r.decision!r} but the code decided {facts['decision']!r}")
    want_variant = facts.get("decision_variant")
    if (r.variant or None) != (want_variant or None):
        problems.append(f"variant is {r.variant!r} but the code's variant is {want_variant!r}")
    allowed = allowed_numbers(facts)
    bad = sorted({t for t in numbers_in(r.text()) if not number_ok(t, allowed)})
    if bad:
        problems.append("numbers not in the facts: " + ", ".join(bad))
    return problems


def template_readout(facts: dict) -> Readout:
    pm = facts["primary_metric"]
    rows = pm["variants"]
    details = [f"{v['variant']}: {v['converted']:,} of {v['users']:,} users ({v['rate']})"
               + (f", {v['chance_of_being_best']} chance of being best" if v.get("chance_of_being_best") else "")
               for v in rows]
    for g in facts.get("guardrail_metrics", []):
        details.append(f"{g['name']}: " + "; ".join(f"{v['variant']} {v['rate']}" for v in g["variants"]))
    head = {"ship": f"Ship {facts['decision_variant']}", "keep_control": "Keep control",
            "keep_running": "Keep the experiment running", "invalid": "Don't trust this result"}[facts["decision"]]
    return Readout(headline=f"{facts['experiment']}: {head}", decision=facts["decision"], variant=facts.get("decision_variant"),
                   summary=facts["decision_explanation"], details=details[:6],
                   next_step={"ship": f"Roll {facts['decision_variant']} out to everyone after Rina approves.",
                              "keep_control": "Conclude the experiment and keep control after Rina approves.",
                              "keep_running": "Keep it running and check again when it has enough data.",
                              "invalid": "Fix the problem and restart the experiment with fresh users."}[facts["decision"]])


def user_prompt(facts: dict, feedback: list[str] | None = None) -> str:
    msg = "Facts JSON:\n" + json.dumps(facts, indent=2)
    if feedback:
        msg += "\n\nYour previous readout failed these checks; fix them:\n- " + "\n- ".join(feedback)
    return msg


def naive_prompt(data: dict) -> str:
    exp = data["experiment"]
    raw = {"name": exp["name"], "description": exp["description"], "planned_split": data["planned_pct"],
           "runtime_days": round(data["runtime_days"], 1), "still_running": data["running"],
           "users_per_variant": data["users"], "users_who_saw_more_than_one_variant": data["multi_exposed"],
           "metrics": [{"name": m["name"], "goal": m["goal"], "conversions": m["conversions"]} for m in data["metrics"]]}
    return "Experiment:\n" + json.dumps(raw, indent=2)
