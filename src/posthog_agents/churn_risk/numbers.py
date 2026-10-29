"""Every number in a drafted message must come from the computed facts.

Strips URLs, the week label, account names and @handles, then checks each remaining number against the facts
(also allowing the common roundings of each fact). Returns the numbers that weren't found.
"""

from __future__ import annotations

import re

NUM = re.compile(r"(?<![\w.])[-+]?\$?\d[\d,]*(?:\.\d+)?%?")
CONSTANTS = {4.0, 14.0, 1.0}   # "4-week average", "last 14 days", "1:1"


def allowed_numbers(facts: dict, extras: tuple = ()) -> set[float]:
    vals: set[float] = set(CONSTANTS)

    def add(v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            for x in (v, abs(v), round(v), round(abs(v)), round(v, 1), round(abs(v), 1)):
                vals.add(float(x))
        elif isinstance(v, dict):
            for x in v.values(): add(x)
        elif isinstance(v, (list, tuple)):
            for x in v: add(x)

    for k, v in facts.items():
        if k not in ("account_id", "name", "owner", "signals"):
            add(v)
    for e in extras:
        add(e)
    return vals


def unsupported_numbers(text: str, facts: dict, strip: tuple[str, ...] = (), extras: tuple = ()) -> list[str]:
    t = re.sub(r"https?://\S+", " ", text)
    for s in sorted([x for x in strip if x], key=len, reverse=True):
        t = t.replace(s, " ")
    t = re.sub(r"@\w+", " ", t)
    t = re.sub(r"\b[\w.-]+_u\d+\b", " ", t)        # user ids like acct_x_u0
    ok = allowed_numbers(facts, extras)
    bad = []
    for m in NUM.finditer(t):
        raw = m.group(0)
        v = float(raw.replace("$", "").replace(",", "").replace("%", "").replace("+", ""))
        if v not in ok and abs(v) not in ok:
            bad.append(raw)
    return bad
