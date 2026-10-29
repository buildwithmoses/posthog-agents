"""Experiment statistics, standard library only. Every decision is made here, never by the model.

- Bayesian beta-binomial, like PostHog: each variant's conversion rate gets a Beta(1 + conversions,
  1 + non-conversions) posterior. A seeded Monte Carlo gives the probability each variant is best, 95%
  credible intervals for each rate, and the lift of each variant over control.
- Frequentist two-proportion z-test, as a cross-check only.
- Sample ratio mismatch: chi-square goodness of fit against the planned split.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

DRAWS = 20_000
SEED = 7
PROB_THRESHOLD = 0.95          # a variant "wins" when P(best) >= 95%
SRM_P = 0.001                  # SRM when the chi-square p-value is below 0.001
MAX_MULTI_EXPOSURE = 0.05      # more than 5% of exposed users saw 2+ variants -> invalid
MIN_RUNTIME_DAYS = 14
MIN_USERS_PER_VARIANT = 1000
GUARDRAIL_PROB = 0.95          # guardrail regressed when P(winner worse than control) >= 95%


# ---------- distributions ----------

def _percentile(sorted_xs: list[float], q: float) -> float:
    if not sorted_xs:
        return float("nan")
    k = (len(sorted_xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_xs[lo] if lo == hi else sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (k - lo)


def posterior_draws(conversions: int, users: int, rng: random.Random, n: int = DRAWS) -> list[float]:
    a, b = 1 + conversions, 1 + max(users - conversions, 0)
    return [rng.betavariate(a, b) for _ in range(n)]


def norm_sf(z: float) -> float:
    """P(Z > z) for a standard normal."""
    return 0.5 * math.erfc(z / math.sqrt(2))


def two_proportion_z(c1: int, n1: int, c2: int, n2: int) -> tuple[float, float]:
    """Pooled two-proportion z-test of variant 2 vs 1. Returns (z, two-sided p-value)."""
    if min(n1, n2) == 0:
        return 0.0, 1.0
    p = (c1 + c2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0, 1.0
    z = (c2 / n2 - c1 / n1) / se
    return z, 2 * norm_sf(abs(z))


def _gammainc_upper_reg(s: float, x: float) -> float:
    """Regularized upper incomplete gamma Q(s, x) (Numerical Recipes: series below s+1, continued fraction above)."""
    if x <= 0:
        return 1.0
    gln = math.lgamma(s)
    if x < s + 1:
        term = total = 1.0 / s
        a = s
        for _ in range(1000):
            a += 1
            term *= x / a
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return max(0.0, 1.0 - total * math.exp(-x + s * math.log(x) - gln))
    b = x + 1 - s
    c = 1 / 1e-300
    d = 1 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - s)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-15:
            break
    return math.exp(-x + s * math.log(x) - gln) * h


def chi2_sf(x: float, df: int) -> float:
    return _gammainc_upper_reg(df / 2, x / 2)


def srm_test(observed: dict[str, int], planned_pct: dict[str, float]) -> tuple[float, float]:
    """Chi-square goodness of fit of observed users per variant vs the planned split. Returns (chi2, p)."""
    total = sum(observed.values())
    keys = list(planned_pct)
    if total == 0 or len(keys) < 2:
        return 0.0, 1.0
    weight = sum(planned_pct.values())
    chi2 = 0.0
    for k in keys:
        exp = total * planned_pct[k] / weight
        chi2 += (observed.get(k, 0) - exp) ** 2 / exp if exp else 0.0
    return chi2, chi2_sf(chi2, len(keys) - 1)


# ---------- per-metric analysis ----------

@dataclass
class VariantStat:
    key: str
    users: int
    conversions: int
    rate: float
    ci_low: float
    ci_high: float
    prob_best: float
    lift_mean: float | None = None      # relative lift vs control (0.30 = +30%)
    lift_ci_low: float | None = None
    lift_ci_high: float | None = None
    prob_beats_control: float | None = None
    z_vs_control: float | None = None
    p_vs_control: float | None = None


@dataclass
class MetricResult:
    name: str
    event: str
    goal: str                            # "increase" (primary) or "decrease" (guardrail)
    variants: list[VariantStat] = field(default_factory=list)

    def by_key(self) -> dict[str, VariantStat]:
        return {v.key: v for v in self.variants}


def analyze_metric(name: str, event: str, goal: str, users: dict[str, int], conversions: dict[str, int],
                   control: str = "control", seed: int = SEED) -> MetricResult:
    rng = random.Random(f"{seed}:{event}")
    keys = list(users)
    draws = {k: posterior_draws(conversions.get(k, 0), users[k], rng) for k in keys}
    better = (lambda a, b: a > b) if goal == "increase" else (lambda a, b: a < b)
    wins = {k: 0 for k in keys}
    for i in range(DRAWS):
        best = keys[0]
        for k in keys[1:]:
            if better(draws[k][i], draws[best][i]):
                best = k
        wins[best] += 1
    out = MetricResult(name, event, goal)
    for k in keys:
        s = sorted(draws[k])
        vs = VariantStat(k, users[k], conversions.get(k, 0), conversions.get(k, 0) / users[k] if users[k] else 0.0,
                         _percentile(s, 0.025), _percentile(s, 0.975), wins[k] / DRAWS)
        if k != control and control in draws:
            lifts = sorted((d - c) / c for d, c in zip(draws[k], draws[control]) if c > 0)
            vs.lift_mean = sum(lifts) / len(lifts)
            vs.lift_ci_low, vs.lift_ci_high = _percentile(lifts, 0.025), _percentile(lifts, 0.975)
            vs.prob_beats_control = sum(1 for d, c in zip(draws[k], draws[control]) if d > c) / DRAWS
            vs.z_vs_control, vs.p_vs_control = two_proportion_z(conversions.get(control, 0), users[control],
                                                                conversions.get(k, 0), users[k])
        out.variants.append(vs)
    return out


# ---------- the decision ----------

@dataclass
class Decision:
    decision: str            # ship | keep_control | keep_running | invalid
    variant: str | None      # the variant to roll out (ship -> winner, keep_control -> control), else None
    reason: str              # machine code
    explanation: str         # one plain sentence, from code
    checks: dict = field(default_factory=dict)


def decide(*, primary: MetricResult, guardrails: list[MetricResult], planned_pct: dict[str, float],
           multi_exposed: int, runtime_days: float, min_lift: float, control: str = "control",
           checks_enabled: bool = True) -> Decision:
    """Order matters: a broken experiment can't be read, an unfinished one isn't read yet, a winner must clear
    the guardrails and be worth shipping. `checks_enabled=False` is the regression build (no SRM, exposure,
    guardrail or minimum-lift checks)."""
    users = {v.key: v.users for v in primary.variants}
    total_exposed = sum(users.values()) + multi_exposed
    multi_share = multi_exposed / total_exposed if total_exposed else 0.0
    chi2, srm_p = srm_test(users, planned_pct)
    checks = {"multi_exposed": multi_exposed, "multi_exposed_share": multi_share, "srm_chi2": chi2, "srm_p": srm_p,
              "runtime_days": runtime_days, "min_users": min(users.values()) if users else 0}

    if checks_enabled and multi_share > MAX_MULTI_EXPOSURE:
        return Decision("invalid", None, "multi_variant_exposure",
                        f"{multi_exposed} users ({multi_share:.1%}) saw more than one variant, above the 5% limit, so the "
                        "groups are mixed and the result can't be trusted.", checks)
    if checks_enabled and srm_p < SRM_P:
        return Decision("invalid", None, "sample_ratio_mismatch",
                        "The groups are not the size the experiment planned (sample ratio mismatch), which usually "
                        "means a bug in assignment or tracking, so the result can't be trusted.", checks)
    if runtime_days < MIN_RUNTIME_DAYS or min(users.values()) < MIN_USERS_PER_VARIANT:
        why = []
        if runtime_days < MIN_RUNTIME_DAYS:
            why.append(f"it has run {runtime_days:.0f} of the minimum {MIN_RUNTIME_DAYS} days")
        if min(users.values()) < MIN_USERS_PER_VARIANT:
            why.append(f"the smallest group has {min(users.values())} of the minimum {MIN_USERS_PER_VARIANT} users")
        return Decision("keep_running", None, "not_enough_data", "Keep it running: " + " and ".join(why) + ".", checks)

    challengers = [v for v in primary.variants if v.key != control]
    winner = max(challengers, key=lambda v: v.prob_best) if challengers else None
    if winner is None or winner.prob_best < PROB_THRESHOLD:
        ctrl = primary.by_key().get(control)
        if ctrl and ctrl.prob_best >= PROB_THRESHOLD:
            return Decision("keep_control", control, "control_better", "Control converts better than every variant.", checks)
        return Decision("keep_control", control, "no_clear_winner",
                        f"No variant reached a {PROB_THRESHOLD:.0%} chance of being best, so there is no reliable "
                        "difference; keep control.", checks)

    if checks_enabled:
        for g in guardrails:
            gv = g.by_key().get(winner.key)
            # For a "decrease" metric, prob_beats_control = P(winner rate > control rate) = P(winner is worse).
            if gv and gv.prob_beats_control is not None and gv.prob_beats_control >= GUARDRAIL_PROB:
                checks["guardrail_failed"] = g.name
                return Decision("keep_control", control, "guardrail_regressed",
                                f"{winner.key} wins on {primary.name}, but {g.name} got worse, a guardrail the "
                                "experiment must not break; keep control.", checks)
        if winner.lift_mean is not None and winner.lift_mean < min_lift:
            return Decision("keep_control", control, "lift_below_minimum",
                            f"{winner.key} is very likely better, but the lift is below the {min_lift:.0%} minimum "
                            "worth shipping; keep control.", checks)
    return Decision("ship", winner.key, "clear_winner",
                    f"{winner.key} has a {winner.prob_best:.1%} chance of being best on {primary.name}"
                    + (" and no guardrail got worse." if checks_enabled else "."), checks)
