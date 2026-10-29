"""Code side of the agent: turn a recording's snapshots into a scrubbed, numbered timeline, find the failure,
classify it and compute a signature for dedupe. Nothing here calls a model.

Categories (classify):
  frontend_exception    an exception in the app with no failing request behind it
  asset_load_error      a JS chunk or static asset failed to load (typically a stale tab after a deploy)
  api_error             the app's own API returned 5xx
  timeout               the app's API returned 504 or took >= TIMEOUT_MS
  client_request_error  the app's API returned a 4xx that is not a validation message shown to the user
  validation_working    4xx validation error that the user saw (ui_message) -> not a bug
  third_party_outage    only a third-party host failed (payment provider etc.) -> not a bug for engineering
  no_error              nothing failed -> not a bug
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit

APP_HOSTS = {"app.ledgerly.example", "api.ledgerly.example"}
TIMEOUT_MS = 15_000
NOT_A_BUG = {"validation_working", "third_party_outage", "no_error"}
MAX_STEPS = 12

# ---------- PII scrubbing ----------
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}")
SECRET = re.compile(r"\b(?:sk|pk|rk|phx|phc)_(?:live|test)?_?[A-Za-z0-9]{8,}\b")
KV_SECRET = re.compile(r"(?i)\b(token|api_key|apikey|password|secret|authorization|bearer)([=: ]+)([^\s&'\"]+)")
PHONE = re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)")


def scrub(text: str) -> str:
    if not text:
        return text
    text = JWT.sub("[token]", text)
    text = KV_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
    text = SECRET.sub("[secret]", text)
    text = EMAIL.sub("[email]", text)
    text = CARD.sub("[card]", text)
    text = PHONE.sub("[phone]", text)
    return text


def find_pii(text: str) -> list[str]:
    """What scrub() would remove. Used by the verifier and the evals."""
    hits = []
    for rx in (JWT, SECRET, EMAIL, CARD, PHONE):
        hits += rx.findall(text or "")
    hits += [m.group(0) for m in KV_SECRET.finditer(text or "") if m.group(3) not in ("[redacted]",)]
    return [h for h in hits if h and "[" not in h]


def clean_url(url: str) -> str:
    """Path plus query keys with values removed (query values carry emails and tokens)."""
    p = urlsplit(url)
    host = p.netloc
    path = scrub(p.path) or "/"
    q = "&".join(f"{kv.split('=')[0]}=…" for kv in p.query.split("&") if kv) if p.query else ""
    shown = path if (not host or host in APP_HOSTS) else f"{host}{path}"
    return shown + (f"?{q}" if q else "")


def is_app(url: str) -> bool:
    h = urlsplit(url).netloc
    return not h or h in APP_HOSTS


ID_SEG = re.compile(r"^(?:(?:inv|cus|pay|usr|req)_[A-Za-z0-9]+|\d+|[0-9a-f]{8}-[0-9a-f-]{27,})$")


def norm_path(url: str) -> str:
    segs = re.sub(r"\.[0-9a-f]{6,}\.", ".<hash>.", urlsplit(url).path).split("/")
    return "/".join(":id" if ID_SEG.match(s) else s for s in segs)


def mmss(ms: int) -> str:
    s = int(ms // 1000)
    return f"{s // 60:02d}:{s % 60:02d}"


# ---------- timeline ----------

def build(snapshots: list[dict]) -> dict:
    """Returns {"steps", "evidence", "failures", "primary", "omitted_steps", "corpus"} — all scrubbed."""
    steps, evidence, failures = [], [], []
    for e in sorted(snapshots, key=lambda x: x["t"]):
        t, kind = mmss(e["t"]), e["type"]
        if kind == "nav":
            steps.append({"t": t, "ms": e["t"], "text": f"Opened {clean_url(e['url'])}"})
        elif kind == "click":
            label = scrub(e.get("text") or "")
            steps.append({"t": t, "ms": e["t"], "text": f"Clicked {label + ' ' if label else ''}({e['selector']})".replace("  ", " ")})
        elif kind == "input":
            steps.append({"t": t, "ms": e["t"], "text": f"Typed into {e['selector']} (value hidden)"})
        elif kind == "network":
            failed = e["status"] >= 400 or e["duration_ms"] >= TIMEOUT_MS
            if failed:
                body = e.get("body") or {}
                detail = f"{e['method']} {clean_url(e['url'])} -> {e['status']} in {e['duration_ms']} ms"
                msg = body.get("message") or body.get("error")
                if msg:
                    detail += f" ({scrub(str(msg))})"
                item = {"t": t, "ms": e["t"], "kind": "network", "detail": detail, "status": e["status"],
                        "method": e["method"], "path": clean_url(e["url"]).split("?")[0], "app": is_app(e["url"]),
                        "validation": body.get("error_type") == "validation", "slow": e["duration_ms"] >= TIMEOUT_MS,
                        "asset": "/static/" in e["url"], "raw_url": e["url"]}
                evidence.append(item); failures.append(item)
        elif kind == "exception":
            frame = (e.get("stack") or [""])[0]
            item = {"t": t, "ms": e["t"], "kind": "exception", "detail": scrub(f"{e['error_type']}: {e['message']} {frame}".strip()),
                    "error_type": e["error_type"], "message": scrub(e["message"]), "frame": scrub(frame)}
            evidence.append(item); failures.append(item)
        elif kind == "console" and e["level"] in ("error", "warn"):
            evidence.append({"t": t, "ms": e["t"], "kind": "console", "detail": f"console.{e['level']}: {scrub(e['message'])}"})
        elif kind == "rage_click":
            evidence.append({"t": t, "ms": e["t"], "kind": "rage_click", "detail": f"Rage click x{e['count']} on {e['selector']}"})
        elif kind == "ui_message":
            evidence.append({"t": t, "ms": e["t"], "kind": "ui_message", "detail": f"User saw: \"{scrub(e['text'])}\""})
    for i, s in enumerate(steps, 1):
        s["id"] = f"S{i}"
    for i, ev in enumerate(evidence, 1):
        ev["id"] = f"E{i}"
    primary = _primary(failures, evidence)
    omitted = 0
    if primary is not None and len([s for s in steps if s["ms"] <= primary["ms"]]) > MAX_STEPS:
        before = [s for s in steps if s["ms"] <= primary["ms"]]
        omitted = len(before) - MAX_STEPS
        keep = {s["id"] for s in before[-MAX_STEPS:]} | {s["id"] for s in steps if s["ms"] > primary["ms"]}
        steps = [s for s in steps if s["id"] in keep]
    tl = {"steps": steps, "evidence": evidence, "failures": failures, "primary": primary, "omitted_steps": omitted}
    tl["corpus"] = corpus(tl)
    return tl


def _ui_after(evidence, ms, window=3000):
    return any(e["kind"] == "ui_message" and 0 <= e["ms"] - ms <= window for e in evidence)


def _primary(failures, evidence):
    """The first failure that is the app's fault, else the first failure of any kind."""
    for f in failures:
        if f["kind"] == "exception":
            return f
        if f["app"] and (f["status"] >= 500 or f["slow"] or f["asset"]):
            return f
        if f["app"] and 400 <= f["status"] < 500 and not (f["validation"] and _ui_after(evidence, f["ms"])):
            return f
    return failures[0] if failures else None


