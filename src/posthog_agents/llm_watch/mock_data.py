"""Fictional PostHog data for the LLM watcher (project 104): Ledgerly's "Draft with AI" feature.

Eight labeled scenarios, each in its own 30-day block so trailing baselines never overlap. A scenario is
"run the watcher for the window ending at as_of": 7 normal days, then the window under test.
Everything is generated from a fixed seed, so every run sees identical events.

Per draft request: one `ai_draft_requested`, 1–2 `$ai_generation` calls (span `line_items` or
`reminder_email`), then `ai_draft_accepted` or `ai_draft_rejected`.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

BASE = datetime(2026, 4, 1)
PRICES = {  # USD per million tokens (fictional)
    "claude-haiku-5.5": (1.0, 5.0),
    "claude-sonnet-5.5": (3.0, 15.0),
    "gpt-5.4-mini": (0.4, 1.6),
}
NORMAL = {"requests_per_day": 300, "accept": 0.62, "err": 0.005, "bedrock_share": 0.3,
          "in_tokens": {"line_items": 1150, "reminder_email": 1300}, "out_tokens": {"line_items": 320, "reminder_email": 260},
          "lat": {"anthropic": 1.6, "amazon-bedrock": 1.8}}

# Hour-of-day weights: busy business hours, quiet nights (US Central-ish).
HOUR_W = [0.15, 0.1, 0.1, 0.1, 0.15, 0.3, 0.6, 1.0, 1.6, 2.0, 2.1, 2.0, 1.8, 2.0, 2.1, 2.0, 1.7, 1.3, 0.9, 0.7, 0.5, 0.4, 0.3, 0.2]

SCENARIOS = [
    {"id": "normal", "window_hours": 24, "label": {"incidents": [], "expected_changes": []},
     "notes": "An ordinary day. An unrelated annotation (new invoice template) must not change anything."},
    {"id": "prompt_bloat", "window_hours": 24,
     "label": {"incidents": [{"kind": "cost_spike", "cause": "prompt_bloat:reminder_email"}], "expected_changes": []},
     "notes": "A prompt change stuffed the customer's full payment history into reminder_email: input tokens 1,300 -> ~5,200."},
    {"id": "provider_latency", "window_hours": 24,
     "label": {"incidents": [{"kind": "latency_spike", "cause": "provider_latency:amazon-bedrock"}], "expected_changes": []},
     "notes": "Bedrock-hosted Haiku slows down; Anthropic-hosted is fine."},
    {"id": "error_spike", "window_hours": 24,
     "label": {"incidents": [{"kind": "error_spike", "cause": "error_cluster:529 overloaded_error"}], "expected_changes": []},
     "notes": "Anthropic returns 529 overloaded on ~10% of its calls; one message dominates."},
    {"id": "overnight_noise", "window_hours": 1, "label": {"incidents": [], "expected_changes": []},
     "notes": "The 03:00-04:00 window: 3 calls, one error and one slow call. Too little volume to mean anything."},
    {"id": "intended_switch", "window_hours": 24,
     "label": {"incidents": [], "expected_changes": ["model_switch:claude-sonnet-5.5"]},
     "notes": "Theo moved Draft with AI to Sonnet on purpose and left an annotation. Cost triples; not an incident."},
    {"id": "unannounced_switch", "window_hours": 24,
     "label": {"incidents": [{"kind": "cost_spike", "cause": "model_switch:claude-sonnet-5.5"}], "expected_changes": []},
     "notes": "A flag edit silently moved 100% of traffic to Sonnet. Same cost jump, no annotation: an incident."},
    {"id": "acceptance_drop", "window_hours": 24,
     "label": {"incidents": [{"kind": "cost_per_accepted", "cause": "acceptance_drop"}], "expected_changes": []},
     "notes": "Cost per call is flat but users accept half as many drafts, so cost per accepted draft doubles."},
]


def as_of(i: int) -> datetime:
    s = SCENARIOS[i]
    day = BASE + timedelta(days=30 * i + 8)
    return day.replace(hour=4) if s["window_hours"] == 1 else day


def _params(sid: str, in_window: bool) -> dict:
    p = {**NORMAL, "in_tokens": dict(NORMAL["in_tokens"]), "lat": dict(NORMAL["lat"]), "model": "claude-haiku-5.5",
         "err_by_provider": {}, "lat_by_provider": {}}
    if not in_window:
        return p
    if sid == "prompt_bloat":
        p["in_tokens"]["reminder_email"] = 5200
    elif sid == "provider_latency":
        p["lat_by_provider"] = {"amazon-bedrock": 7.5}
    elif sid == "error_spike":
        p["err_by_provider"] = {"anthropic": 0.10}
    elif sid in ("intended_switch", "unannounced_switch"):
        p["model"] = "claude-sonnet-5.5"
        p["lat"] = {"anthropic": 2.1, "amazon-bedrock": 2.3}
    elif sid == "acceptance_drop":
        p["accept"] = 0.31
    return p


def _gen_scenario(i: int, rng: random.Random, out: list[dict]) -> None:
    s = SCENARIOS[i]
    end = as_of(i)
    win_start = end - timedelta(hours=s["window_hours"])
    start = end - timedelta(days=8)
    day = start
    n = 0
    while day < end:
        for hour in range(24):
            hstart = day + timedelta(hours=hour)
            if hstart >= end:
                break
            in_window = hstart >= win_start
            p = _params(s["id"], in_window)
            lam = p["requests_per_day"] * HOUR_W[hour] / sum(HOUR_W)
            if s["id"] == "overnight_noise" and in_window:
                k = 3  # exactly three calls in the tested hour
            else:
                k = max(0, int(round(rng.gauss(lam, lam ** 0.5)))) if lam > 2 else sum(rng.random() < lam / 3 for _ in range(3))
            for _ in range(k):
                n += 1
                t = hstart + timedelta(seconds=rng.randrange(3600))
                user = f"user_{rng.randrange(1, 900):03d}"
                company = f"acct_{rng.randrange(1, 400):03d}"
                trace = f"tr_{s['id'][:4]}_{n:05d}"
                grp = {"$groups": {"company": company}}
                out.append({"event": "ai_draft_requested", "distinct_id": user, "timestamp": t.strftime("%Y-%m-%d %H:%M:%S"),
                            "properties": {**grp, "$ai_trace_id": trace}})
                spans = ["line_items"] if rng.random() < 0.5 else ["line_items", "reminder_email"]
                if rng.random() < 0.5:
                    spans = ["reminder_email"]
                any_error = False
                for j, span in enumerate(spans):
                    provider = "amazon-bedrock" if rng.random() < p["bedrock_share"] else "anthropic"
                    model = p["model"]
                    tin = max(50, int(rng.gauss(p["in_tokens"][span], p["in_tokens"][span] * 0.12)))
                    tout = max(20, int(rng.gauss(p["out_tokens"][span], p["out_tokens"][span] * 0.2)))
                    base_lat = p["lat_by_provider"].get(provider, p["lat"][provider])
                    lat = round(rng.lognormvariate(0, 0.35) * base_lat * (1 + tout / 1500), 3)
                    err_rate = p["err_by_provider"].get(provider, p["err"])
                    is_err = rng.random() < err_rate
                    if s["id"] == "overnight_noise" and in_window:
                        is_err = (n % 3 == 0)
                        if n % 3 == 1:
                            lat = 12.4
                    if s["id"] == "error_spike" and in_window and provider == "anthropic" and is_err:
                        err_msg = "529 overloaded_error" if rng.random() < 0.92 else "500 api_error"
                    else:
                        err_msg = rng.choice(["500 api_error", "timeout", "529 overloaded_error"]) if is_err else None
                    pin, pout = PRICES[model]
                    cost = 0.0 if is_err else round((tin * pin + tout * pout) / 1e6, 6)
                    props = {**grp, "$ai_model": model, "$ai_provider": provider, "$ai_input_tokens": tin if not is_err else 0,
                             "$ai_output_tokens": tout if not is_err else 0, "$ai_total_cost_usd": cost, "$ai_latency": lat,
                             "$ai_is_error": is_err, "$ai_trace_id": trace, "$ai_span_name": span, "$ai_http_status": 200}
                    if is_err:
                        props["$ai_error"] = err_msg
                        props["$ai_http_status"] = int(err_msg.split()[0]) if err_msg[0].isdigit() else 504
                        any_error = True
                    out.append({"event": "$ai_generation", "distinct_id": user,
                                "timestamp": (t + timedelta(seconds=j * 2)).strftime("%Y-%m-%d %H:%M:%S"), "properties": props})
                if not any_error:
                    ev = "ai_draft_accepted" if rng.random() < p["accept"] else "ai_draft_rejected"
                    out.append({"event": ev, "distinct_id": user, "timestamp": (t + timedelta(seconds=40)).strftime("%Y-%m-%d %H:%M:%S"),
                                "properties": {**grp, "$ai_trace_id": trace}})
        day += timedelta(days=1)


_CACHE: list[dict] | None = None


def load_events() -> list[dict]:
    global _CACHE
    if _CACHE is None:
        rng = random.Random(104)
        out: list[dict] = []
        for i in range(len(SCENARIOS)):
            _gen_scenario(i, rng, out)
        _CACHE = out
    return _CACHE


def annotations() -> list[dict]:
    a = []
    for i, s in enumerate(SCENARIOS):
        end = as_of(i)
        if s["id"] == "normal":
            a.append({"id": 1000 + i, "content": "Launched the new invoice template gallery", "date_marker": (end - timedelta(hours=10)).isoformat() + "Z",
                      "scope": "project", "created_by": {"first_name": "Rina"}})
        if s["id"] == "intended_switch":
            a.append({"id": 1000 + i, "content": "Switched Draft with AI from claude-haiku-5.5 to claude-sonnet-5.5 for better line items (cost up ~3x expected)",
                      "date_marker": (end - timedelta(hours=23, minutes=30)).isoformat() + "Z", "scope": "project", "created_by": {"first_name": "Theo"}})
    return a


FLAGS = [
    {"id": 41, "key": "draft-ai-provider-routing", "name": "Draft with AI: provider routing", "active": True,
     "filters": {"groups": [{"rollout_percentage": 100}], "payloads": {"true": {"primary": "anthropic", "secondary": "amazon-bedrock", "bedrock_share": 0.3}}}},
    {"id": 42, "key": "draft-ai-model", "name": "Draft with AI: model", "active": True,
     "filters": {"groups": [{"rollout_percentage": 100}], "payloads": {"true": {"model": "claude-haiku-5.5", "fallback": "gpt-5.4-mini"}}}},
    {"id": 43, "key": "draft-ai-kill-switch", "name": "Draft with AI: kill switch (active = feature off)", "active": False,
     "filters": {"groups": [{"rollout_percentage": 100}]}},
]
NOW = as_of(len(SCENARIOS) - 1).strftime("%Y-%m-%d %H:%M:%S")


def handle(path: str, method: str, body, params):
    """Annotations and feature flags, shaped like the real API. PATCHes are recorded by the mock server."""
    if path == "/annotations" and method == "GET":
        return {"results": annotations(), "next": None}
    if path == "/feature_flags" and method == "GET":
        return {"results": FLAGS, "next": None}
    if path.startswith("/feature_flags/"):
        fid = int(path.split("/")[2])
        flag = next((f for f in FLAGS if f["id"] == fid), None)
        if flag is None:
            return (404, {"detail": "Not found."})
        if method == "GET":
            return flag
        if method == "PATCH":
            return {**flag, **(body or {})}  # not persisted: the eval reads the recorded write
    return None
