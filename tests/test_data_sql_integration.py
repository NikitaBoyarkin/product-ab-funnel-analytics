"""Raw CSVs → SQL → committed artifacts: the drift gate for every README number."""

from pathlib import Path

import duckdb
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "sql" / "results"


@pytest.fixture(scope="module")
def con():
    connection = duckdb.connect()
    connection.execute(f"SET file_search_path = '{ROOT}'")
    yield connection
    connection.close()


def run(con, sql_file):
    sql = (ROOT / "sql" / sql_file).read_text(encoding="utf-8")
    return con.execute(sql).df()


def test_funnel_sql_reproduces_the_committed_counts(con):
    sql_df = run(con, "05_funnel.sql")
    overall = sql_df[sql_df["scope_type"] == "overall"].set_index("step")["users"].astype(int)
    golden = pd.read_csv(RESULTS / "05_funnel_steps.csv")
    with_renew = golden[golden["funnel"] == "with_renew"]
    golden_overall = with_renew.set_index("step")["users"].astype(int)

    assert overall.to_dict() == golden_overall.to_dict()
    assert overall["signup"] == 12000

    shorter = golden[golden["funnel"] == "main"].set_index("step")["users"].astype(int)
    assert shorter.to_dict() == overall.drop("renew").to_dict()


def test_ab_sql_reproduces_the_committed_counts(con):
    fresh = run(con, "06_ab.sql").set_index("experiment_group").sort_index()
    golden = pd.read_csv(RESULTS / "06_ab_counts.csv").set_index("experiment_group").sort_index()

    assert list(fresh.index) == list(golden.index)
    for column in ("users", "paid_given_activated_num", "paid_given_activated_den"):
        assert fresh[column].tolist() == pytest.approx(golden[column].tolist())


def test_ab_artifact_reconciles_with_its_counts():
    ab = pd.read_json(ROOT / "analysis" / "ab_metrics.json", typ="series")
    counts = pd.read_csv(RESULTS / "06_ab_counts.csv").set_index("experiment_group")

    primary = ab["primary"]
    control = counts.loc["control"]
    treatment = counts.loc["treatment"]

    assert primary["rate_control"] == pytest.approx(
        control["paid_given_activated_num"] / control["paid_given_activated_den"],
        abs=1e-6,
    )
    assert primary["rate_treatment"] == pytest.approx(
        treatment["paid_given_activated_num"] / treatment["paid_given_activated_den"],
        abs=1e-6,
    )
