"""Power-law fit and payback conventions behind the LTV/CAC headline."""

import json
from pathlib import Path

import pytest

import retention_ltv as rl

LTV_METRICS = Path(__file__).resolve().parent.parent / "analysis" / "ltv_metrics.json"


def power_law_points(a=0.5, b=0.6, horizon=12):
    return [{"t": t, "r": a * t**-b} for t in range(1, horizon + 1)]


def test_fit_recovers_known_curve():
    fit = rl.fit_power_law(power_law_points())

    assert fit.a == pytest.approx(0.5, abs=1e-6)
    assert fit.b == pytest.approx(0.6, abs=1e-6)
    assert fit.r_squared > 0.999


def test_payback_returns_first_period_covering_cac():
    fit = rl.fit_power_law(power_law_points())
    series = rl.cohort_ltv(
        fit=fit,
        acquired_at_zero=1000.0,
        arpu_per_period=500.0,
        cohort_size=1000.0,
        horizon=12,
    )

    target = series[4]
    assert rl.payback(series, cac=target.cum_ltv_per_cohort) == target.t


def test_payback_conventions_for_undefined_and_zero_cac():
    fit = rl.fit_power_law(power_law_points())
    series = rl.cohort_ltv(
        fit=fit,
        acquired_at_zero=1000.0,
        arpu_per_period=500.0,
        cohort_size=1000.0,
        horizon=12,
    )

    assert rl.payback(series, cac=None) is None
    assert rl.payback(series, cac=0) == 1
    unattainable = series[-1].cum_ltv_per_cohort * 10
    assert rl.payback(series, cac=unattainable) is None


def test_published_effect_is_the_measured_itt_lift_not_the_conditional_one():
    metrics = json.loads(LTV_METRICS.read_text(encoding="utf-8"))
    baseline = metrics["sensitivities"]["baseline"]["ltv_per_acquired_12m"]
    measured = metrics["sensitivities"]["conv_uplift_itt_measured"]
    conditional = metrics["sensitivities"]["conv_uplift_ab_+19.6pct"]
    itt_pct = metrics["iso_magnitude"]["ab_itt_uplift_pct"]

    assert measured["lift_pct"] == pytest.approx(itt_pct)
    assert measured["ltv_per_acquired_12m"] == pytest.approx(
        baseline * (1 + itt_pct / 100), abs=0.02
    )
    assert measured["ltv_per_acquired_12m"] < conditional["ltv_per_acquired_12m"]
