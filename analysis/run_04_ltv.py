#!/usr/bin/env python
"""LTV leg (Leg 04: `ltv`) for product-ab-funnel-analytics.

Produces:
  * `data/processed/retention_points.csv` — (t, retention) points fed to the fit
  * `data/processed/ltv_by_cohort.csv`    — cohort LTV table (from the core)
  * `analysis/ltv_metrics.json`           — every number cited in 04-ltv.md
  * `figures/retention-fit.png`           — observed points + fitted curve + band

Run:
    uv run --project "<env>" python analysis/run_04_ltv.py
    uv run --project "<env>" python analysis/run_04_ltv.py --no-figures

Design notes:
  * the retention-by-period input is produced by `sql/04_ltv.sql` (DuckDB) — the
    SQL is the source of truth for the input points, this script consumes it;
  * all curve/LTV/payback math is DELEGATED to the zero-dep core
    `retention-ltv-calculator/scripts/retention_ltv.py` (fit_power_law,
    cohort_ltv, cohort_ltv_band, payback, run_ltv) — nothing is reimplemented;
  * every number in docs/insights/04-ltv.md comes from here (reproducible).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import duckdb
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_DIR / "data" / "raw"
PROC_DIR = PROJECT_DIR / "data" / "processed"
SQL_DIR = PROJECT_DIR / "sql"
FIG_DIR = PROJECT_DIR / "figures"
OUT_DIR = PROJECT_DIR / "analysis"

# Zero-dep core (delegated math). A vendored copy ships in vendor/ so a clean
# clone runs; point DSWOK_SKILLS_ROOT at a DSWoK checkout to use the live one.
SKILLS_ROOT = os.environ.get("DSWOK_SKILLS_ROOT")
CORE_DIR = (
    Path(SKILLS_ROOT) / "retention-ltv-calculator" / "scripts"
    if SKILLS_ROOT
    else PROJECT_DIR / "vendor"
)
sys.path.insert(0, str(CORE_DIR))
import retention_ltv as rl  # noqa: E402

WINDOW_END = pd.Period("2024-12", freq="M")
HORIZON_PRIMARY = 12  # in-window; every cohort contributes what it can observe
HORIZON_EXTRAP = 24  # one year past the window (power-law extrapolation)

DTYPES_USERS = {
    "user_id": "str",
    "signup_date": "str",
    "channel": "str",
    "acquisition_cost": "float64",
}
DTYPES_TXN = {"user_id": "str", "transaction_id": "str", "is_refund": "str"}


# ---------------------------------------------------------------- load
def load_raw() -> tuple[pd.DataFrame, pd.DataFrame]:
    users = pd.read_csv(
        RAW_DIR / "users.csv", dtype=DTYPES_USERS, parse_dates=["signup_date"]
    )
    txn = pd.read_csv(
        RAW_DIR / "transactions.csv", dtype=DTYPES_TXN, parse_dates=["transaction_date"]
    )
    txn["is_refund"] = txn["is_refund"].map({"True": True, "False": False})
    return users, txn


# ---------------------------------------------------------------- retention input (SQL)
def retention_points_from_sql() -> pd.DataFrame:
    """Run sql/04_ltv.sql → (period t, active_users, eligible_users, retention)."""
    sql = (SQL_DIR / "04_ltv.sql").read_text()
    con = duckdb.connect()
    con.execute(f"SET file_search_path = '{PROJECT_DIR}'")
    df = con.sql(sql).df()
    return df.rename(columns={"period": "t"})


# ---------------------------------------------------------------- paid cohorts
def paid_cohort_frame(users: pd.DataFrame, txn: pd.DataFrame) -> pd.DataFrame:
    """user → first-payment month (paid cohort)."""
    charges = txn[~txn["is_refund"]].copy()
    first = (
        charges.groupby("user_id")["transaction_date"]
        .min()
        .rename("first_pay")
        .reset_index()
    )
    m = users.merge(first, on="user_id", how="left")
    m["pay_month"] = m["first_pay"].dt.to_period("M")
    return m


def observed_cohort_ltv(
    users: pd.DataFrame, txn: pd.DataFrame, m: pd.DataFrame
) -> pd.DataFrame:
    """Per paid cohort: observed net revenue and LTV per payer (in-window)."""
    txn[~txn["is_refund"]].copy()
    net = (
        txn.groupby("user_id", as_index=False)["amount"]
        .sum()
        .rename(columns={"amount": "net_revenue"})
    )
    pm = m[["user_id", "pay_month"]].dropna().merge(net, on="user_id", how="left")
    agg = (
        pm.groupby("pay_month")
        .agg(
            cohort_payers=("user_id", "nunique"), net_revenue_rub=("net_revenue", "sum")
        )
        .reset_index()
    )
    agg["months_observed"] = agg["pay_month"].apply(
        lambda p: 12 - (p.ordinal - pd.Period("2024-01", freq="M").ordinal)
    )
    agg["observed_ltv_per_payer_rub"] = agg["net_revenue_rub"] / agg["cohort_payers"]
    agg["censored"] = agg["months_observed"] < 12
    return agg


# ---------------------------------------------------------------- main
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-figures", action="store_true")
    args = parser.parse_args()

    users, txn = load_raw()
    pts_df = retention_points_from_sql()
    pts_df.to_csv(PROC_DIR / "retention_points.csv", index=False)

    points = [{"t": int(r.t), "r": float(r.retention)} for r in pts_df.itertuples()]

    # --- fit (core) ---------------------------------------------------------
    fit = rl.fit_power_law(points)

    # --- economics ----------------------------------------------------------
    charges = txn[~txn["is_refund"]].copy()
    charges["cal_month"] = charges["transaction_date"].dt.to_period("M")
    m = paid_cohort_frame(users, txn)
    pm = m[["user_id", "pay_month"]].dropna()
    active_months = (
        charges.groupby(["user_id", "cal_month"]).size().rename("n").reset_index()
    )
    act = active_months.merge(pm, on="user_id", how="inner")
    act["t"] = (act["cal_month"] - act["pay_month"]).apply(lambda x: x.n) + 1
    active_payer_months = int(len(act))

    net_revenue = float(txn["amount"].sum())
    gross_revenue = float(charges["amount"].sum())
    arpu_per_period = net_revenue / active_payer_months  # ₽ per active payer-month

    n_signups = int(users["user_id"].nunique())
    n_payers = int(pm["user_id"].nunique())
    conv = n_payers / n_signups
    cac_mean = float(users["acquisition_cost"].mean())
    cac_median = float(users["acquisition_cost"].median())

    # --- headline LTV (core run_ltv) ---------------------------------------
    funnel = [{"label": "signup → first paid", "conversion_pct": conv * 100.0}]

    def headline(horizon: int):
        return rl.run_ltv(
            retention_points=points,
            cohort_size=float(n_signups),
            arpu_per_period=arpu_per_period,
            horizon=horizon,
            period="month",
            funnel=funnel,
            cac=cac_mean,
        )

    rep12 = headline(HORIZON_PRIMARY)
    rep24 = headline(HORIZON_EXTRAP)

    # --- per-cohort LTV table (core cohort_ltv per cohort) ------------------
    coh = observed_cohort_ltv(users, txn, m)
    rows = []
    for r in coh.itertuples():
        h = int(r.months_observed)
        series = rl.cohort_ltv(
            fit=fit,
            acquired_at_zero=float(r.cohort_payers),
            arpu_per_period=arpu_per_period,
            cohort_size=float(r.cohort_payers),
            horizon=h,
        )
        model_per_payer = series[-1].cum_ltv_per_acquired
        rows.append(
            {
                "cohort": str(r.pay_month),
                "cohort_payers": int(r.cohort_payers),
                "months_observed": h,
                "observed_net_revenue_rub": round(float(r.net_revenue_rub), 2),
                "observed_ltv_per_payer_rub": round(
                    float(r.observed_ltv_per_payer_rub), 2
                ),
                "model_ltv_per_payer_rub": round(model_per_payer, 2),
                "model_ltv_per_acquired_rub": round(model_per_payer * conv, 2),
                "censored": bool(r.censored),
            }
        )
    cohort_df = pd.DataFrame(rows)
    cohort_df.to_csv(PROC_DIR / "ltv_by_cohort.csv", index=False)

    # --- per-channel LTV/CAC ------------------------------------------------
    chan = (
        users.groupby("channel")
        .agg(signups=("user_id", "nunique"), cac_rub=("acquisition_cost", "mean"))
        .reset_index()
    )
    payers_ch = m.dropna(subset=["pay_month"]).groupby("channel")["user_id"].nunique()
    chan["payers"] = chan["channel"].map(payers_ch).fillna(0).astype(int)
    chan["conv"] = chan["payers"] / chan["signups"]
    # LTV per acquired = conv × LTV per paid (12-month, core fit)
    ltv_per_paid_12 = rep12.ltv_per_paid
    chan["ltv_per_acquired_rub"] = chan["conv"] * ltv_per_paid_12
    chan["ltv_cac"] = chan["ltv_per_acquired_rub"] / chan["cac_rub"]
    chan["payback_months"] = [
        rl.payback(
            rl.cohort_ltv(
                fit=fit,
                acquired_at_zero=float(n_signups) * c,
                arpu_per_period=arpu_per_period,
                cohort_size=float(n_signups),
                horizon=HORIZON_PRIMARY,
            ),
            float(cac),
        )
        for c, cac in zip(chan["conv"], chan["cac_rub"], strict=False)
    ]

    # --- sensitivities ------------------------------------------------------
    # Pooled signup→paid conversion (computed above) × the A/B relative lift
    # read from the A/B leg's artifact — no hardcoded conversion or uplift.
    ab_metrics_path = OUT_DIR / "ab_metrics.json"
    ab_metrics = json.loads(ab_metrics_path.read_text(encoding="utf-8"))
    ab_rel_lift_pct = float(ab_metrics["primary"]["relative_lift_pct"])  # 19.59
    # Measured on `paid per signup` (ITT): the conditional lift diluted by the
    # activation drop in treatment. This is what a rollout actually books.
    ab_itt_lift_pct = float(ab_metrics["robustness_itt"]["relative_lift_pct"])  # 16.41
    conv_treat = conv * (1 + ab_rel_lift_pct / 100.0)

    def ltv_acq(conv_override: float, b_mult: float = 1.0) -> float:
        f = rl.PowerLawFit(
            a=fit.a, b=fit.b * b_mult, r_squared=fit.r_squared, se=fit.se, n=fit.n
        )
        ser = rl.cohort_ltv(
            fit=f,
            acquired_at_zero=n_signups * conv_override,
            arpu_per_period=arpu_per_period,
            cohort_size=float(n_signups),
            horizon=HORIZON_PRIMARY,
        )
        return ser[-1].cum_ltv_per_cohort

    sensitivities = {
        "baseline": {
            "conv": round(conv, 4),
            "ltv_per_acquired_12m": round(ltv_acq(conv), 2),
        },
        "conv_uplift_ab_+19.6pct": {
            "conv": round(conv_treat, 4),
            "ltv_per_acquired_12m": round(ltv_acq(conv_treat), 2),
        },
        # The measured effect. The entry above extrapolates the *conditional*
        # `paid given activated` lift to every signup, which overstates it:
        # activation itself was lower in treatment, so `paid per signup` moved
        # +16.41%, not +19.59%. Cite this one as the effect of a rollout.
        "conv_uplift_itt_measured": {
            "conv": round(conv * (1 + ab_itt_lift_pct / 100.0), 4),
            "ltv_per_acquired_12m": round(
                ltv_acq(conv * (1 + ab_itt_lift_pct / 100.0)), 2
            ),
            "lift_pct": round(ab_itt_lift_pct, 2),
        },
        # Decay-exponent shocks. NOTE: b×0.9 is NOT a "+10% retention"
        # scenario — it is "the tail decays 10% slower", which lifts the
        # curve by only ~2..8% pointwise over months 2..12. Named for what
        # it is; the equal-step "+10% retention" comparison lives in
        # `iso_magnitude` below.
        "retention_decay_b_+10pct": {  # faster decay
            "ltv_per_acquired_12m": round(ltv_acq(conv, 1.10), 2),
        },
        "retention_decay_b_-10pct": {  # slower decay
            "ltv_per_acquired_12m": round(ltv_acq(conv, 0.90), 2),
        },
    }

    # --- iso-magnitude lever comparison (B39) -------------------------------
    # The A/B uplift (+19.59% relative on activation→paid) and a retention
    # change are different-size levers. Comparing them head-on ("conversion is
    # N× stronger") only measures the step sizes: 19.59% vs a decay shock that
    # is ~+5% on the curve over 12 months. Both levers are therefore
    # normalised to the same relative step (+10%) before being compared.
    #
    # Conversion is exactly multiplicative in LTV per acquired:
    #     LTV = conv × Σ_t ARPU · R(t)
    # so a +10% relative step on conv is +10.0% on LTV — no re-integration.
    #
    # Retention cannot lift month 1: R(1) is already the entire paying cohort
    # (t = month of the first payment). A "+10% retention curve" therefore
    # scales R(t) for t ≥ 2 only and is capped at R(1), which makes it
    # sub-linear in LTV. `ltv_acq_retention` mirrors `rl.cohort_ltv`'s revenue
    # formula (same ARPU, same numerator/denominator) with that one multiplier
    # added; it is asserted to reproduce the baseline to the kopeck at mult=1.
    r1 = rl.predict(1, fit)

    def ltv_acq_retention(mult: float, conv_override: float = conv) -> float:
        cum = 0.0
        for t in range(1, HORIZON_PRIMARY + 1):
            r = r1 if t == 1 else min(r1, mult * rl.predict(t, fit))
            cum += n_signups * conv_override * r * arpu_per_period
        return cum / n_signups

    ISO_STEP_PCT = 10.0
    iso_mult = 1.0 + ISO_STEP_PCT / 100.0

    base_ltv = ltv_acq(conv)
    if abs(ltv_acq_retention(1.0) - base_ltv) > 0.01:
        raise AssertionError(
            "ltv_acq_retention(1.0) must reproduce the core baseline "
            f"({ltv_acq_retention(1.0):.2f} vs {base_ltv:.2f})"
        )

    conv_step_ltv = ltv_acq(conv * iso_mult)
    ret_step_ltv = ltv_acq_retention(iso_mult)
    conv_step_delta_pct = (conv_step_ltv / base_ltv - 1.0) * 100.0
    ret_step_delta_pct = (ret_step_ltv / base_ltv - 1.0) * 100.0

    # Retention multiplier that would deliver the A/B's measured conversion
    # lift. LTV(m) is affine in m above the cap (month-1 revenue is untouched),
    # so solve it in closed form: LTV(m) = month1 + m × (base − month1).
    month1_ltv = ltv_acq_retention(0.0)
    ret_mult_for_ab = 1.0 + (ab_rel_lift_pct / 100.0) * base_ltv / (
        base_ltv - month1_ltv
    )

    iso_magnitude = {
        "purpose": (
            "equal +10% relative step on each lever, same model / ARPU / "
            "conversion / 12-month horizon — the only fair way to say which "
            "lever is stronger"
        ),
        "step_pct": ISO_STEP_PCT,
        "horizon_months": HORIZON_PRIMARY,
        "retention_step_definition": (
            "R(t) × 1.10 for t ≥ 2, capped at R(1); month 1 is unchanged "
            "because it already holds 100% of the paying cohort"
        ),
        "conversion_plus10pct": {
            "conv": round(conv * iso_mult, 4),
            "ltv_per_acquired_12m": round(conv_step_ltv, 2),
            "delta_pct": round(conv_step_delta_pct, 2),
        },
        "retention_plus10pct": {
            "ltv_per_acquired_12m": round(ret_step_ltv, 2),
            "delta_pct": round(ret_step_delta_pct, 2),
        },
        "ratio_conversion_over_retention": round(
            conv_step_delta_pct / ret_step_delta_pct, 2
        ),
        "ab_uplift_pct": round(ab_rel_lift_pct, 2),
        "ab_itt_uplift_pct": round(ab_itt_lift_pct, 2),
        "retention_multiplier_equivalent_to_ab_uplift": round(ret_mult_for_ab, 3),
    }

    # --- band ---------------------------------------------------------------
    band = rl.retention_band(fit, HORIZON_EXTRAP, k_sigma=1.0)
    curve = rl.retention_curve(fit, HORIZON_EXTRAP)

    # --- metrics json -------------------------------------------------------
    metrics = {
        "leg": "ltv",
        "generated_by": "analysis/run_04_ltv.py",
        "fit": {
            "model": "R(t) = a * t^(-b)  (OLS в log-space)",
            "a": round(fit.a, 4),
            "b": round(fit.b, 4),
            "r_squared": round(fit.r_squared, 4),
            "se": round(fit.se, 4),
            "n_points": fit.n,
        },
        "retention_points": points,
        "economics": {
            "signups": n_signups,
            "payers": n_payers,
            "conversion_signup_to_paid": round(conv, 4),
            "active_payer_months": active_payer_months,
            "arpu_per_active_payer_month_rub": round(arpu_per_period, 2),
            "net_revenue_rub": round(net_revenue, 2),
            "gross_revenue_rub": round(gross_revenue, 2),
            "cac_mean_rub": round(cac_mean, 2),
            "cac_median_rub": round(cac_median, 2),
        },
        "headline_12m": {
            "horizon_months": HORIZON_PRIMARY,
            "ltv_per_acquired_rub": round(rep12.ltv_per_acquired, 2),
            "ltv_per_paid_rub": round(rep12.ltv_per_paid, 2),
            "ltv_cac": round(rep12.ltv_cac, 3),
            "payback_months": rep12.payback_period,
            "extrapolation": rep12.extrapolation,
        },
        "headline_24m": {
            "horizon_months": HORIZON_EXTRAP,
            "ltv_per_acquired_rub": round(rep24.ltv_per_acquired, 2),
            "ltv_per_paid_rub": round(rep24.ltv_per_paid, 2),
            "ltv_cac": round(rep24.ltv_cac, 3),
            "payback_months": rep24.payback_period,
            "extrapolation": rep24.extrapolation,
        },
        "by_channel": chan.round(4).to_dict(orient="records"),
        "by_cohort": cohort_df.to_dict(orient="records"),
        "sensitivities": sensitivities,
        "iso_magnitude": iso_magnitude,
    }
    (OUT_DIR / "ltv_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2)
    )

    # --- figure -------------------------------------------------------------
    if not args.no_figures:
        make_figure(pts_df, curve, band)

    # --- console summary ----------------------------------------------------
    print(
        f"FIT   a={fit.a:.4f}  b={fit.b:.4f}  R²={fit.r_squared:.4f}  "
        f"se={fit.se:.4f}  n={fit.n}"
    )
    print(f"ECON  conv={conv:.4f}  ARPU/mo={arpu_per_period:.2f}₽  CAC={cac_mean:.2f}₽")
    print(
        f"LTV12 per-acquired={rep12.ltv_per_acquired:.1f}₽  "
        f"per-paid={rep12.ltv_per_paid:.1f}₽  LTV/CAC={rep12.ltv_cac:.2f}  "
        f"payback={rep12.payback_period} мес"
    )
    print(
        f"LTV24 per-acquired={rep24.ltv_per_acquired:.1f}₽  "
        f"per-paid={rep24.ltv_per_paid:.1f}₽  LTV/CAC={rep24.ltv_cac:.2f}"
    )
    print("\nBY CHANNEL\n" + chan.to_string(index=False))
    print("\nBY COHORT\n" + cohort_df.to_string(index=False))
    print("\nSENSITIVITIES", json.dumps(sensitivities, ensure_ascii=False))
    print("ISO-MAGNITUDE", json.dumps(iso_magnitude, ensure_ascii=False))
    print(f"\nWROTE {PROC_DIR / 'retention_points.csv'}")
    print(f"WROTE {PROC_DIR / 'ltv_by_cohort.csv'}")
    print(f"WROTE {OUT_DIR / 'ltv_metrics.json'}")


def make_figure(pts_df: pd.DataFrame, curve: list[dict], band: list[dict]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    matplotlib.rcParams["font.family"] = "DejaVu Sans"

    obs_t = pts_df["t"].tolist()
    obs_r = pts_df["retention"].tolist()
    ct = [p["t"] for p in curve]
    cr = [p["r"] for p in curve]
    lo = [p["lower"] for p in band]
    up = [p["upper"] for p in band]

    fig, ax = plt.subplots(figsize=(9, 5.5), dpi=300)
    ax.fill_between(
        ct, lo, up, color="#1f77b4", alpha=0.15, label="±1σ доверительная полоса"
    )
    ax.plot(ct, cr, color="#1f77b4", lw=2.0, label="Фит R(t) = a·t^(−b)")
    ax.scatter(
        obs_t,
        obs_r,
        color="#d62728",
        zorder=5,
        s=38,
        label="Наблюдаемые точки (платящая база)",
    )
    ax.axvline(12, color="#7f7f7f", ls="--", lw=1.0)
    ax.text(12.2, 0.95, "граница окна\n(12 мес)", color="#7f7f7f", fontsize=8, va="top")
    ax.set_xlabel("t — месяц жизни подписки (t=1 — месяц первой оплаты)")
    ax.set_ylabel("Retention, доля платящей базы")
    ax.set_title("Retention платящей базы: power-law фит R(t) = a·t^(−b)")
    ax.set_ylim(0, 1.05)
    ax.set_xlim(0.5, 24.5)
    ax.grid(alpha=0.25)
    ax.legend(loc="upper right", frameon=False)
    fig.tight_layout()
    fig.savefig(FIG_DIR / "retention-fit.png")
    plt.close(fig)


if __name__ == "__main__":
    main()
