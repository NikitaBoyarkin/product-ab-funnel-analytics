"""Drift gate for every number the README publishes.

The gate binds one direction through three layers:

    data/raw/*.csv  ->  recomputation  ->  artifact (analysis/ or sql/results/)  ->  README.md

Each test recomputes a published figure from the raw data, then asserts that the
committed artifact carries that value **and** that the README prints it. A number
that survives only as text in the README is not bound: change the data, re-run the
analysis, or edit the prose alone, and this file goes red.

Why this file exists: before it, only the funnel and the A/B counts were re-derived
by a test. LTV, RFM, cohort and channel figures were traced by hand — "the number
appears in some file" — which cannot fail for a wrong number.

Reuse, not reimplementation: the cohort SQL and the `retention_ltv` / `rfm` cores are
the same ones the analysis scripts call. The tests deliberately import them so a
gate never disagrees with production because someone rewrote the maths twice.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path

import duckdb
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
README = ROOT / "README.md"
COHORT_METRICS = ROOT / "sql" / "results" / "02_cohort_metrics.json"
RFM_METRICS = ROOT / "analysis" / "rfm_metrics.json"
LTV_METRICS = ROOT / "analysis" / "ltv_metrics.json"

RAW_INPUTS = ("users.csv", "events.csv", "transactions.csv")


def test_raw_inputs_are_present():
    """A drift gate over absent inputs proves nothing; fail loudly instead."""
    for name in RAW_INPUTS:
        path = RAW / name
        assert path.is_file(), f"missing raw input {path}"


def _load(name: str, rel: str):
    """Import an analysis script by path — `analysis/` and `sql/` are not packages."""
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cohorts():
    """Cohort retention matrix, recomputed from data/raw by the committed SQL."""
    module = _load("run_02_cohorts", "sql/run_02_cohorts.py")
    previous = Path.cwd()
    os.chdir(ROOT)  # the SQL reads data/raw/*.csv by relative path
    try:
        queries = module.load_queries()
        con = duckdb.connect()
        try:
            activity = module.run_query(con, queries["activity_retention"])
            paid = module.run_query(con, queries["paid_retention"])
        finally:
            con.close()
        return (
            module,
            module.headline(activity, paid),
            json.loads(COHORT_METRICS.read_text(encoding="utf-8")),
        )
    finally:
        os.chdir(previous)


@pytest.fixture(scope="module")
def rfm():
    """RFM metrics, recomputed from data/raw by the committed core."""
    module = _load("run_03_rfm", "analysis/run_03_rfm.py")
    txn = module.load_transactions()
    scored, facts = module.build_scored(txn)
    metrics, _, _ = module.build_metrics(scored, facts)
    return metrics, json.loads(RFM_METRICS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ltv():
    """LTV economics and fit, recomputed from data/raw and sql/04_ltv.sql."""
    module = _load("run_04_ltv", "analysis/run_04_ltv.py")
    previous = Path.cwd()
    os.chdir(ROOT)
    try:
        users, txn = module.load_raw()
        points_df = module.retention_points_from_sql()
    finally:
        os.chdir(previous)

    points = [
        {"t": int(row.t), "r": float(row.retention)} for row in points_df.itertuples()
    ]
    fit = module.rl.fit_power_law(points)

    charges = txn[~txn["is_refund"]].copy()
    charges["cal_month"] = charges["transaction_date"].dt.to_period("M")
    paid_months = module.paid_cohort_frame(users, txn)[
        ["user_id", "pay_month"]
    ].dropna()
    active = charges.groupby(["user_id", "cal_month"]).size().rename("n").reset_index()
    active = active.merge(paid_months, on="user_id", how="inner")
    active["t"] = (active["cal_month"] - active["pay_month"]).apply(lambda x: x.n) + 1

    economics = {
        "signups": int(users["user_id"].nunique()),
        "payers": int(paid_months["user_id"].nunique()),
        "active_payer_months": int(len(active)),
        "net_revenue_rub": round(float(txn["amount"].sum()), 2),
        "gross_revenue_rub": round(float(charges["amount"].sum()), 2),
        "cac_mean_rub": round(float(users["acquisition_cost"].mean()), 2),
    }
    arpu = economics["net_revenue_rub"] / economics["active_payer_months"]
    conv = economics["payers"] / economics["signups"]
    report = module.rl.run_ltv(
        retention_points=points,
        cohort_size=float(economics["signups"]),
        arpu_per_period=arpu,
        horizon=12,
        period="month",
        funnel=[{"label": "signup → first paid", "conversion_pct": conv * 100.0}],
        cac=economics["cac_mean_rub"],
    )

    # per-channel LTV/CAC, following run_04_ltv.py's own construction
    channel = (
        users.groupby("channel")
        .agg(signups=("user_id", "nunique"), cac_rub=("acquisition_cost", "mean"))
        .reset_index()
    )
    payers = (
        paid_months.merge(users[["user_id", "channel"]], on="user_id", how="left")
        .groupby("channel")["user_id"]
        .nunique()
    )
    channel["payers"] = channel["channel"].map(payers).fillna(0).astype(int)
    channel["conv"] = channel["payers"] / channel["signups"]
    channel["ltv_cac"] = (channel["conv"] * report.ltv_per_paid) / channel["cac_rub"]

    return {
        "module": module,
        "points": points,
        "fit": fit,
        "economics": economics,
        "report": report,
        "channel": channel.set_index("channel"),
        "artifact": json.loads(LTV_METRICS.read_text(encoding="utf-8")),
    }


def readme_text() -> str:
    return README.read_text(encoding="utf-8")


def publishes(text: str, value: float, decimals: int = 2) -> bool:
    """True if the README prints this value as its own number, not inside a larger one.

    Bare substring matching is what made the ratchet's claims gate unfalsifiable:
    `1.72` matched inside `541.72`. Token boundaries close that hole.
    """
    rendered = f"{value:.{decimals}f}"
    return re.search(rf"(?<![\d.]){re.escape(rendered)}(?![\d])", text) is not None


# --------------------------------------------------------------------- cohorts
def test_cohort_retention_reproduces_from_raw_data(cohorts):
    _, fresh, artifact = cohorts
    committed = artifact["headline"]["activity_pooled"]

    for month in ("M1", "M3", "M6", "M11"):
        assert fresh["activity_pooled"][month]["pct"] == pytest.approx(
            committed[month]["pct"], abs=1e-6
        ), f"cohort {month} retention drifted from the committed artifact"


def test_cohort_numbers_are_published(cohorts):
    _, fresh, _ = cohorts
    text = readme_text()
    for month in ("M1", "M3", "M6", "M11"):
        value = fresh["activity_pooled"][month]["pct"]
        assert publishes(text, value), (
            f"README must publish cohort {month} retention {value:.2f}, "
            "which the SQL reproduces from data/raw"
        )


# ------------------------------------------------------------------------- RFM
def test_rfm_metrics_reproduce_from_raw_data(rfm):
    fresh, artifact = rfm
    for field in (
        "users_scored",
        "segments_count",
        "gini",
        "top10pct_revenue_share_pct",
        "top20pct_revenue_share_pct",
        "champions_revenue_share_pct",
        "top_revenue_segment_share_pct",
    ):
        assert fresh[field] == pytest.approx(artifact[field], abs=1e-9), (
            f"RFM metric {field} drifted from the committed artifact"
        )


def test_rfm_concentration_is_published(rfm):
    fresh, _ = rfm
    text = readme_text()

    assert publishes(text, float(fresh["gini"]), 3), "README must publish Gini"
    assert publishes(text, fresh["top20pct_revenue_share_pct"]), (
        "README must publish the top-20% revenue share"
    )
    assert fresh["users_scored"] == 3384
    assert str(fresh["users_scored"]) in text, "README must publish the payer count"


# ------------------------------------------------------------------------- LTV
def test_ltv_retention_curve_reproduces_from_raw_data(ltv):
    committed = ltv["artifact"]["retention_points"]
    assert len(ltv["points"]) == len(committed)
    for fresh_point, stored in zip(ltv["points"], committed):
        assert fresh_point["t"] == stored["t"]
        assert fresh_point["r"] == pytest.approx(stored["r"], abs=5e-5), (
            f"retention at t={fresh_point['t']} drifted"
        )


def test_ltv_fit_reproduces_from_the_curve(ltv):
    committed = ltv["artifact"]["fit"]
    for field in ("a", "b", "r_squared"):
        assert getattr(ltv["fit"], field) == pytest.approx(
            committed[field], abs=1e-4
        ), f"power-law fit {field} drifted"


def test_ltv_economics_reproduce_from_raw_data(ltv):
    committed = ltv["artifact"]["economics"]
    for field, fresh in ltv["economics"].items():
        assert fresh == pytest.approx(committed[field], abs=0.01), (
            f"LTV economics {field} drifted"
        )


def test_ltv_headline_reproduces(ltv):
    committed = ltv["artifact"]["headline_12m"]
    assert ltv["report"].ltv_per_acquired == pytest.approx(
        committed["ltv_per_acquired_rub"], abs=0.01
    )
    assert ltv["report"].ltv_per_paid == pytest.approx(
        committed["ltv_per_paid_rub"], abs=0.01
    )


def test_ltv_headline_is_published(ltv):
    text = readme_text()
    committed = ltv["artifact"]["headline_12m"]
    economics = ltv["artifact"]["economics"]

    for label, value in (
        ("LTV per acquired", committed["ltv_per_acquired_rub"]),
        ("LTV per payer", committed["ltv_per_paid_rub"]),
        ("CAC", economics["cac_mean_rub"]),
    ):
        assert publishes(text, value), f"README must publish {label} = {value:.2f}"


# --------------------------------------------------------------------- channel
def test_channel_ltv_cac_reproduces_and_is_published(ltv):
    text = readme_text()
    artifact = {row["channel"]: row for row in ltv["artifact"]["by_channel"]}

    for name in ("referral", "paid_search"):
        fresh = float(ltv["channel"].loc[name, "ltv_cac"])
        assert fresh == pytest.approx(artifact[name]["ltv_cac"], abs=0.01), (
            f"channel LTV/CAC for {name} drifted from the artifact"
        )
        assert publishes(text, round(fresh, 2)), (
            f"README must publish LTV/CAC for {name} = {fresh:.2f}"
        )
