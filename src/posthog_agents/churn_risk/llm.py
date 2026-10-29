"""Structured model calls with transient-error retries (one retry layer, here)."""

from __future__ import annotations

import time

from langchain_core.messages import HumanMessage, SystemMessage


def is_transient(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return any(s in name for s in ("ratelimit", "timeout", "connection", "apiconnection", "internalserver")) or \
        any(s in text for s in ("429", "rate limit", "timed out", "502", "503", "504", "overloaded", "connection"))


def structured(model, schema: dict, system: str, user: str, *, config=None, retries: int = 3):
    runnable = model.with_structured_output(schema, method="json_schema", include_raw=True)
    msgs = [SystemMessage(system), HumanMessage(user)]
    parse_fail = 0
    for attempt in range(retries + 1):
        try:
            out = runnable.invoke(msgs, config=config)
        except Exception as exc:  # noqa: BLE001
            if is_transient(exc) and attempt < retries:
                time.sleep(2 * (2 ** attempt)); continue
            raise
        parsed = out.get("parsed") if isinstance(out, dict) else out
        if parsed is not None:
            return parsed
        parse_fail += 1
        if parse_fail > 1:
            raise ValueError(f"model output failed the schema twice: {out.get('parsing_error')}")
    raise RuntimeError("unreachable")
