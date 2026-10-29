"""Fictional PostHog data for project 101 (churn-risk). Deterministic: same seed, same events.

40 Ledgerly accounts over 10 weeks (Mon 2026-08-17 .. Sun 2026-10-25). "This week" for the main eval is
Oct 19-25 (week index 9). Week 6 (Sep 28 - Oct 4) is a tracking outage: the web SDK broke and every account
sent about a quarter of its events.

Scenarios (label -> what happens), used by the evals as ground truth:
  healthy        steady usage, +-10% noise
  noisy          healthy, but week 9 is ~22% lower (should NOT alert)
  usage_drop     every user does ~35% of normal in week 9
  champion_left  the heaviest user (>=45% of activity) stops in week 9; others pick up a little
  seat_cut       half the active users stop in week 9; the rest do a bit more
  error_spike    $exception and $rageclick spike in week 9 and core actions fall ~30%
  seasonal       ~65% drop in weeks 8-9, covered by a "seasonal:" annotation
  new_account    first event Oct 5; week 9 is half of week 8 (ramping, should NOT alert)
  recently_alerted  usage drop in weeks 8-9, but a churn-risk alert annotation exists from Oct 14
Account roster (name, plan, seats, owner) comes from the CRM, not PostHog: see ACCOUNTS / accounts.py.
"""

from __future__ import annotations

import random
import re
from datetime import datetime, timedelta

NOW = "2026-10-26 09:00:00"
WEEK0 = datetime(2026, 8, 17)
N_WEEKS = 10
OUTAGE_WEEK = 6
THIS_WEEK = 9
CORE = ("invoice_created", "invoice_sent", "payment_received")
FEATURES = ("reminder_sent", "integration_synced", "report_viewed", "ai_draft_requested")
OWNERS = ["@maya", "@devon", "@luis", "@carmen"]
PLANS = {"Starter": 49, "Growth": 199, "Scale": 799}

_NAMES = ["Alder Freight", "Birchwood Dental Supply", "Cobalt Roofing", "Dunmore Legal", "Elm Street Bakery Co",
          "Fairview Plumbing", "Granite Peak Outfitters", "Harbor Light Marine", "Ironclad Fabrication", "Juniper Florals",
          "Kestrel Logistics", "Lakeside Veterinary", "Maple Row Builders", "Northgate Printing", "Oakhurst Consulting",
          "Pinecrest HVAC", "Quarry Hill Stone", "Redfern Catering", "Silverline Electric", "Tamarack Landscaping",
          "Upland Coffee Roasters", "Vantage Signworks", "Willow Creek Farms", "Yarrow Health Clinic", "Zephyr Bike Works",
          "Aspen Ridge Realty", "Brookside Auto", "Cedar Point Tutors", "Driftwood Design", "Emberly Candles",
          "Foxglove Interiors", "Goldfinch Bookkeeping", "Hearthstone Masonry", "Indigo Print Lab", "Jetty Seafood Supply",
          "Kingfisher Events", "Larkspur Pediatrics", "Meridian Survey", "Nightjar Security", "Orchard Lane Grocers"]

SCENARIOS = (["healthy"] * 24 + ["noisy"] * 2 + ["usage_drop"] * 3 + ["champion_left"] * 2 + ["seat_cut"] * 2
             + ["error_spike"] * 2 + ["seasonal"] * 2 + ["new_account"] * 2 + ["recently_alerted"] * 1)
AT_RISK = {"usage_drop", "champion_left", "seat_cut", "error_spike"}


