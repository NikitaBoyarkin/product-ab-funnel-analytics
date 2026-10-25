"""Gini/Lorenz invariants — the numbers behind "top-20% give 50.7% of revenue"."""

import pandas as pd
import pytest

import rfm


def frame(values):
    return pd.DataFrame({"monetary_value": values})


def test_uniform_revenue_gives_zero_gini():
    gini, _, _ = rfm.lorenz_gini(frame([100.0] * 100))

    assert gini == pytest.approx(0.0, abs=1e-9)


def test_skewed_revenue_gives_concentrated_gini():
    gini, cum_customers, cum_revenue = rfm.lorenz_gini(
        frame(list(range(1, 100)) + [10000.0])
    )

    assert gini > 0.75
    assert cum_customers[0] == 0.0 and cum_revenue[0] == 0.0
    assert cum_customers[-1] == pytest.approx(1.0)
    assert cum_revenue[-1] == pytest.approx(1.0)


def test_empty_frame_is_zero_not_a_division_error():
    gini, _, _ = rfm.lorenz_gini(frame([]))

    assert gini == 0.0
