#!/usr/bin/env python
"""EDA profile for product-ab-funnel-analytics (Leg 01: `eda`).

Produces:
  * `analysis/eda_metrics.json`  — every number cited in `docs/insights/00-eda.md`
  * `figures/*.png`              — the charts embedded in that report (dpi=300)

Run:
    uv run --project "<env>" python scripts/eda_profile.py        # numbers + figures
    uv run --project "<env>" python scripts/eda_profile.py --no-figures

Design notes (house style):
  * explicit dtypes on read — ids stay `str`, dates parsed explicitly, no silent coercion;
  * Copy-on-Write-safe: no chained assignment, no `inplace=True`, no defensive `.copy()`;
  * every number in the report comes from this file, so it is reproducible by re-running it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_DIR / "data" / "raw"
FIG_DIR = PROJECT_DIR / "figures"
OUT_DIR = PROJECT_DIR / "analysis"

EVENT_ORDER = ["signup", "activation", "paid_subscription", "renew", "cancel"]
AB_START = "2024-03-01"
AB_END = "2024-04-30"

# ---------------------------------------------------------------- palette (report-styling)
PALETTE = {
    "win": "#2ca02c",
    "baseline": "#1f77b4",
    "warn": "#ff7f0e",
    "fail": "#d62728",
    "muted": "#7f7f7f",
    "highlight": "#ffd700",
}

DTYPES_EVENTS = {
    "user_id": "str",
    "event_ts": "str",
    "event_name": "str",
    "platform": "str",
    "country": "str",
    "channel": "str",
    "experiment_group": "str",
    "experiment_flag": "int8",
}
DTYPES_TXN = {
    "user_id": "str",
    "transaction_id": "str",
    "transaction_date": "str",
    "amount": "float64",
    "plan": "str",
    "is_refund": "str",
}
DTYPES_USERS = {
    "user_id": "str",
    "signup_date": "str",
    "country": "str",
    "channel": "str",
    "platform": "str",
    "experiment_group": "str",
    "acquisition_cost": "float64",
}


# --------------------------------------------------------------------------- load
def load() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    events = pd.read_csv(RAW_DIR / "events.csv", dtype=DTYPES_EVENTS)
    events["event_ts"] = pd.to_datetime(events["event_ts"], format="%Y-%m-%d %H:%M:%S")
    events["event_date"] = events["event_ts"].dt.date

    txn = pd.read_csv(RAW_DIR / "transactions.csv", dtype=DTYPES_TXN)
    txn["transaction_date"] = pd.to_datetime(txn["transaction_date"], format="%Y-%m-%d")
    txn["is_refund_bool"] = txn["is_refund"].eq("True")

    users = pd.read_csv(RAW_DIR / "users.csv", dtype=DTYPES_USERS)
    users["signup_date"] = pd.to_datetime(users["signup_date"], format="%Y-%m-%d")

    for name, df in (("events", events), ("transactions", txn), ("users", users)):
        assert not df.columns.duplicated().any(), f"{name}: duplicated column names"
    return events, txn, users


def frame_profile(df: pd.DataFrame) -> dict:
    return {
        "rows": int(len(df)),
        "columns": int(df.shape[1]),
        "nulls_total": int(df.isna().sum().sum()),
        "duplicate_rows_full": int(df.duplicated().sum()),
        "dtypes": {c: str(t) for c, t in df.dtypes.items()},
    }


# --------------------------------------------------------------------------- contract
def contract(events: pd.DataFrame, txn: pd.DataFrame, users: pd.DataFrame) -> dict:
    users_ids = set(users["user_id"])
    event_ids = set(events["user_id"])
    txn_ids = set(txn["user_id"])

    # users -> events
    joined_ev = events.merge(users[["user_id"]], on="user_id", how="inner")
    lost_ev = len(events) - len(joined_ev)
    # users -> transactions
    joined_tx = txn.merge(users[["user_id"]], on="user_id", how="inner")
    lost_tx = len(txn) - len(joined_tx)

    # naive events x transactions join on user_id (fan-out trap)
    n_ev = events.groupby("user_id", observed=True).size()
    n_tx = txn.groupby("user_id", observed=True).size()
    common = n_ev.index.intersection(n_tx.index)
    naive_fanout = int((n_ev.loc[common] * n_tx.loc[common]).sum())

    return {
        "grain": {
            "users": "1 строка = 1 приобретённый пользователь (cohort member)",
            "events": "1 строка = 1 продуктовое событие пользователя в момент времени",
            "transactions": "1 строка = 1 списание (charge) или возврат (refund) по подписке",
        },
        "primary_key": {
            "users": "user_id (уникален)",
            "events": "нет суррогатного ключа; уникален по (user_id, event_ts, event_name)",
            "transactions": "transaction_id (уникален)",
        },
        "foreign_key": {
            "events": "user_id -> users.user_id",
            "transactions": "user_id -> users.user_id",
        },
        "users_unique_ids": int(users["user_id"].nunique()),
        "users_txn_unique_ids": int(len(txn_ids)),
        "events_unique_ids": int(len(event_ids)),
        "users_pk_duplicates": int(users["user_id"].duplicated().sum()),
        "txn_pk_duplicates": int(txn["transaction_id"].duplicated().sum()),
        "events_composite_key_duplicates": int(
            events.duplicated(["user_id", "event_ts", "event_name"]).sum()
        ),
        "join_users_events": {
            "step": "users INNER JOIN events ON user_id",
            "left_rows": int(len(users)),
            "right_rows": int(len(events)),
            "matched_rows": int(len(joined_ev)),
            "rows_lost": int(lost_ev),
            "users_without_events": int(len(users_ids - event_ids)),
            "events_without_user": int(len(event_ids - users_ids)),
        },
        "join_users_txn": {
            "step": "users INNER JOIN transactions ON user_id",
            "left_rows": int(len(users)),
            "right_rows": int(len(txn)),
            "matched_rows": int(len(joined_tx)),
            "rows_lost": int(lost_tx),
            "users_without_txn": int(len(users_ids - txn_ids)),
            "txn_without_user": int(len(txn_ids - users_ids)),
        },
        "three_way": {
            "step": "events JOIN users JOIN transactions (users LEFT JOIN both)",
            "users_with_events_and_txn": int(len(event_ids & txn_ids)),
            "users_with_events_no_txn": int(len(event_ids - txn_ids)),
            "users_with_txn_no_events": int(len(txn_ids - event_ids)),
        },
        "fanout_warning": {
            "note": "прямой events JOIN transactions ON user_id раздувает строки; агрегировать до user_id",
            "naive_join_rows": naive_fanout,
            "events_rows": int(len(events)),
            "multiplier": round(naive_fanout / len(events), 2),
        },
    }


# --------------------------------------------------------------------------- profile
def profile(events: pd.DataFrame, txn: pd.DataFrame, users: pd.DataFrame) -> dict:
    amount_all = txn["amount"]
    charges = txn.loc[~txn["is_refund_bool"], "amount"]
    refunds = txn.loc[txn["is_refund_bool"], "amount"]

    q1, q3 = charges.quantile([0.25, 0.75])
    iqr = q3 - q1
    iqr_outliers = int(((charges < q1 - 1.5 * iqr) | (charges > q3 + 1.5 * iqr)).sum())

    events_per_user = events.groupby("user_id", observed=True).size()
    txn_per_user = txn.groupby("user_id", observed=True).size()

    return {
        "frames": {
            "events": frame_profile(events),
            "transactions": frame_profile(txn),
            "users": frame_profile(users),
        },
        "date_ranges": {
            "events_ts": [str(events["event_ts"].min()), str(events["event_ts"].max())],
            "events_days_covered": int(events["event_date"].nunique()),
            "transactions_date": [
                str(txn["transaction_date"].min().date()),
                str(txn["transaction_date"].max().date()),
            ],
            "users_signup_date": [
                str(users["signup_date"].min().date()),
                str(users["signup_date"].max().date()),
            ],
        },
        "categorical": {
            "event_name": {
                k: int(v)
                for k, v in events["event_name"]
                .value_counts()
                .reindex(EVENT_ORDER)
                .items()
            },
            "platform_events": {
                k: int(v) for k, v in events["platform"].value_counts().items()
            },
            "platform_users": {
                k: int(v) for k, v in users["platform"].value_counts().items()
            },
            "country_events": {
                k: int(v) for k, v in events["country"].value_counts().items()
            },
            "country_users": {
                k: int(v) for k, v in users["country"].value_counts().items()
            },
            "channel_events": {
                k: int(v) for k, v in events["channel"].value_counts().items()
            },
            "channel_users": {
                k: int(v) for k, v in users["channel"].value_counts().items()
            },
            "plan": {k: int(v) for k, v in txn["plan"].value_counts().items()},
            "is_refund": {
                k: int(v) for k, v in txn["is_refund"].value_counts().items()
            },
        },
        "numeric": {
            "amount": {
                "n": int(len(amount_all)),
                "mean": round(float(amount_all.mean()), 2),
                "median": float(amount_all.median()),
                "min": float(amount_all.min()),
                "max": float(amount_all.max()),
                "std": round(float(amount_all.std()), 2),
                "p25": float(amount_all.quantile(0.25)),
                "p75": float(amount_all.quantile(0.75)),
                "p95": round(float(amount_all.quantile(0.95)), 2),
                "unique_values": sorted(float(v) for v in amount_all.unique()),
                "gross_charges_sum": round(float(charges.sum()), 2),
                "refunds_sum": round(float(refunds.sum()), 2),
                "net_sum": round(float(amount_all.sum()), 2),
                "charges_n": int(len(charges)),
                "charges_aov_mean": round(float(charges.mean()), 2),
                "charges_median": float(charges.median()),
                "charges_std": round(float(charges.std()), 2),
                "charges_iqr_outliers": iqr_outliers,
                "refunds_n": int(len(refunds)),
                "refunds_mean": round(float(refunds.mean()), 2),
                "refunds_median": float(refunds.median()),
                "refunds_std": round(float(refunds.std()), 2),
                "refunds_min": float(refunds.min()),
                "refunds_max": float(refunds.max()),
                "refund_share_pct": round(float(len(refunds) / len(txn) * 100), 2),
            },
            "acquisition_cost": {
                "n": int(len(users)),
                "mean": round(float(users["acquisition_cost"].mean()), 2),
                "median": round(float(users["acquisition_cost"].median()), 2),
                "min": float(users["acquisition_cost"].min()),
                "max": float(users["acquisition_cost"].max()),
                "std": round(float(users["acquisition_cost"].std()), 2),
                "by_channel_mean": {
                    k: round(float(v), 2)
                    for k, v in users.groupby("channel", observed=True)[
                        "acquisition_cost"
                    ]
                    .mean()
                    .items()
                },
                "by_country_mean": {
                    k: round(float(v), 2)
                    for k, v in users.groupby("country", observed=True)[
                        "acquisition_cost"
                    ]
                    .mean()
                    .items()
                },
            },
            "events_per_user": {
                "n_users": int(len(events_per_user)),
                "mean": round(float(events_per_user.mean()), 3),
                "median": float(events_per_user.median()),
                "min": int(events_per_user.min()),
                "max": int(events_per_user.max()),
                "std": round(float(events_per_user.std()), 3),
            },
            "txn_per_user": {
                "n_users": int(len(txn_per_user)),
                "mean": round(float(txn_per_user.mean()), 3),
                "median": float(txn_per_user.median()),
                "min": int(txn_per_user.min()),
                "max": int(txn_per_user.max()),
                "std": round(float(txn_per_user.std()), 3),
            },
        },
    }


# --------------------------------------------------------------------------- quality
def quality(events: pd.DataFrame, txn: pd.DataFrame, users: pd.DataFrame) -> dict:
    u = users[["user_id", "signup_date", "experiment_group"]].rename(
        columns={"experiment_group": "users_experiment_group"}
    )
    ev = events.merge(u, on="user_id", how="left", validate="many_to_one")

    paid_ts = ev.loc[
        ev["event_name"] == "paid_subscription", ["user_id", "event_ts"]
    ].rename(columns={"event_ts": "paid_ts"})
    last_renew = (
        ev.loc[ev["event_name"] == "renew"]
        .groupby("user_id", observed=True)["event_ts"]
        .max()
        .rename("last_renew_ts")
    )
    cancels = ev.loc[ev["event_name"] == "cancel"].merge(
        last_renew, on="user_id", how="left"
    )
    first_txn = (
        txn.groupby("user_id", observed=True)["transaction_date"]
        .min()
        .rename("first_txn_date")
    )

    paid_users = set(ev.loc[ev["event_name"] == "paid_subscription", "user_id"])
    txn_users = set(txn["user_id"])

    charges = txn.loc[~txn["is_refund_bool"]]
    refunds = txn.loc[txn["is_refund_bool"]]

    # plan <-> flat price consistency (490 / 890 / 1590)
    price = {"basic": 490.0, "plus": 890.0, "pro": 1590.0}
    expected = txn["plan"].map(price)
    plan_mismatch = int((expected != txn["amount"].abs()).sum())

    grp = ev.groupby("user_id", observed=True)["users_experiment_group"].nunique()
    group_mismatch = int(
        (
            ev.drop_duplicates("user_id").set_index("user_id")["experiment_group"]
            != ev.drop_duplicates("user_id").set_index("user_id")[
                "users_experiment_group"
            ]
        ).sum()
    )

    return {
        "nulls": {
            "events": int(events.isna().sum().sum()),
            "transactions": int(txn.isna().sum().sum()),
            "users": int(users.isna().sum().sum()),
        },
        "duplicates": {
            "events_full_row": int(events.duplicated().sum()),
            "events_user_ts_name": int(
                events.duplicated(["user_id", "event_ts", "event_name"]).sum()
            ),
            "events_user_date": int(events.duplicated(["user_id", "event_date"]).sum()),
            "transactions_full_row": int(txn.duplicated().sum()),
            "transaction_id": int(txn["transaction_id"].duplicated().sum()),
            "users_full_row": int(users.duplicated().sum()),
            "user_id": int(users["user_id"].duplicated().sum()),
        },
        "referential": {
            "events_without_user": int(
                len(set(events["user_id"]) - set(users["user_id"]))
            ),
            "txn_without_user": int(len(set(txn["user_id"]) - set(users["user_id"]))),
            "users_without_events": int(
                len(set(users["user_id"]) - set(events["user_id"]))
            ),
            "users_without_txn": int(len(set(users["user_id"]) - set(txn["user_id"]))),
            "txn_users_missing_paid_event": int(len(txn_users - paid_users)),
            "paid_event_users_missing_txn": int(len(paid_users - txn_users)),
        },
        "amount_integrity": {
            "negative_amounts": int((txn["amount"] < 0).sum()),
            "positive_refunds": int((refunds["amount"] > 0).sum()),
            "refund_flag_matches_sign": bool(
                ((txn["amount"] < 0) == txn["is_refund_bool"]).all()
            ),
            "plan_price_mismatch": plan_mismatch,
            "charges_rows": int(len(charges)),
            "refunds_rows": int(len(refunds)),
        },
        "lifecycle_anomalies": {
            "events_before_signup": int((ev["event_ts"] < ev["signup_date"]).sum()),
            "signup_event_date_differs_from_users": int(
                (
                    ev.loc[ev["event_name"] == "signup", "event_ts"].dt.normalize()
                    != ev.loc[ev["event_name"] == "signup", "signup_date"]
                ).sum()
            ),
            "renew_before_paid": int(
                ev.loc[ev["event_name"] == "renew"]
                .merge(paid_ts, on="user_id", how="inner")
                .pipe(lambda d: (d["event_ts"] < d["paid_ts"]).sum())
            ),
            "activation_after_paid": int(
                ev.loc[ev["event_name"] == "activation"]
                .merge(paid_ts, on="user_id", how="inner")
                .pipe(lambda d: (d["event_ts"] > d["paid_ts"]).sum())
            ),
            "cancel_events": int(len(cancels)),
            "cancel_without_prior_renew": int(cancels["last_renew_ts"].isna().sum()),
            "renew_after_cancel": int(
                (cancels["last_renew_ts"] > cancels["event_ts"]).sum()
            ),
            "first_txn_after_paid_event": int(
                paid_ts.merge(first_txn, on="user_id").pipe(
                    lambda d: (d["first_txn_date"] > d["paid_ts"].dt.normalize()).sum()
                )
            ),
            "first_txn_same_day_as_paid_event": int(
                paid_ts.merge(first_txn, on="user_id").pipe(
                    lambda d: (d["first_txn_date"] == d["paid_ts"].dt.normalize()).sum()
                )
            ),
        },
        "timestamp_hygiene": {
            "max_events_per_user_per_day": int(
                events.groupby(["user_id", "event_date"], observed=True).size().max()
            ),
            "users_with_multiple_experiment_groups": int((grp > 1).sum()),
            "experiment_group_mismatch_users_vs_events": group_mismatch,
            "future_dates": int(
                (events["event_ts"] > pd.Timestamp("2024-12-31 23:59:59")).sum()
                + (txn["transaction_date"] > pd.Timestamp("2024-12-31")).sum()
            ),
            "duplicate_timestamps_global": int(events["event_ts"].duplicated().sum()),
        },
        "funnel_consistency": {
            "users_with_signup_event": int(
                events.loc[events["event_name"] == "signup", "user_id"].nunique()
            ),
            "users_with_activation_event": int(
                events.loc[events["event_name"] == "activation", "user_id"].nunique()
            ),
            "users_with_paid_event": int(
                events.loc[
                    events["event_name"] == "paid_subscription", "user_id"
                ].nunique()
            ),
            "users_with_renew_event": int(
                events.loc[events["event_name"] == "renew", "user_id"].nunique()
            ),
            "users_with_cancel_event": int(
                events.loc[events["event_name"] == "cancel", "user_id"].nunique()
            ),
            "max_renews_per_user": int(
                events.loc[events["event_name"] == "renew"]
                .groupby("user_id", observed=True)
                .size()
                .max()
            ),
            "activation_without_signup": int(
                len(
                    set(events.loc[events["event_name"] == "activation", "user_id"])
                    - set(events.loc[events["event_name"] == "signup", "user_id"])
                )
            ),
            "paid_without_activation": int(
                len(
                    set(
                        events.loc[
                            events["event_name"] == "paid_subscription", "user_id"
                        ]
                    )
                    - set(events.loc[events["event_name"] == "activation", "user_id"])
                )
            ),
            "cancel_without_paid": int(
                len(
                    set(events.loc[events["event_name"] == "cancel", "user_id"])
                    - paid_users
                )
            ),
        },
    }


# --------------------------------------------------------------------------- experiment
def experiment(events: pd.DataFrame, txn: pd.DataFrame, users: pd.DataFrame) -> dict:
    exp = users.loc[users["experiment_group"] != "none"]
    in_window = users.loc[
        (users["signup_date"] >= AB_START) & (users["signup_date"] <= AB_END)
    ]
    groups = exp["experiment_group"].value_counts()
    n_control = int(groups.get("control", 0))
    n_treatment = int(groups.get("treatment", 0))
    n_total = n_control + n_treatment

    # chi-square vs 50/50 (SRM), no scipy dependency
    expected = n_total / 2
    chi2 = ((n_control - expected) ** 2 + (n_treatment - expected) ** 2) / expected

    # group consistency events vs users
    per_user_events = events.drop_duplicates("user_id").set_index("user_id")[
        "experiment_group"
    ]
    per_user_users = users.set_index("user_id")["experiment_group"]
    aligned = per_user_events.index.intersection(per_user_users.index)
    mismatch = int((per_user_events.loc[aligned] != per_user_users.loc[aligned]).sum())

    # activation -> paid inside cohort
    ev = events.merge(
        exp[["user_id", "experiment_group"]].rename(
            columns={"experiment_group": "cohort_group"}
        ),
        on="user_id",
        how="inner",
        validate="many_to_one",
    )
    activated = ev.loc[ev["event_name"] == "activation"].drop_duplicates("user_id")
    paid = ev.loc[ev["event_name"] == "paid_subscription"].drop_duplicates("user_id")
    rows = {}
    for g in ("control", "treatment"):
        a = int((activated["cohort_group"] == g).sum())
        p = int((paid["cohort_group"] == g).sum())
        rows[g] = {
            "users": int((exp["experiment_group"] == g).sum()),
            "activated": a,
            "paid": p,
            "activation_rate_pct": round(a / rows_denom(exp, g) * 100, 2),
            "paid_given_activated_pct": round(p / a * 100, 2) if a else None,
        }

    ratio = n_control / n_treatment if n_treatment else None
    return {
        "cohort_users": n_total,
        "control": n_control,
        "treatment": n_treatment,
        "expected_per_arm": n_total / 2,
        "none_group_users": int((users["experiment_group"] == "none").sum()),
        "control_share_pct": round(n_control / n_total * 100, 3),
        "treatment_share_pct": round(n_treatment / n_total * 100, 3),
        "control_treatment_ratio": round(ratio, 4),
        "chi2_vs_50_50": round(chi2, 3),
        "srm_critical_001": 6.63,
        "srm_detected": bool(chi2 > 6.63),
        "ab_window": [AB_START, AB_END],
        "ab_window_days": int((pd.Timestamp(AB_END) - pd.Timestamp(AB_START)).days + 1),
        "users_signup_in_window": int(len(in_window)),
        "users_in_window_and_flagged": int(
            (in_window["experiment_group"] != "none").sum()
        ),
        "users_flagged_outside_window": int(
            len(
                users.loc[
                    (users["experiment_group"] != "none")
                    & (
                        (users["signup_date"] < AB_START)
                        | (users["signup_date"] > AB_END)
                    )
                ]
            )
        ),
        "none_flag_users_inside_window": int(
            (in_window["experiment_group"] == "none").sum()
        ),
        "events_in_cohort": int(len(ev)),
        "events_flag_vs_group_mismatch_users": mismatch,
        "group_details": rows,
    }


def rows_denom(exp: pd.DataFrame, group: str) -> int:
    return int((exp["experiment_group"] == group).sum())


# --------------------------------------------------------------------------- trend
def trend(events: pd.DataFrame, users: pd.DataFrame) -> dict:
    daily = events.groupby("event_date", observed=True).size()
    monthly_events = events.groupby(
        events["event_ts"].dt.to_period("M"), observed=True
    ).size()
    monthly_signups = users.groupby(
        users["signup_date"].dt.to_period("M"), observed=True
    ).size()
    daily_signups = (
        events.loc[events["event_name"] == "signup"]
        .groupby("event_date", observed=True)
        .size()
    )
    return {
        "daily_events": {
            "days": int(len(daily)),
            "total": int(daily.sum()),
            "mean": round(float(daily.mean()), 1),
            "min": int(daily.min()),
            "min_day": str(daily.idxmin()),
            "max": int(daily.max()),
            "max_day": str(daily.idxmax()),
            "p95": float(daily.quantile(0.95)),
        },
        "monthly_events": {str(k): int(v) for k, v in monthly_events.items()},
        "monthly_signups": {str(k): int(v) for k, v in monthly_signups.items()},
        "daily_series": {str(k): int(v) for k, v in daily.items()},
        "daily_signup_series": {str(k): int(v) for k, v in daily_signups.items()},
        "weekend_share_pct": round(
            float(events["event_ts"].dt.dayofweek.ge(5).mean() * 100), 2
        ),
    }


# --------------------------------------------------------------------------- derived
def derived(c: dict, pr: dict, q: dict, e: dict, tr: dict) -> dict:
    """Ratios cited in prose — exported so every prose number is machine-checkable."""
    sig = q["funnel_consistency"]["users_with_signup_event"]
    act = q["funnel_consistency"]["users_with_activation_event"]
    paid = q["funnel_consistency"]["users_with_paid_event"]
    renew = q["funnel_consistency"]["users_with_renew_event"]
    cancel = q["funnel_consistency"]["users_with_cancel_event"]
    events_total = pr["frames"]["events"]["rows"]
    counts = pr["categorical"]["event_name"]

    return {
        "signup_to_activation_pct": round(act / sig * 100, 2),
        "activation_to_paid_pct": round(paid / act * 100, 2),
        "signup_to_paid_pct": round(paid / sig * 100, 2),
        "renew_users_share_of_signup_pct": round(renew / sig * 100, 2),
        "cancel_users_share_of_signup_pct": round(cancel / sig * 100, 2),
        "non_payer_share_of_base_pct": round((sig - paid) / sig * 100, 2),
        "base_over_payers_ratio": round(sig / paid, 2),
        "payers_without_renew": paid - renew,
        "funnel_loss_at_activation_to_paid_share_pct": round(
            (act - paid) / (sig - paid) * 100, 1
        ),
        "renew_event_share_pct": round(counts["renew"] / events_total * 100, 2),
        "cancel_event_share_pct": round(counts["cancel"] / events_total * 100, 2),
        "charges_iqr_outlier_share_pct": round(
            pr["numeric"]["amount"]["charges_iqr_outliers"]
            / pr["numeric"]["amount"]["charges_n"]
            * 100,
            1,
        ),
        "dec_over_jan_events_ratio": round(
            tr["monthly_events"]["2024-12"] / tr["monthly_events"]["2024-01"], 2
        ),
        "dec_signups": tr["monthly_signups"]["2024-12"],
        "treatment_over_control_paid_ratio": round(
            e["group_details"]["treatment"]["paid_given_activated_pct"]
            / e["group_details"]["control"]["paid_given_activated_pct"],
            3,
        ),
        "control_activation_minus_treatment_pp": round(
            e["group_details"]["control"]["activation_rate_pct"]
            - e["group_details"]["treatment"]["activation_rate_pct"],
            2,
        ),
        "events_in_cohort_share_pct": round(
            e["events_in_cohort"] / events_total * 100, 2
        ),
    }


# --------------------------------------------------------------------------- figures
def _style() -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.family": ["DejaVu Sans", "sans-serif"],
            "axes.unicode_minus": False,
            "font.size": 12,
            "axes.titlesize": 15,
            "axes.titleweight": "bold",
            "axes.labelsize": 12,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "figure.facecolor": "white",
            "figure.dpi": 100,
        }
    )


def figures(
    events: pd.DataFrame, txn: pd.DataFrame, users: pd.DataFrame, exp: dict
) -> list[str]:
    _style()
    import matplotlib.pyplot as plt

    FIG_DIR.mkdir(parents=True, exist_ok=True)
    made: list[str] = []

    # 1. daily event volume
    daily = events.groupby("event_date", observed=True).size()
    fig, ax = plt.subplots(figsize=(13, 5.5), layout="constrained")
    ax.plot(
        pd.to_datetime(daily.index),
        daily.values,
        color=PALETTE["baseline"],
        linewidth=1.4,
    )
    ax.axvspan(
        pd.Timestamp(AB_START),
        pd.Timestamp(AB_END),
        color=PALETTE["highlight"],
        alpha=0.35,
    )
    ax.set(
        title="Объём событий растёт весь год: декабрь 4033 против января 2167 — A/B-окно даёт локальный всплеск в марте-апреле",
        xlabel="Дата (2024)",
        ylabel="События в день, шт.",
    )
    ax.annotate(
        f"A/B-окно 2024-03-01 … 2024-04-30\ncohort: {exp['cohort_users']} пользователя с флагом",
        xy=(pd.Timestamp("2024-04-01"), 120),
        xytext=(pd.Timestamp("2024-05-20"), 150),
        arrowprops={"arrowstyle": "->", "color": PALETTE["warn"], "lw": 2},
        fontsize=11,
        fontweight="bold",
        bbox={
            "boxstyle": "round",
            "facecolor": "lightyellow",
            "edgecolor": PALETTE["warn"],
        },
    )
    ax.annotate(
        f"max {daily.max()} событий — {str(daily.idxmax())}",
        xy=(pd.to_datetime(daily.idxmax()), daily.max()),
        xytext=(pd.Timestamp("2024-01-20"), 138),
        arrowprops={"arrowstyle": "->", "color": PALETTE["muted"], "lw": 1.5},
        fontsize=10,
        color="#333333",
    )
    ax.set_ylim(40, 172)
    ax.grid(alpha=0.25)
    p = FIG_DIR / "daily-event-volume.png"
    fig.savefig(p, dpi=300, bbox_inches="tight")
    plt.close(fig)
    made.append(str(p.relative_to(PROJECT_DIR)))

    # 2. event_name distribution
    counts = events["event_name"].value_counts().reindex(EVENT_ORDER)
    label_map = {
        "signup": "signup\n(регистрация)",
        "activation": "activation\n(онбординг)",
        "paid_subscription": "paid_subscription\n(1-й платёж)",
        "renew": "renew\n(продление)",
        "cancel": "cancel\n(отток)",
    }
    colors = [
        PALETTE["baseline"],
        PALETTE["baseline"],
        PALETTE["win"],
        PALETTE["baseline"],
        PALETTE["fail"],
    ]
    fig, ax = plt.subplots(figsize=(11, 6), layout="constrained")
    bars = ax.barh(
        [label_map[k] for k in counts.index],
        counts.values,
        color=colors,
        edgecolor="white",
    )
    ax.bar_label(bars, fmt="%d", padding=4, fontweight="bold")
    ax.invert_yaxis()
    ax.set(
        title=f"renew — {counts['renew'] / counts.sum() * 100:.0f}% событий: событийный лог весит биллингом, а не воронкой",
        xlabel="События, шт.",
    )
    ax.set_xlim(0, counts.max() * 1.18)
    ax.grid(axis="x", alpha=0.25)
    p = FIG_DIR / "event-name-distribution.png"
    fig.savefig(p, dpi=300, bbox_inches="tight")
    plt.close(fig)
    made.append(str(p.relative_to(PROJECT_DIR)))

    # 3. amount distribution
    charges = txn.loc[~txn["is_refund_bool"], "amount"]
    refunds = txn.loc[txn["is_refund_bool"], "amount"]
    fig, ax = plt.subplots(figsize=(12, 5.8), layout="constrained")
    bins = [400, 600, 800, 1000, 1200, 1400, 1700]
    ax.hist(
        charges,
        bins=bins,
        color=PALETTE["baseline"],
        edgecolor="white",
        label=f"charges (n={len(charges)})",
    )
    ax.hist(
        refunds,
        bins=[-1700, -1400, -1200, -1000, -800, -600, -400],
        color=PALETTE["fail"],
        edgecolor="white",
        alpha=0.85,
        label=f"refunds (n={len(refunds)})",
    )
    for x, lab in ((490, "basic 490"), (890, "plus 890"), (1590, "pro 1590")):
        ax.axvline(x, color=PALETTE["muted"], linestyle=":", linewidth=1)
        ax.annotate(
            lab,
            xy=(x, ax.get_ylim()[1] * 0.95),
            xytext=(x - 60, ax.get_ylim()[1] * 0.98),
            fontsize=10,
            color="#333333",
        )
    ax.set(
        title="amount дискретен: ровно три тарифа 490/890/1590 ₽ + зеркальные возвраты",
        xlabel="Сумма транзакции, ₽ (отрицательные = возвраты)",
        ylabel="Транзакции, шт.",
    )
    ax.legend(frameon=False)
    ax.grid(axis="y", alpha=0.25)
    p = FIG_DIR / "amount-distribution.png"
    fig.savefig(p, dpi=300, bbox_inches="tight")
    plt.close(fig)
    made.append(str(p.relative_to(PROJECT_DIR)))

    # 4. funnel stages (user counts)
    order = ["signup", "activation", "paid_subscription", "renew", "cancel"]
    users_at = [
        events.loc[events["event_name"] == n, "user_id"].nunique() for n in order
    ]
    fig, ax = plt.subplots(figsize=(11.5, 5.8), layout="constrained")
    bars = ax.bar(
        [label_map[k] for k in order],
        users_at,
        color=[
            PALETTE["baseline"],
            PALETTE["baseline"],
            PALETTE["win"],
            PALETTE["muted"],
            PALETTE["fail"],
        ],
    )
    ax.bar_label(bars, fmt="%d", padding=4, fontweight="bold")
    base = users_at[0]
    for i, v in enumerate(users_at):
        ax.annotate(
            f"{v / base * 100:.1f}% от signup",
            xy=(i, v),
            xytext=(i, v + base * 0.085),
            ha="center",
            fontsize=10.5,
            color="#333333",
        )
    ax.tick_params(axis="x", labelsize=11)
    ax.set(
        title=f"signup → activation {users_at[1] / base * 100:.1f}%, activation → paid {users_at[2] / users_at[1] * 100:.1f}%: потери воронки живут в активации→платеже",
        ylabel="Уникальных пользователей, достигших шага",
    )
    ax.set_ylim(0, base * 1.22)
    ax.grid(axis="y", alpha=0.25)
    p = FIG_DIR / "funnel-stage-users.png"
    fig.savefig(p, dpi=300, bbox_inches="tight")
    plt.close(fig)
    made.append(str(p.relative_to(PROJECT_DIR)))

    # 5. experiment balance
    fig, ax = plt.subplots(figsize=(9.5, 5.5), layout="constrained")
    bars = ax.bar(
        ["control", "treatment"],
        [exp["control"], exp["treatment"]],
        color=[PALETTE["baseline"], PALETTE["win"]],
        width=0.55,
    )
    ax.bar_label(bars, fmt="%d", padding=4, fontweight="bold")
    ax.axhline(
        exp["cohort_users"] / 2,
        color=PALETTE["muted"],
        linestyle="--",
        linewidth=1.4,
        label="ожидание при 50/50",
    )
    ax.set(
        title=f"A/B-группы сбалансированы: контроль {exp['control']} vs вариант {exp['treatment']}, χ²={exp['chi2_vs_50_50']} — SRM нет",
        ylabel="Пользователей в группе",
    )
    ax.set_ylim(0, exp["cohort_users"] * 0.68)
    ax.legend(frameon=False)
    ax.grid(axis="y", alpha=0.25)
    p = FIG_DIR / "experiment-group-balance.png"
    fig.savefig(p, dpi=300, bbox_inches="tight")
    plt.close(fig)
    made.append(str(p.relative_to(PROJECT_DIR)))

    return made


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(
        description="EDA profile for product-ab-funnel-analytics"
    )
    ap.add_argument("--no-figures", action="store_true", help="skip PNG rendering")
    ap.add_argument(
        "--json",
        type=Path,
        default=OUT_DIR / "eda_metrics.json",
        help="where to write the metrics JSON",
    )
    args = ap.parse_args()

    events, txn, users = load()
    metrics = {
        "meta": {
            "pandas_version": pd.__version__,
            "source_files": {
                "events": "data/raw/events.csv",
                "transactions": "data/raw/transactions.csv",
                "users": "data/raw/users.csv",
            },
            "source_columns": {
                name: len(pd.read_csv(RAW_DIR / f"{name}.csv", nrows=0).columns)
                for name in ("events", "transactions", "users")
            },
            "ab_window": [AB_START, AB_END],
        },
        "contract": contract(events, txn, users),
        "profile": profile(events, txn, users),
        "quality": quality(events, txn, users),
        "experiment": experiment(events, txn, users),
        "trend": trend(events, users),
    }
    metrics["derived"] = derived(
        metrics["contract"],
        metrics["profile"],
        metrics["quality"],
        metrics["experiment"],
        metrics["trend"],
    )

    if not args.no_figures:
        metrics["figures"] = figures(events, txn, users, metrics["experiment"])

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # ---- human-readable digest, mirrors docs/insights/00-eda.md
    c = metrics["contract"]
    q = metrics["quality"]
    e = metrics["experiment"]
    tr = metrics["trend"]
    pr = metrics["profile"]
    try:
        json_label = args.json.resolve().relative_to(PROJECT_DIR)
    except ValueError:
        json_label = args.json
    print("=" * 78)
    print("EDA PROFILE — product-ab-funnel-analytics")
    print(f"pandas {metrics['meta']['pandas_version']} | json -> {json_label}")
    print("=" * 78)
    print(
        "\n-- CONTRACT --------------------------------------------------------------"
    )
    for k, v in c["join_users_events"].items():
        print(f"  users->events   {k:28} {v}")
    for k, v in c["join_users_txn"].items():
        print(f"  users->txn      {k:28} {v}")
    for k, v in c["three_way"].items():
        print(f"  three-way       {k:28} {v}")
    print(f"  composite-key duplicates       {c['events_composite_key_duplicates']}")
    print(
        f"  fan-out trap                   naive events x txn = {c['fanout_warning']['naive_join_rows']} rows (x{c['fanout_warning']['multiplier']})"
    )

    print(
        "\n-- PROFILE ---------------------------------------------------------------"
    )
    for name, f in pr["frames"].items():
        print(
            f"  {name:13} rows={f['rows']:>6} cols={f['columns']} nulls={f['nulls_total']} dup_rows={f['duplicate_rows_full']}"
        )
    print(f"  event_name counts              {pr['categorical']['event_name']}")
    print(
        f"  amount  n={pr['numeric']['amount']['n']} mean={pr['numeric']['amount']['mean']} median={pr['numeric']['amount']['median']} "
        f"min={pr['numeric']['amount']['min']} max={pr['numeric']['amount']['max']} unique={pr['numeric']['amount']['unique_values']}"
    )
    print(
        f"  amounts gross={pr['numeric']['amount']['gross_charges_sum']} refunds={pr['numeric']['amount']['refunds_sum']} net={pr['numeric']['amount']['net_sum']}"
    )
    print(
        f"  charges aov={pr['numeric']['amount']['charges_aov_mean']} n={pr['numeric']['amount']['charges_n']} iqr_outliers={pr['numeric']['amount']['charges_iqr_outliers']}"
    )
    print(
        f"  cac     mean={pr['numeric']['acquisition_cost']['mean']} min={pr['numeric']['acquisition_cost']['min']} max={pr['numeric']['acquisition_cost']['max']}"
    )
    print(f"  cac by channel {pr['numeric']['acquisition_cost']['by_channel_mean']}")
    print(f"  events/user    {pr['numeric']['events_per_user']}")
    print(f"  txn/user       {pr['numeric']['txn_per_user']}")
    print(
        f"  dates          events={pr['date_ranges']['events_ts']} days={pr['date_ranges']['events_days_covered']} txn={pr['date_ranges']['transactions_date']} signup={pr['date_ranges']['users_signup_date']}"
    )

    print(
        "\n-- QUALITY ---------------------------------------------------------------"
    )
    for section in (
        "referential",
        "amount_integrity",
        "lifecycle_anomalies",
        "timestamp_hygiene",
        "funnel_consistency",
    ):
        print(f"  [{section}]")
        for k, v in q[section].items():
            print(f"    {k:42} {v}")

    print(
        "\n-- EXPERIMENT ------------------------------------------------------------"
    )
    for k in (
        "cohort_users",
        "control",
        "treatment",
        "control_share_pct",
        "treatment_share_pct",
        "control_treatment_ratio",
        "chi2_vs_50_50",
        "srm_detected",
        "ab_window",
        "users_signup_in_window",
        "users_flagged_outside_window",
        "events_in_cohort",
    ):
        print(f"  {k:32} {e[k]}")
    for g, d in e["group_details"].items():
        print(f"  {g:10} {d}")

    print("\n-- TREND ----------------------------------------------------------------")
    print(f"  daily events {tr['daily_events']}")
    print(f"  monthly events {tr['monthly_events']}")
    print(f"  monthly signups {tr['monthly_signups']}")
    print(f"  weekend share {tr['weekend_share_pct']}%")
    print("\n-- DERIVED --------------------------------------------------------------")
    for k, v in metrics["derived"].items():
        print(f"  {k:46} {v}")
    if "figures" in metrics:
        print(
            "\n-- FIGURES --------------------------------------------------------------"
        )
        for f in metrics["figures"]:
            print(f"  {f}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