def _slug(name: str) -> str:
    return "acct_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _build():
    rng = random.Random(1017)
    order = SCENARIOS[:]
    rng.shuffle(order)
    accounts, events, recordings, truth = [], [], [], {}
    for i, (name, scen) in enumerate(zip(_NAMES, order)):
        aid = _slug(name)
        plan = rng.choice(["Starter", "Growth", "Growth", "Scale"])
        n_users = {"seat_cut": 8, "champion_left": 4, "new_account": 3}.get(scen, rng.randint(3, 6))
        seats = n_users + (2 if scen == "seat_cut" else rng.randint(0, 2))
        created = datetime(2026, 10, 5) if scen == "new_account" else WEEK0 - timedelta(days=rng.randint(60, 700))
        accounts.append({"account_id": aid, "name": name, "plan": plan, "mrr": PLANS[plan], "seats": seats,
                         "owner": OWNERS[i % len(OWNERS)], "crm_created_at": created.strftime("%Y-%m-%d")})
        users = [f"{aid}_u{k}" for k in range(n_users)]
        rates = [rng.randint(5, 9) for _ in users]           # core actions per user per week
        if scen == "champion_left":
            rates[0] = sum(rates[1:]) + rng.randint(1, 3)      # champion does about half of all work
        truth[aid] = {"scenario": scen, "core_by_week": [0] * N_WEEKS, "users_by_week": [set() for _ in range(N_WEEKS)]}
        for w in range(N_WEEKS):
            wk_start = WEEK0 + timedelta(weeks=w)
            if wk_start + timedelta(days=7) <= created:
                continue
            for u_idx, uid in enumerate(users):
                mult = rng.uniform(0.9, 1.1)
                active = True
                errors = 1 if rng.random() < 0.3 else 0
                rage = 0
                if w == THIS_WEEK:
                    if scen == "noisy": mult *= 0.78
                    elif scen == "usage_drop": mult *= 0.35
                    elif scen == "champion_left":
                        if u_idx == 0: active = False
                        else: mult *= 1.15
                    elif scen == "seat_cut":
                        if u_idx >= n_users // 2: active = False
                        else: mult *= 1.4
                    elif scen == "error_spike":
                        mult *= 0.7; errors = rng.randint(6, 9); rage = rng.randint(2, 4)
                if scen in ("seasonal",) and w in (8, 9): mult *= 0.35
                if scen == "recently_alerted" and w in (8, 9): mult *= 0.4
                if scen == "new_account":
                    mult *= {3: 0.0}.get(w, 1.0) * (0.5 if w == THIS_WEEK else 1.0)
                if not active:
                    continue
                n_core = round(rates[u_idx] * mult)
                n_feat = rng.randint(1, 3) if n_core else 0
                if w == OUTAGE_WEEK:  # SDK outage: only ~1 in 4 events arrive
                    n_core, n_feat, errors, rage = n_core // 4, n_feat // 4, 0, 0
                if n_core + n_feat + errors + rage == 0:
                    continue
                n_sessions = 2
                sids = [f"s_{aid}_{u_idx}_{w}_{k}" for k in range(n_sessions)]
                def ts(k):
                    day = (k * 3 + u_idx) % 5
                    return wk_start + timedelta(days=day, hours=9 + (k % 8), minutes=(k * 7) % 60)
                k = 0
                def emit(event, extra=None):
                    nonlocal k
                    sid = sids[k % n_sessions]
                    props = {"$groups": {"company": aid}, "$session_id": sid, "plan": plan, **(extra or {})}
                    events.append({"event": event, "distinct_id": uid, "timestamp": ts(k).strftime("%Y-%m-%d %H:%M:%S"),
                                   "properties": props, "person_properties": {"email": f"{uid}@{aid[5:]}.example", "plan": plan}})
                    k += 1
                for j in range(n_core):
                    emit(CORE[j % 3])
                for j in range(n_feat):
                    emit(FEATURES[(j + u_idx) % len(FEATURES)])
                for _ in range(errors):
                    emit("$exception", {"$exception_type": "TypeError", "$exception_message": "Cannot read properties of undefined (reading 'lineItems')"})
                for _ in range(rage):
                    emit("$rageclick", {"$el_text": "Send invoice"})
                truth[aid]["core_by_week"][w] += n_core
                truth[aid]["users_by_week"][w].add(uid)
                for s_i, sid in enumerate(sids):
                    has_err = (errors + rage) > 0 and s_i == 0
                    recordings.append({"id": sid, "distinct_id": uid, "start_time": ts(s_i).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                       "recording_duration": 300 + 60 * s_i, "console_error_count": errors if has_err else 0,
                                       "click_count": 20 + rage * 5, "keypress_count": 40, "start_url": "https://app.ledgerly.example/invoices",
                                       "person": {"distinct_ids": [uid], "properties": {"email": f"{uid}@{aid[5:]}.example"}}})
    for t in truth.values():
        t["wau_by_week"] = [len(s) for s in t.pop("users_by_week")]
    return accounts, events, recordings, truth


ACCOUNTS, _EVENTS, _RECORDINGS, TRUTH = _build()
BY_SCENARIO = {s: sorted(a for a, t in TRUTH.items() if t["scenario"] == s) for s in set(SCENARIOS)}
EXPECTED_AT_RISK_W9 = sorted(a for a, t in TRUTH.items() if t["scenario"] in AT_RISK)

ANNOTATIONS = (
    [{"id": 900 + i, "content": f"seasonal:{a} closed for the harvest season until 2026-11-02", "date_marker": "2026-10-12T00:00:00Z",
      "scope": "project", "created_by": {"first_name": "Jordan"}} for i, a in enumerate(BY_SCENARIO["seasonal"])]
    + [{"id": 950 + i, "content": f"churn-risk: alerted {a}", "date_marker": "2026-10-14T00:00:00Z", "scope": "project",
        "created_by": {"first_name": "churn-risk agent"}} for i, a in enumerate(BY_SCENARIO["recently_alerted"])]
)


def load_events():
    return _EVENTS


def load_recordings():
    return _RECORDINGS


def load_persons():
    seen = {}
    for e in _EVENTS:
        seen.setdefault(e["distinct_id"], {"uuid": e["distinct_id"], "distinct_ids": [e["distinct_id"]], "properties": e["person_properties"]})
    return list(seen.values())


# ---- custom routes -------------------------------------------------------------------------------------------
# PostHog events carry the account as properties.$groups.company. The shared mock translator handles one level of
# properties.x, so this project pre-translates the nested group key before running the (unchanged) HogQL.
_store = None


def _own_store():
    global _store
    if _store is None:
        from posthog_agents.mock.hogql import Store
        _store = Store(_EVENTS, load_persons(), _RECORDINGS, None, NOW, None)
    return _store


_GROUP = re.compile(r"properties\.\$groups\.([A-Za-z0-9_]+)")


def handle(path, method, body, params):
    if path == "/query" and method == "POST":
        q = (body.get("query") or {})
        hogql = q.get("query", "")
        pre = _GROUP.sub(lambda m: f"json_extract_string(properties, '$.\"$groups\".\"{m.group(1)}\"')", hogql)
        try:
            res = _own_store().query(pre, q.get("values") or {})
        except Exception as e:  # mirror PostHog's 400
            return (400, {"type": "invalid_request", "detail": f"HogQL error: {e}"})
        res["hogql"] = hogql
        return res
    if path == "/annotations" and method == "GET":
        return {"results": ANNOTATIONS, "next": None}
    return None
