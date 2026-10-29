"""Offline tests for replay-to-bug: no keys, no network. A fake model, fake PostHog and fake tracker."""

from __future__ import annotations

import json

import pytest
from langgraph.types import Command

from posthog_agents.replay_to_bug import mock_data as M
from posthog_agents.replay_to_bug import report as RP
from posthog_agents.replay_to_bug import timeline as T
from posthog_agents.replay_to_bug.graph import NaiveDraft, build_graph, run_one

RECS = {r["id"]: r for r in M.load_recordings()}


def tl(rid):
    return T.build(RECS[rid]["snapshots"])


@pytest.mark.parametrize("rid", list(M.LABELS))
def test_classification_matches_every_label(rid):
    assert T.classify(tl(rid)) == M.LABELS[rid]["category"]


def test_signatures_match_existing_tracker_bugs():
    sigs = {b["id"]: b["signature"] for b in M.EXISTING_BUGS}
    assert T.signature(tl("rec_07_pdf_dup")) == sigs["BUG-311"]
    assert T.signature(tl("rec_14_payments_null_amount")) == sigs["BUG-280"]
    assert T.signature(tl("rec_02_reminder_500")) == "net:POST /api/invoices/:id/reminders 500"


def test_scrub_removes_emails_cards_and_tokens():
    s = T.scrub("dana@x.example 4242 4242 4242 4242 token=abc123 eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI0MiJ9.c2lnbmF0dXJl sk_live_abcdef123456")
    assert T.find_pii(s) == []
    assert "dana@" not in s and "4242" not in s and "abc123" not in s and "sk_live" not in s


def test_model_view_has_no_pii_and_no_console_log_injection():
    view = T.for_model(tl("rec_08_export_pii"), RECS["rec_08_export_pii"])
    for p in M.LABELS["rec_08_export_pii"]["pii"]:
        assert p not in view
    inj = T.for_model(tl("rec_09_injection"), RECS["rec_09_injection"])
    assert "AI ASSISTANT" not in inj and "All good" not in inj


def test_long_session_keeps_the_steps_near_the_failure():
    t = tl("rec_10_long_session")
    assert len([s for s in t["steps"] if s["ms"] <= t["primary"]["ms"]]) == T.MAX_STEPS
    assert t["omitted_steps"] > 200


def test_verify_catches_invented_steps_timestamps_statuses_paths_quotes_and_pii():
    t = tl("rec_02_reminder_500")
    bad = {"title": "Fails with 503 at 00:09", "summary": 'Says "Totally made up message"', "expected": "", "severity": "low",
           "actual": "mail jane@x.example", "suspected_cause": "/api/nope broke",
           "steps_to_reproduce": [{"step_id": "S9", "text": "x"}], "evidence": [{"evidence_id": "E1", "t": "00:01", "note": ""}]}
    p = " | ".join(RP.verify(bad, t))
    for needle in ("S9", "timestamp 00:09", "status 503", "Totally made up", "personal data", "/api/nope", "E1 has timestamp"):
        assert needle in p


def test_verify_accepts_stack_line_numbers_durations_and_apostrophes():
    t = tl("rec_14_payments_null_amount")
    d = RP.fallback(t, "frontend_exception")
    d["actual"] = "TypeError (Cannot read properties of null (reading 'amount') at formatRow, PaymentsTable.tsx:77:30); user saw \"Something went wrong.\""
    assert RP.verify(d, t) == []
    t2 = tl("rec_02_reminder_500")
    d2 = RP.fallback(t2, "api_error")
    d2["summary"] = "The request failed after 412 ms."
    assert RP.verify(d2, t2) == []


@pytest.mark.parametrize("rid", [r for r, l in M.LABELS.items() if l["action"] != "skip"])
def test_fallback_report_always_verifies(rid):
    t = tl(rid)
    assert RP.verify(RP.fallback(t, T.classify(t)), t) == []


# ---------- graph with fakes ----------

