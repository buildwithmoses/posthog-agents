"""Code-side number check: every number in the alert must be one the queries produced.

Allowed numbers come from the display strings in the facts (e.g. "$1.80", "4.4x", "9.7%", "16.1s", "5,180"),
plus the window length and baseline days. Model names, providers, trace ids, span names, flag keys and dates are
stripped first so "claude-haiku-5.5" doesn't count as the number 5.5.
"""

from __future__ import annotations

import re

NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


def _walk(x, out: list[str]):
    if isinstance(x, dict):
        if set(x) == {"value", "display"}:
            out.append(str(x["display"]))
            return
        for v in x.values():
            _walk(v, out)
    elif isinstance(x, list):
        for v in x:
            _walk(v, out)
    elif isinstance(x, str):
        out.append(x)


def _vals(text: str) -> set[float]:
    out = set()
    for m in NUM.findall(text):
        try:
            out.add(round(float(m.replace(",", "")), 4))
        except ValueError:
            pass
    return out


def identifiers(analysis: dict) -> list[str]:
    ids = set()
    f = analysis["facts"]
    for g in f["groups"]:
        ids.update([g["model"], g["provider"]])
    ids.update(f.get("model_mix", {}).keys())
    for inc in analysis["incidents"]:
        ids.add(inc["cause"].split(":", 1)[-1])
        for t in inc.get("top_traces", []):
            ids.add(t["trace"])
        for k in ("model", "provider", "span", "top_error"):
            v = inc.get("evidence", {}).get(k)
            if isinstance(v, str):
                ids.add(v)
    for e in analysis["expected_changes"]:
        ids.update([e["model"], e["annotation"]])
    for a in f.get("annotations_in_window", []):
        ids.update([a["content"], a["at"]])
    p = analysis.get("proposal")
    if p:
        ids.update([p["flag_key"], p["reason"]])
    ids.update([f["window"]["start"], f["window"]["end"], "claude-haiku-5.5", "claude-sonnet-5.5", "gpt-5.4-mini"])
    ids.update(re.findall(r"\b\d{3} [a-z_]+\b", " ".join(str(i) for i in ids)))  # "529 overloaded_error"
    return sorted((i for i in ids if i), key=len, reverse=True)


def allowed(analysis: dict) -> set[float]:
    texts: list[str] = []
    _walk(analysis["facts"], texts)
    _walk(analysis["incidents"], texts)
    _walk(analysis["expected_changes"], texts)
    vals = set()
    for t in texts:
        vals |= _vals(t)
    w = analysis["facts"]["window"]["hours"]
    vals |= {float(w), float(analysis["facts"]["baseline_days"]), 1.0, 24.0, 7.0}
    for v in list(vals):  # "9.7%" may be written "9.7 percent"; "$1.80" may be "1.8"
        vals.add(round(v, 1)); vals.add(round(v, 2))
    return vals


def check(text: str, analysis: dict) -> list[str]:
    """Return the numbers in `text` that no query produced (empty list = pass)."""
    t = text
    for ident in identifiers(analysis):
        t = t.replace(ident, " ")
    t = re.sub(r"\b(?:tr|acct|user)_[\w]+", " ", t)
    t = re.sub(r"@\w+", " ", t)
    ok = allowed(analysis)
    bad = []
    for m in NUM.findall(t):
        v = round(float(m.replace(",", "")), 4)
        if v not in ok and round(v, 1) not in ok and round(v, 2) not in ok:
            bad.append(m)
    return bad
