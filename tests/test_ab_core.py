"""SRM and the two-proportion z-test checked against independent scipy maths."""

import pytest
from scipy import stats

import ab_test_analyzer as aba


def test_srm_balanced_groups_is_clean():
    res = aba.srm_check(5000, 5000)

    assert res["chi2"] == pytest.approx(0.0, abs=1e-9)
    assert res["p_value"] == pytest.approx(1.0, abs=1e-9)
    assert res["srm_detected"] is False


def test_srm_detects_a_4pct_imbalance():
    res = aba.srm_check(4800, 5200)

    expected_p = stats.chi2.sf(16.0, df=1)
    assert res["chi2"] == pytest.approx(16.0, abs=1e-4)
    # The core rounds p to 6 decimals, so 1e-6 is the tightest honest bound.
    assert res["p_value"] == pytest.approx(expected_p, abs=1e-6)
    assert res["srm_detected"] is True


def test_ztest_p_value_matches_scipy():
    res = aba.analyze_binary_metric(1000, 100, 1000, 130)

    rate_c, rate_t = 0.10, 0.13
    pooled = (100 + 130) / 2000
    se = (pooled * (1 - pooled) * (1 / 1000 + 1 / 1000)) ** 0.5
    z = (rate_t - rate_c) / se

    assert res["z_score"] == pytest.approx(z, abs=1e-3)
    assert res["p_value"] == pytest.approx(2 * stats.norm.sf(abs(z)), abs=1e-6)
    assert res["relative_lift_pct"] == pytest.approx(30.0, abs=1e-6)
    assert res["significant"] is True
