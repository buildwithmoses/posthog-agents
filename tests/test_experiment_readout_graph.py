"""Readout checks and the approval path, with a fake model and an in-process PostHog. No network, no keys."""

from types import SimpleNamespace

import pytest

from posthog_agents.experiment_readout import mock_data as M
from posthog_agents.experiment_readout import readout as R
from posthog_agents.experiment_readout.graph import build_graph, resume, run_until_approval
from posthog_agents.mock.hogql import Store


class FakePostHog:
    """Answers the same calls as core.posthog.PostHog from one experiment's fixture events."""

    def __init__(self, spec_id: int):
        spec = next(s for s in M.SPECS if s["id"] == spec_id)
        self.project = 105
        self.store = Store(M._events_for(spec), now=M.NOW)
        self.exps = {e["id"]: e for e in M.load_experiments()}
        self.patches: list[tuple[str, dict]] = []
        outer = self

        class _Resp:
            def raise_for_status(self):
                return None

        self.http = SimpleNamespace(patch=lambda path, json: (outer.patches.append((path, json)), _Resp())[1])

    def experiment(self, eid):
        return self.exps[eid]

    def rows(self, query, values=None):
        r = self.store.query(query, values)
        return [dict(zip(r["columns"], row)) for row in r["results"]]


class FakeModel:
    """Returns readouts in order; each is a function of the user prompt's facts."""

    def __init__(self, *makers):
        self.makers = list(makers)
        self.calls = 0

    def with_structured_output(self, schema, method=None):
        model = self

        class _Run:
            def invoke(self, messages, config=None):
                maker = model.makers[min(model.calls, len(model.makers) - 1)]
                model.calls += 1
                return maker(messages[1].content)
        return _Run()


def good(prompt: str) -> R.Readout:
    import json
    facts = json.loads(prompt.split("Facts JSON:\n", 1)[1].split("\n\nYour previous", 1)[0])
    r = R.template_readout(facts)
    return r


def invented(prompt: str) -> R.Readout:
    r = good(prompt)
    return r.model_copy(update={"summary": r.summary + " That is 3.6 times better and worth $41,000 a year."})


@pytest.fixture(scope="module")
def ph1():
    return FakePostHog(1)


def test_number_check_accepts_rounding_and_rejects_invented_numbers(ph1):
    from posthog_agents.experiment_readout.analysis import analyze
    from posthog_agents.experiment_readout.queries import fetch
    facts = analyze(fetch(ph1, 1, M.NOW))["facts"]
    ok = R.template_readout(facts)
    assert R.check_readout(ok, facts) == []
    rounded = ok.model_copy(update={"summary": "Test converted at 13% against 10% for control, out of 3,000 users each."})
    assert R.check_readout(rounded, facts) == []
    bad = ok.model_copy(update={"summary": "Test is 3 points higher, a 3.6 z-score."})
    assert any("numbers" in p and "3.6" in p for p in R.check_readout(bad, facts))
    wrong_call = ok.model_copy(update={"decision": "keep_control", "variant": "control"})
    assert any("decision" in p for p in R.check_readout(wrong_call, facts))


def test_approved_by_rina_concludes_and_rolls_out(ph1):
    ph1.patches.clear()
    g = build_graph(ph=ph1, model=FakeModel(good))
    state, ask = run_until_approval(g, 1, M.NOW, "t-approve")
    assert ask and ask["decision"] == "ship" and ask["variant"] == "test"
    assert ph1.patches == []                       # nothing written before approval
    final = resume(g, "t-approve", {"approved": True, "approver": "@Rina"})
    assert final["concluded"]["rolled_out"] == "test"
    paths = [p for p, _ in ph1.patches]
    assert paths == ["/api/projects/105/experiments/1/", "/api/projects/105/feature_flags/501/"]
    rollout = ph1.patches[1][1]["filters"]["multivariate"]["variants"]
    assert {v["key"]: v["rollout_percentage"] for v in rollout} == {"control": 0, "test": 100}


@pytest.mark.parametrize("answer", [{"approved": False, "approver": "rina"}, {"approved": True, "approver": "dana"},
                                    {"approver": "rina"}, "yes"])
def test_rejected_or_wrong_approver_writes_nothing(ph1, answer):
    ph1.patches.clear()
    g = build_graph(ph=ph1, model=FakeModel(good))
    run_until_approval(g, 1, M.NOW, f"t-{answer}")
    final = resume(g, f"t-{answer}", answer)
    assert final["concluded"] is None and ph1.patches == []


def test_invented_numbers_get_one_retry_then_the_template(ph1):
    g = build_graph(ph=ph1, model=FakeModel(invented, invented))
    state, _ = run_until_approval(g, 1, M.NOW, "t-template")
    assert state["first_try_passed"] is False and state["used_template"] is True
    assert R.check_readout(R.Readout(**state["readout"]), state["facts"]) == []


def test_retry_with_feedback_recovers(ph1):
    m = FakeModel(invented, good)
    g = build_graph(ph=ph1, model=m)
    state, _ = run_until_approval(g, 1, M.NOW, "t-retry")
    assert m.calls == 2 and state["used_template"] is False and state["first_try_passed"] is False


@pytest.mark.parametrize("eid,decision", [(3, "invalid"), (4, "keep_running")])
def test_invalid_or_unfinished_experiments_never_ask_to_conclude(eid, decision):
    ph = FakePostHog(eid)
    g = build_graph(ph=ph, model=FakeModel(good))
    state, ask = run_until_approval(g, eid, M.NOW, f"t-{eid}")
    assert ask is None and state["decision"]["decision"] == decision and ph.patches == []


def test_conversions_before_exposure_do_not_count():
    from posthog_agents.experiment_readout.queries import fetch
    d = fetch(FakePostHog(2), 2, M.NOW)
    assert d["metrics"][0]["conversions"] == {"control": 250, "test": 255}
    assert d["users"] == {"control": 2500, "test": 2500}
