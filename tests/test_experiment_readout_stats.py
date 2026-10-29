"""Statistics and decisions, checked against hand-computed values. No network, no keys."""

import math

import pytest

from posthog_agents.experiment_readout import stats as S
from posthog_agents.experiment_readout.mock_data import SPECS


def test_two_proportion_z_matches_hand_calculation():
    # pooled p = 690/6000 = 0.115; se = sqrt(0.115*0.885*(2/3000)) = 0.0082371; z = 0.03/0.0082371 = 3.642
    z, p = S.two_proportion_z(300, 3000, 390, 3000)
    assert z == pytest.approx(3.642, abs=1e-3)
    assert p == pytest.approx(2 * 0.5 * math.erfc(3.642 / math.sqrt(2)), rel=1e-3)
    assert p < 0.001


@pytest.mark.parametrize("x,df,expected", [(3.841459, 1, 0.05), (6.634897, 1, 0.01), (5.991465, 2, 0.05),
                                            (7.814728, 3, 0.05), (0.0, 1, 1.0), (1.0, 1, 0.3173105)])
def test_chi2_survival_matches_tables(x, df, expected):
    assert S.chi2_sf(x, df) == pytest.approx(expected, abs=1e-6)


def test_srm_detects_3000_vs_2400_on_a_50_50_split():
    # expected 2700 each: chi2 = 2 * 300^2 / 2700 = 66.667
    chi2, p = S.srm_test({"control": 3000, "test": 2400}, {"control": 50, "test": 50})
    assert chi2 == pytest.approx(66.667, abs=1e-3)
    assert p < 1e-10
    _, p_ok = S.srm_test({"control": 2000, "test": 1940, "x": 1940}, {"control": 34, "test": 33, "x": 33})
    assert p_ok > 0.5


def test_beta_posterior_interval_matches_normal_approximation():
    # Beta(301, 2701): mean 0.10027, sd 0.005482 -> 95% interval about [0.0895, 0.1110]
    m = S.analyze_metric("m", "e", "increase", {"control": 3000}, {"control": 300})
    v = m.variants[0]
    assert v.ci_low == pytest.approx(0.0895, abs=0.001)
    assert v.ci_high == pytest.approx(0.1110, abs=0.001)


def test_probability_best_matches_normal_approximation():
    # 10.0% vs 10.5% on 3,000 each: z = 0.005 / sqrt(0.09/3000 + 0.094/3000) = 0.6385 -> P(test best) = 0.738
    m = S.analyze_metric("m", "e", "increase", {"control": 3000, "test": 3000}, {"control": 300, "test": 315})
    t = m.by_key()["test"]
    assert t.prob_best == pytest.approx(0.738, abs=0.012)
    assert t.prob_best + m.by_key()["control"].prob_best == pytest.approx(1.0)
    assert t.lift_mean == pytest.approx(0.05, abs=0.01)


def test_monte_carlo_is_reproducible():
    a = S.analyze_metric("m", "e", "increase", {"control": 500, "test": 500}, {"control": 50, "test": 60})
    b = S.analyze_metric("m", "e", "increase", {"control": 500, "test": 500}, {"control": 50, "test": 60})
    assert a.by_key()["test"].prob_best == b.by_key()["test"].prob_best


def _decide_from_spec(spec, checks=True, runtime=None):
    users = {k: sum(s["n"] for s in segs) for k, segs in spec["segments"].items()}
    conv = lambda ev: {k: sum(s["conv"].get(ev, 0) for s in segs) for k, segs in spec["segments"].items()}  # noqa: E731
    primary = S.analyze_metric(spec["primary"][0], spec["primary"][1], "increase", users, conv(spec["primary"][1]))
    guards = [S.analyze_metric(n, e, "decrease", users, conv(e)) for n, e in spec["guardrails"]]
    days = runtime if runtime is not None else (spec["start"] - (spec["end"] or 0))
    return S.decide(primary=primary, guardrails=guards, planned_pct=dict(spec["variants"]), multi_exposed=spec.get("multi", 0),
                    runtime_days=days, min_lift=spec["mde"] / 100, checks_enabled=checks)


EXPECTED = {1: ("ship", "test"), 2: ("keep_control", "control"), 3: ("invalid", None), 4: ("keep_running", None),
            5: ("keep_control", "control"), 6: ("ship", "variant_b"), 7: ("keep_control", "control"),
            8: ("invalid", None), 9: ("keep_control", "control")}


@pytest.mark.parametrize("spec", SPECS, ids=[s["key"] for s in SPECS])
def test_code_decision_for_every_fixture(spec):
    d = _decide_from_spec(spec)
    assert (d.decision, d.variant) == EXPECTED[spec["id"]], d.explanation


def test_reasons_are_specific():
    by_id = {s["id"]: s for s in SPECS}
    assert _decide_from_spec(by_id[3]).reason == "sample_ratio_mismatch"
    assert _decide_from_spec(by_id[8]).reason == "multi_variant_exposure"
    assert _decide_from_spec(by_id[5]).reason == "guardrail_regressed"
    assert _decide_from_spec(by_id[9]).reason == "lift_below_minimum"
    assert _decide_from_spec(by_id[4]).reason == "not_enough_data"


def test_regression_build_without_checks_ships_broken_experiments():
    by_id = {s["id"]: s for s in SPECS}
    assert _decide_from_spec(by_id[3], checks=False).decision == "ship"
    assert _decide_from_spec(by_id[8], checks=False).decision == "ship"
    assert _decide_from_spec(by_id[5], checks=False).decision == "ship"
    assert _decide_from_spec(by_id[9], checks=False).decision == "ship"


def test_a_winner_still_waits_for_the_minimum_runtime():
    by_id = {s["id"]: s for s in SPECS}
    assert _decide_from_spec(by_id[1], runtime=5).decision == "keep_running"
