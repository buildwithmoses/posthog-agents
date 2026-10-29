"""Offline tests for the LLM watcher: no server, no keys. HogQL runs on the same DuckDB translator as the mock."""

from __future__ import annotations

import pytest
from langgraph.types import Command

from posthog_agents.llm_watch import analyze as A
from posthog_agents.llm_watch import mock_data as M
from posthog_agents.llm_watch import numbers, queries, truth
from posthog_agents.llm_watch.graph import build_graph, template
from posthog_agents.mock.hogql import Store


class FakePH:
    """Answers the same calls as core.posthog.PostHog, locally."""
    project = 104

    def __init__(self):
        self.store = Store.from_module(M)
        self.patches = []
        outer = self

        class Http:
            def patch(self, path, json):
                outer.patches.append((path, json))

                class R:
                    def raise_for_status(self):
                        pass
                return R()
        self.http = Http()

    def rows(self, q, values=None):
        r = self.store.query(q, values)
        return [dict(zip(r["columns"], row)) for row in r["results"]]

    def _get(self, path):
        return M.handle(path.rstrip("/"), "GET", None, {})


@pytest.fixture(scope="module")
def ph():
    return FakePH()


@pytest.fixture(scope="module")
def analyses(ph):
    return {s["id"]: A.analyze(queries.collect(ph, M.as_of(i), s["window_hours"])) for i, s in enumerate(M.SCENARIOS)}


def test_every_scenario_matches_its_label(analyses):
    for s in M.SCENARIOS:
        a = analyses[s["id"]]
        assert [(x["kind"], x["cause"]) for x in a["incidents"]] == [(x["kind"], x["cause"]) for x in s["label"]["incidents"]], s["id"]
        assert [f"model_switch:{e['model']}" for e in a["expected_changes"]] == s["label"]["expected_changes"], s["id"]


def test_query_numbers_match_ground_truth(analyses):
    for i, s in enumerate(M.SCENARIOS):
        assert truth.verify(analyses[s["id"]], M.as_of(i), s["window_hours"]) == [], s["id"]


def test_minimum_volume_rule_is_what_silences_overnight_noise(ph):
    i = [s["id"] for s in M.SCENARIOS].index("overnight_noise")
    raw = queries.collect(ph, M.as_of(i), 1)
    assert A.analyze(raw)["incidents"] == []
    assert A.analyze(raw, A.NO_MIN)["incidents"], "without the volume rule, 3 calls at 3am raise an alert"


def test_annotation_is_what_makes_a_model_switch_expected(analyses):
    assert analyses["intended_switch"]["incidents"] == []
    assert analyses["unannounced_switch"]["incidents"][0]["cause"] == "model_switch:claude-sonnet-5.5"


def test_unrelated_annotation_does_not_suppress_anything(analyses):
    assert analyses["normal"]["facts"]["annotations_in_window"]
    assert analyses["normal"]["expected_changes"] == []


def test_prompt_bloat_evidence_names_the_span(analyses):
    inc = analyses["prompt_bloat"]["incidents"][0]
    assert inc["evidence"]["span"] == "reminder_email"
    assert inc["evidence"]["ratio"]["value"] > 3
    assert len(inc["top_traces"]) == 3


def test_proposals_only_for_containable_incidents(analyses):
    assert analyses["error_spike"]["proposal"]["flag_key"] == "draft-ai-provider-routing"
    assert analyses["provider_latency"]["proposal"]["patch"]["filters"]["payloads"]["true"]["primary"] == "anthropic"
    assert analyses["unannounced_switch"]["proposal"]["flag_key"] == "draft-ai-model"
    for sid in ("prompt_bloat", "acceptance_drop", "normal", "intended_switch"):
        assert analyses[sid]["proposal"] is None, sid


def test_number_check(analyses):
    a = analyses["prompt_bloat"]
    cost = a["facts"]["totals"]["cost"]["display"]
    assert numbers.check(f"Cost hit {cost} on claude-haiku-5.5 in the last 24 hours.", a) == []
    assert numbers.check("Cost hit $9.99, up 312%.", a) == ["9.99", "312"]


def test_template_fallback_passes_the_number_check(analyses):
    for sid, a in analyses.items():
        assert numbers.check(template(a), a) == [], sid


class FakeLLM:
    """Writes an alert that repeats a fact verbatim (the graph's number check must accept it)."""
    def invoke(self, msgs):
        import json, re
        facts = json.loads(msgs[1].content.split("FACTS:\n", 1)[1])

        class R:
            content = f"Cost was {facts['facts']['totals']['cost']} over the window."
        return R()


def _run(ph, sid, decision):
    i = [s["id"] for s in M.SCENARIOS].index(sid)
    g = build_graph(ph=ph, model=FakeLLM())
    cfg = {"configurable": {"thread_id": f"t-{sid}-{decision}"}}
    out = g.invoke({"as_of": M.as_of(i).isoformat(sep=" "), "hours": M.SCENARIOS[i]["window_hours"], "variant": "baseline"}, cfg)
    assert "__interrupt__" in out
    return g.invoke(Command(resume=decision), cfg)


def test_flag_change_needs_theo(ph):
    before = len(ph.patches)
    assert _run(ph, "error_spike", {"approved": True, "approver": "rina"})["applied"] is None
    assert _run(ph, "error_spike", {"approved": False, "approver": "theo"})["applied"] is None
    assert len(ph.patches) == before
    assert _run(ph, "error_spike", {"approved": True, "approver": "theo"})["applied"]["flag"] == "draft-ai-provider-routing"
    assert len(ph.patches) == before + 1


def test_no_interrupt_without_proposal(ph):
    i = [s["id"] for s in M.SCENARIOS].index("normal")
    g = build_graph(ph=ph, model=FakeLLM())
    out = g.invoke({"as_of": M.as_of(i).isoformat(sep=" "), "hours": 24, "variant": "baseline"}, {"configurable": {"thread_id": "n"}})
    assert "__interrupt__" not in out and out["applied"] is None
    assert out["writer"]["mode"] == "first_pass"
