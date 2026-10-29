"""Offline tests for the churn-risk agent: no network, no keys. HogQL runs on the mock store directly."""

import threading
from datetime import date

import pytest

from posthog_agents.churn_risk import mock_data as M
from posthog_agents.churn_risk import queries
from posthog_agents.churn_risk.accounts import load_accounts
from posthog_agents.churn_risk.graph import FORBIDDEN_IN_EMAIL, _week_label, build_graph, check_draft, fallback
from posthog_agents.churn_risk.numbers import unsupported_numbers
from posthog_agents.churn_risk.scoring import incident_weeks, score, weekly


class FakePH:
    """Runs the agent's real HogQL through the mock's own handler (same path the HTTP mock takes)."""
    project = 101
    _lock = threading.Lock()   # the HTTP mock serves one request at a time; so does this fake

    def __init__(self):
        self.annotations = []

    def rows(self, q, values=None):
        with self._lock:
            r = M.handle("/query", "POST", {"query": {"kind": "HogQLQuery", "query": q, "values": values or {}}}, {})
        assert not isinstance(r, tuple), r
        return [dict(zip(r["columns"], row)) for row in r["results"]]

    def _get(self, path):
        return M.handle(path.rstrip("/"), "GET", {}, {})

    def recording(self, rid):
        rec = next((x for x in M.load_recordings() if x["id"] == rid), None)
        if rec is None:
            raise KeyError(rid)
        return rec

    def annotate(self, content, date_marker):
        self.annotations.append({"content": content, "date_marker": date_marker})
        return {"id": len(self.annotations)}


@pytest.fixture(scope="module")
def ph():
    return FakePH()


def _score(ph, ws, run, guards=True):
    return score(queries.fetch_usage(ph, ws), queries.fetch_first_seen(ph, ws), load_accounts(),
                 queries.fetch_annotations(ph), ws, run, guards=guards)


W9, W6 = date(2026, 10, 19), date(2026, 9, 28)


def test_data_is_deterministic():
    assert len(M.ACCOUNTS) == 40 and len(M.EXPECTED_AT_RISK_W9) == 9
    assert M.TRUTH["acct_harbor_light_marine"]["scenario"] in M.SCENARIOS


def test_hogql_returns_group_keys(ph):
    rows = queries.fetch_usage(ph, W9)
    assert rows and all(r["account"].startswith("acct_") for r in rows)


def test_finds_every_at_risk_account_and_nothing_else(ph):
    r = _score(ph, W9, date(2026, 10, 26))
    assert r["incident"] is None
    assert sorted(a for a, f in r["accounts"].items() if f["at_risk"]) == M.EXPECTED_AT_RISK_W9


def test_each_signal_type_fires(ph):
    acc = _score(ph, W9, date(2026, 10, 26))["accounts"]
    txt = lambda s: " ".join(acc[M.BY_SCENARIO[s][0]]["signals"])  # noqa: E731
    assert "fell to" in txt("usage_drop")
    assert "most active user" in txt("champion_left")
    assert "seats were active" in txt("seat_cut")
    assert "Errors and rage clicks" in txt("error_spike")


def test_suppressions_have_reasons(ph):
    acc = _score(ph, W9, date(2026, 10, 26))["accounts"]
    for a in M.BY_SCENARIO["seasonal"]:
        assert acc[a]["suppressed"] == "seasonal" and not acc[a]["at_risk"]
    for a in M.BY_SCENARIO["new_account"]:
        assert acc[a]["suppressed"] == "ramping"
    for a in M.BY_SCENARIO["recently_alerted"]:
        assert acc[a]["suppressed"] == "recently_alerted"


def test_noisy_dip_is_not_an_alert(ph):
    acc = _score(ph, W9, date(2026, 10, 26))["accounts"]
    for a in M.BY_SCENARIO["noisy"]:
        assert not acc[a]["signals"]


def test_outage_week_is_an_incident_and_alerts_nobody(ph):
    r = _score(ph, W6, date(2026, 10, 5))
    assert r["incident"] is not None
    assert not any(f["at_risk"] for f in r["accounts"].values())


def test_outage_week_is_left_out_of_later_baselines(ph):
    r = _score(ph, W9, date(2026, 10, 26))
    assert -3 in r["incident_weeks"]
    a = M.BY_SCENARIO["usage_drop"][0]
    true_base = sum(M.TRUTH[a]["core_by_week"][w] for w in (5, 7, 8, 4)) / 4
    assert r["accounts"][a]["core_baseline"] == round(true_base, 1)


def test_without_guards_it_cries_wolf(ph):
    r = _score(ph, W6, date(2026, 10, 5), guards=False)
    assert sum(f["at_risk"] for f in r["accounts"].values()) > 30
    r9 = _score(ph, W9, date(2026, 10, 26), guards=False)
    flagged = {a for a, f in r9["accounts"].items() if f["at_risk"]}
    assert flagged & set(M.BY_SCENARIO["seasonal"] + M.BY_SCENARIO["new_account"])


