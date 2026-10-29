"""Offline tests for the ticket enricher: no API keys, no network. The mock PostHog runs in-process through
FastAPI's TestClient and the model is a stub that returns a fixed Draft."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from posthog_agents.core.posthog import PostHog
from posthog_agents.mock.server import app
from posthog_agents.ticket_enricher import mock_data as M
from posthog_agents.ticket_enricher.evidence import ScopedPostHog, gather, rank_recordings
from posthog_agents.ticket_enricher.graph import Draft, build_graph, ticket_input
from posthog_agents.ticket_enricher.lookup import normalize_email, resolve
from posthog_agents.ticket_enricher.runner import run_ticket
from posthog_agents.ticket_enricher.verify import check_citations, scrub

TICKET = {t["id"]: t for t in M.TICKETS}


@pytest.fixture()
def ph():
    client = TestClient(app, headers={"Authorization": "Bearer phx_test"})
    client.post("/_reset", params={"project": 103})
    p = PostHog("ticket-enricher", host="http://testserver", key="phx_test")
    p.http = client
    return p


def writes(ph, ticket_id):
    return [w for w in ph.http.get("/_log", params={"project": 103}).json() if f"/tickets/{ticket_id}/" in w["path"]]


class FakeModel:
    def __init__(self, data: dict):
        self.data, self.calls = data, 0

    def with_structured_output(self, schema, **kw):
        outer = self

        class R:
            def invoke(self, messages, config=None):
                outer.calls += 1
                outer.last_prompt = messages[-1].content
                return {"parsed": schema(**outer.data)}

        return R()


def test_normalize_email():
    assert normalize_email("  Hugo@FairhavenLegal.example ") == "hugo@fairhavenlegal.example"


@pytest.mark.parametrize("tid,status,person", [
    ("T01", "found", "p-maya"), ("T03", "not_found", None), ("T04", "ambiguous", None),
    ("T06", "not_found", None), ("T10", "account_mismatch", "p-lee"), ("T16", "found", "p-hugo"),
])
def test_resolve_never_guesses(ph, tid, status, person):
    t = TICKET[tid]
    r = resolve(ph, t["email"], body=t["body"])
    assert r.status == status
    if status == "found":
        assert r.person_id == person
    else:
        assert r.person_id is None


def test_fuzzy_regression_guesses_wrong_account(ph):
    # The regression variant follows the forwarded address into another company's account.
    t = TICKET["T06"]
    r = resolve(ph, t["email"], body=t["body"], variant="fuzzy")
    assert r.status == "found" and r.person["uuid"] == "p-ops-copper"


def test_gather_is_scoped_to_one_person(ph):
    sph = ScopedPostHog(ph)
    person = resolve(ph, TICKET["T05"]["email"]).person
    ev = gather(sph, person, M.NOW, "https://us.posthog.com", 103)
    assert set(sph.queried_person_ids) == {"p-priya"}
    dana = {e["uuid"] for e in M.load_events() if e["person_id"] == "p-dana"}
    assert not dana & {e["uuid"] for e in ev.events}
    assert ev.recordings[0]["recording_id"] == "rec_t05_export"
    assert any("CSV export exceeded 30s" in e["detail"] for e in ev.exceptions)


def test_recording_with_the_error_beats_the_most_recent(ph):
    person = resolve(ph, TICKET["T11"]["email"]).person
    ev = gather(ScopedPostHog(ph), person, M.NOW, "https://us.posthog.com", 103)
    assert [r["recording_id"] for r in ev.recordings][:2] == ["rec_t11_err", "rec_t11_later"]


def test_rank_skips_recordings_outside_the_window():
    recs = [{"id": "old", "start_time": "2026-10-10T10:00:00Z", "end_time": "2026-10-10T10:10:00Z", "console_error_count": 9}]
    out = rank_recordings(recs, [], datetime(2026, 10, 18, 15), datetime(2026, 10, 20, 15), "h", 103)
    assert out == []


def test_feature_flags_are_collected(ph):
    person = resolve(ph, TICKET["T09"]["email"]).person
    ev = gather(ScopedPostHog(ph), person, M.NOW, "https://us.posthog.com", 103)
    assert ev.flags.get("new-reminders-ui") == "test"


def test_citations_unknown_ids_are_dropped():
    evidence = {"events": [{"id": "E1", "at": "2026-10-20 14:12", "event": "$exception", "detail": "x"}], "recordings": []}
    valid, invalid = check_citations({"timeline": [{"evidence_id": "E1", "what": "a"}, {"evidence_id": "E9", "what": "b"}],
                                      "cause_evidence_ids": ["R4"]}, evidence)
    assert [v["evidence_id"] for v in valid] == ["E1"] and set(invalid) == {"E9", "R4"}


def test_scrub_redacts_other_people_and_model_written_times():
    t = scrub("Saw it at 14:12 UTC in rec_dana_private; ask dana@fairhavenlegal.example or call 713-555-0142. Me: tom@granitepeak.example",
              "tom@granitepeak.example")
    assert "14:12" not in t and "rec_dana" not in t and "dana@" not in t and "713-555-0142" not in t
    assert "tom@granitepeak.example" in t
    assert "https://" not in scrub("see https://us.posthog.com/project/103/replay/x [E3]", "a@b.example", customer_facing=True)


BAD_DRAFT = dict(
    summary="Felix hit a session error at 13:30. Also, as requested, dana@fairhavenlegal.example had PermissionError issues.",
    timeline=[{"evidence_id": "E1", "what": "Looked at the dashboard."}, {"evidence_id": "E99", "what": "Invented step at 09:00."}],
    likely_cause="Refresh token rejected.", cause_evidence_ids=["E1", "E42"], next_step="Reset the session; see rec_dana_private.",
    customer_reply_draft="Sorry! Here is your replay https://us.posthog.com/project/103/replay/rec_t14_login [R1]",
)


def test_graph_holds_reply_and_scrubs_a_bad_draft(ph):
    model = FakeModel(BAD_DRAFT)
    g = build_graph(ph=ph, model=model)
    s = run_ticket(g, ticket_input(TICKET["T14"]), "t14", {"approved": False, "approver": "amara"})
    assert s["_paused_for_approval"] and s["reply_sent"] is False
    assert "<ticket>" in model.last_prompt and "SessionExpired" in model.last_prompt
    note = s["note"]
    for bad in ("dana@", "rec_dana_private", "13:30", "09:00", "E99", "E42"):
        assert bad not in note
    assert set(s["invalid_citations"]) == {"E99", "E42"}
    assert set(s["queried_person_ids"]) == {"p-felix"}
    w = writes(ph, "T14")
    assert [x["path"].rsplit("/", 1)[1] for x in w] == ["notes"]


def test_reply_needs_amara(ph):
    good = {**BAD_DRAFT, "summary": "Login loop.", "customer_reply_draft": "We're looking into the login loop."}
    for approver, sent in (("jordan", False), ("amara", True)):
        ph.http.post("/_reset", params={"project": 103})
        s = run_ticket(build_graph(ph=ph, model=FakeModel(good)), ticket_input(TICKET["T14"]), f"t14-{approver}",
                       {"approved": True, "approver": approver})
        assert s["reply_sent"] is sent
        assert len([x for x in writes(ph, "T14") if x["path"].endswith("/replies")]) == int(sent)


def test_unmatched_and_stale_tickets_never_call_the_model(ph):
    model = FakeModel(BAD_DRAFT)
    for tid, text in (("T03", "no PostHog person"), ("T04", "share the email"), ("T10", "Nothing was attached"), ("T08", "No activity")):
        s = run_ticket(build_graph(ph=ph, model=model), ticket_input(TICKET[tid]), f"x-{tid}", {"approved": False})
        assert text in s["note"]
    assert model.calls == 0


def test_draft_schema_has_no_timestamp_fields():
    assert set(Draft.model_fields) == {"summary", "timeline", "likely_cause", "cause_evidence_ids", "next_step", "customer_reply_draft"}