def classify(tl: dict) -> str:
    p = tl["primary"]
    if p is None:
        return "no_error"
    if p["kind"] == "exception":
        if p["error_type"] == "ChunkLoadError" or any(f.get("asset") for f in tl["failures"]):
            return "asset_load_error"
        return "frontend_exception"
    if not p["app"]:
        return "third_party_outage"
    if p["asset"]:
        return "asset_load_error"
    if p["status"] == 504 or p["slow"]:
        return "timeout"
    if p["status"] >= 500:
        return "api_error"
    if p["validation"] and _ui_after(tl["evidence"], p["ms"]):
        return "validation_working"
    return "client_request_error"


NUM = re.compile(r"\b\d+\b")


def signature(tl: dict) -> str | None:
    p = tl["primary"]
    if p is None:
        return None
    if p["kind"] == "exception":
        file = re.sub(r":\d+:\d+\)?$", "", p["frame"].split("(")[-1]) if "(" in p["frame"] else ""
        msg = p["message"] if p["error_type"] != "ChunkLoadError" else NUM.sub("N", p["message"].split(" (")[0])
        return f"exc:{p['error_type']}: {msg}" + (f" @ {file}" if file else "")
    path = norm_path(p["raw_url"])
    host = "" if p["app"] else urlsplit(p["raw_url"]).netloc
    return f"net:{p['method']} {host}{path} {p['status']}"


def corpus(tl: dict) -> str:
    """Everything a report may cite, as one lowercase string."""
    parts = [s["text"] for s in tl["steps"]] + [e["detail"] for e in tl["evidence"]]
    parts += [e.get("path", "") for e in tl["evidence"]]
    return "\n".join(parts).lower()


def for_model(tl: dict, meta: dict) -> str:
    """The only view of the session the model gets: scrubbed, numbered, no console.log, no raw values."""
    lines = [f"Recording {meta['id']} ({meta.get('recording_duration', '?')} s). Environment: "
             + ", ".join(f"{k} {v}" for k, v in (meta.get("environment") or {}).items())]
    if tl["omitted_steps"]:
        lines.append(f"({tl['omitted_steps']} earlier steps omitted)")
    lines.append("STEPS")
    lines += [f"{s['id']} [{s['t']}] {s['text']}" for s in tl["steps"]]
    lines.append("EVIDENCE")
    lines += [f"{e['id']} [{e['t']}] {e['kind']}: {e['detail']}" for e in tl["evidence"]]
    if tl["primary"]:
        lines.append(f"PRIMARY FAILURE: {tl['primary']['id']}")
    return "\n".join(lines)


def short_hash(sig: str) -> str:
    return hashlib.sha1(sig.encode()).hexdigest()[:8]