def test_re_alert_window_is_14_days(ph):
    a = M.BY_SCENARIO["recently_alerted"][0]
    later = _score(ph, W9, date(2026, 10, 29))["accounts"][a]   # 15 days after the Oct 14 alert
    assert later["at_risk"] and later["suppressed"] is None


def test_number_check():
    facts = {"name": "Acme", "account_id": "acct_acme", "core_this_week": 6, "core_baseline": 18.8, "core_change_pct": -68, "seats": 5}
    assert unsupported_numbers("Acme: core fell to 6 from 18.8 (-68%), 5 seats, 4-week average.", facts, ("Acme",)) == []
    assert unsupported_numbers("Core fell 75% to 6.", facts) == ["75%"]
    assert unsupported_numbers("See https://x.example/replay/123 for week Oct 19–25", facts, ("Oct 19–25",)) == []


def test_customer_email_guardrails():
    facts = {"name": "Acme", "account_id": "acct_acme", "core_this_week": 6}
    good = {"slack_alert": "Acme dropped to 6.", "email_subject": "Checking in", "email_body": "Hi! Can we help? The Ledgerly team"}
    assert check_draft(good, facts, ()) == []
    bad = {**good, "email_body": "We saw your churn risk score rise and watched your session recordings."}
    probs = " ".join(check_draft(bad, facts, ()))
    assert "churn" in probs and "recording" in probs
    assert check_draft({**good, "email_body": "You only sent 6 invoices."}, facts, ())


def test_fallback_template_passes_checks():
    facts = {"name": "Acme", "account_id": "acct_acme", "core_this_week": 6, "signals": ["Core invoicing actions fell to 6."], "evidence_recordings": []}
    d = fallback(facts, "Oct 19–25")
    assert check_draft(d, facts, ("Oct 19–25", "Acme")) == []


def test_week_label():
    assert _week_label(date(2026, 10, 19)) == "Oct 19–25"
    assert _week_label(date(2026, 9, 28)) == "Sep 28–Oct 4"


class FakeModel:
    """Stands in for the chat model: returns a fixed, valid draft."""
    def with_structured_output(self, schema, **kw):
        outer = self

        class R:
            def invoke(self, msgs, config=None):
                import json, re
                facts = json.loads(msgs[1].content.split("FACTS:\n", 1)[1].split("\n\nYour last", 1)[0])
                return {"parsed": {"slack_alert": f"{facts['name']} needs a look. " + " ".join(facts["signals"]) + " Next step: call their admin.",
                                   "email_subject": "Checking in", "email_body": "Hi, can we help with anything? The Ledgerly team"}}
        return R()


def test_graph_pauses_for_approval_and_sends_only_what_jordan_approves(ph):
    from langgraph.types import Command
    fake = FakePH()
    g = build_graph(model=FakeModel(), ph=fake)
    cfg = {"configurable": {"thread_id": "t1"}}
    s = g.invoke({"week_start": "2026-10-19", "run_date": "2026-10-26"}, cfg)
    assert "__interrupt__" in s
    assert not [o for o in s["outbox"] if o["channel"] == "email"] and not fake.annotations
    emails = s["__interrupt__"][0].value["emails"]
    assert sorted(e["account_id"] for e in emails) == M.EXPECTED_AT_RISK_W9
    ids = [e["account_id"] for e in emails]
    dec = {ids[0]: {"action": "approve", "by": "@jordan"}, ids[1]: {"action": "reject", "by": "@jordan"},
           ids[2]: {"action": "approve", "by": "@devon"}, ids[3]: {"action": "edit", "by": "@jordan", "body": "Edited."}}
    s = g.invoke(Command(resume=dec), cfg)
    sent = {o["account_id"]: o for o in s["outbox"] if o["channel"] == "email"}
    assert set(sent) == {ids[0], ids[3]} and sent[ids[3]]["body"] == "Edited."
    assert [a["content"] for a in fake.annotations] == [f"churn-risk: alerted {ids[0]}", f"churn-risk: alerted {ids[3]}"] or \
        sorted(a["content"] for a in fake.annotations) == sorted([f"churn-risk: alerted {ids[0]}", f"churn-risk: alerted {ids[3]}"])


def test_outage_graph_reports_incident_and_never_pauses():
    g = build_graph(model=FakeModel(), ph=FakePH())
    s = g.invoke({"week_start": "2026-09-28", "run_date": "2026-10-05"}, {"configurable": {"thread_id": "t2"}})
    assert "__interrupt__" not in s
    assert [o["kind"] for o in s["outbox"]] == ["tracking_incident"]