class FakePH:
    def recording(self, rid): return {k: v for k, v in RECS[rid].items() if k != "snapshots"}
    def snapshots(self, rid): return {"snapshots": RECS[rid]["snapshots"]}


class FakeTracker:
    def __init__(self): self.created, self.comments = [], []
    def bugs(self): return M.EXISTING_BUGS + self.created
    def create(self, bug): b = {"id": f"BUG-{400 + len(self.created)}", "state": "open", **bug}; self.created.append(b); return b
    def comment(self, bug_id, body): self.comments.append((bug_id, body)); return {"ok": True}


class FakeModel:
    """Returns the code-built report (or a deliberately bad one first, to exercise the retry)."""
    def __init__(self, bad_first=False, naive_is_bug=True): self.calls, self.bad_first, self.naive_is_bug = 0, bad_first, naive_is_bug
    def with_structured_output(self, schema, method=None):
        model = self
        class R:
            def invoke(self, msgs, config=None):
                model.calls += 1
                text = msgs[1][1]
                rid = text.split()[1] if text.startswith("Recording") else json.loads(text)["recording"]["id"]
                t = tl(rid)
                d = RP.fallback(t, T.classify(t)); d.pop("fallback")
                if model.bad_first and model.calls == 1:
                    d["title"] = "Fails with 503"
                if schema is NaiveDraft:
                    return NaiveDraft(**d, is_bug=model.naive_is_bug, category="api_error")
                return RP.BugDraft(**d)
        return R()


def run(rid, decision="approve", variant="baseline", model=None, edits=None):
    tr = FakeTracker()
    g = build_graph(variant, model=model or FakeModel(), ph=FakePH(), tracker=tr)
    out = run_one(g, rid, lambda p: {"decision": decision, "approver": "amara", **({"edits": edits} if edits else {})})
    return out, tr


def test_not_a_bug_is_never_filed_or_sent_to_the_model():
    m = FakeModel()
    out, tr = run("rec_06_paylane_outage", model=m)
    assert out["action"] == "skip" and tr.created == [] and m.calls == 0


def test_duplicate_becomes_a_comment_not_a_new_bug():
    out, tr = run("rec_07_pdf_dup")
    assert tr.created == [] and tr.comments and tr.comments[0][0] == "BUG-311"


def test_closed_bug_with_same_signature_is_filed_as_a_regression():
    out, tr = run("rec_14_payments_null_amount")
    assert len(tr.created) == 1 and "BUG-280" in tr.created[0]["summary"]


def test_reject_writes_nothing():
    out, tr = run("rec_13_customer_undefined", decision="reject")
    assert tr.created == [] and out["result"]["status"] == "rejected"


def test_nothing_is_written_before_approval():
    tr = FakeTracker()
    g = build_graph(model=FakeModel(), ph=FakePH(), tracker=tr)
    out = run_one(g, "rec_01_send_invoice", decide=None)
    assert out.get("pending") and tr.created == []
    out = g.invoke(Command(resume={"decision": "approve", "approver": "amara"}), {"configurable": {"thread_id": "rec_01_send_invoice"}})
    assert len(tr.created) == 1


def test_approver_edit_is_scrubbed():
    out, tr = run("rec_02_reminder_500", decision="edit", edits={"title": "Reminder 500 (reported by jane@client.example)"})
    assert "jane@" not in tr.created[0]["title"] and "[email]" in tr.created[0]["title"]


def test_bad_draft_is_retried_with_the_problems():
    m = FakeModel(bad_first=True)
    out, tr = run("rec_02_reminder_500", model=m)
    assert m.calls == 2 and out["attempts"] == 2 and not out["used_fallback"]
    assert "503" not in tr.created[0]["title"]


def test_naive_variant_files_duplicates_and_skips_verification():
    out, tr = run("rec_07_pdf_dup", variant="naive")
    assert len(tr.created) == 1 and tr.comments == []
