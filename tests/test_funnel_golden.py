"""Golden check on the committed funnel artifact: shape, maths and drop-off."""

from pathlib import Path

import pandas as pd
import pytest

STEPS_CSV = Path(__file__).resolve().parent.parent / "sql" / "results" / "05_funnel_steps.csv"


@pytest.fixture(scope="module")
def main_funnel():
    df = pd.read_csv(STEPS_CSV)
    df = df[df["funnel"] == "main"].sort_values("step_index").reset_index(drop=True)
    assert len(df) >= 3, "main funnel must have at least signup/activation/paid"
    return df


def test_users_never_grow_down_the_funnel(main_funnel):
    users = main_funnel["users"].tolist()
    assert users == sorted(users, reverse=True)


def test_conversions_reconcile_with_counts(main_funnel):
    signup = main_funnel["users"].iloc[0]
    prev = signup
    for row in main_funnel.itertuples():
        if row.step_index == 1:
            assert row.step_conversion == pytest.approx(1.0, abs=1e-6)
            assert row.overall_conversion == pytest.approx(1.0, abs=1e-6)
        else:
            assert row.step_conversion == pytest.approx(row.users / prev, abs=1e-4)
            assert row.overall_conversion == pytest.approx(row.users / signup, abs=1e-4)
            assert row.dropoff_n == prev - row.users
        prev = row.users


def test_headline_counts_match_the_readme():
    df = pd.read_csv(STEPS_CSV)
    main = df[df["funnel"] == "main"].set_index("step")["users"]

    assert main["signup"] == 12000
    assert main["activation"] == 9623
    assert main["paid_subscription"] == 3384
