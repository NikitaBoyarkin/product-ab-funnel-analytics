#!/usr/bin/env python3
"""Deterministic synthetic dataset generator for `product-ab-funnel-analytics`.

                     ***  SYNTHETIC DATA -- NOT REAL  ***

Nothing in this repository is real user data. This script fabricates a
realistic-looking subscription B2C product (ru) and writes three flat CSV
files. Every number below is an explicit, documented assumption.

Outputs (all under ``data/raw/``):

* ``events.csv``       -- product event log (signup / activation /
                          paid_subscription / renew / cancel)
* ``transactions.csv`` -- payment ledger for paying users (charges + refunds)
* ``users.csv``        -- one row per acquired user (cohort attributes)

Determinism
-----------
A single ``numpy.random.default_rng(SEED)`` instance drives every random
draw, in a fixed order, so the three files are byte-reproducible given the
same library versions. ``SEED = 42``.

Dependencies: stdlib + numpy + pandas only.

Usage
-----
    python data/raw/_generate.py          # writes the three CSVs next to this file
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ===========================================================================
# 1. TUNABLES  (every value is a synthetic assumption -- documented in data/README.md)
# ===========================================================================

SEED = 42
N_USERS = 12_000

PERIOD_START = date(2024, 1, 1)
PERIOD_END = date(2024, 12, 31)
N_DAYS = (PERIOD_END - PERIOD_START).days + 1  # 366 -- 2024 is a leap year

# --- A/B experiment -------------------------------------------------------
# New onboarding flow tested on everyone who signed up in this window.
AB_START = date(2024, 3, 1)
AB_END = date(2024, 4, 30)

# Target metric = P(paid_subscription | activation), i.e. activation -> paid.
# Treatment bakes in a REAL effect: +25% relative lift.
P_PAID_CONTROL = 0.35
P_PAID_TREATMENT = 0.35 * 1.25  # 0.4375

# --- Acquisition mix ------------------------------------------------------
# Seasonal signup intensity by calendar month (Jan..Dec); higher in Jan/Sep,
# plus a spring onboarding campaign that concentrates traffic into the A/B
# window (Mar-Apr) so the experiment cohort is large enough to be conclusive.
MONTH_SIGNUP_WEIGHT = np.array(
    [1.15, 0.95, 1.35, 1.30, 0.95, 0.90, 0.85, 0.92, 1.10, 1.05, 1.05, 0.98]
)

PLATFORMS = ["android", "ios", "web"]
P_PLATFORM = np.array([0.44, 0.34, 0.22])

COUNTRIES = ["RU", "KZ", "BY", "AM", "Other"]
P_COUNTRY = np.array([0.78, 0.09, 0.06, 0.04, 0.03])

CHANNELS = ["organic", "paid_search", "social", "referral", "content", "email"]
P_CHANNEL = np.array([0.28, 0.22, 0.18, 0.12, 0.11, 0.09])

# Blended acquisition cost (RUB) per channel + noise; clipped to [400, 900].
CHANNEL_CAC = {
    "organic": 450,
    "referral": 420,
    "content": 520,
    "email": 560,
    "social": 700,
    "paid_search": 850,
}
CAC_NOISE_SD = 40.0
CAC_CLIP = (400.0, 900.0)

# --- Funnel ---------------------------------------------------------------
# P(activation | signup), modified by platform (mobile activates better).
P_ACT_BY_PLATFORM = {"android": 0.80, "ios": 0.86, "web": 0.74}
ACTIVATION_DELAY_MEAN_DAYS = 0.8  # exponential, capped at 7 days
ACTIVATION_DELAY_CAP_DAYS = 7.0

# Channel intent multiplier on P(paid | activation).
CHANNEL_INTENT = {
    "organic": 1.00,
    "referral": 1.15,
    "email": 1.12,
    "content": 0.98,
    "paid_search": 0.95,
    "social": 0.88,
}
PAID_DELAY_MIN_DAYS = 1
PAID_DELAY_MAX_DAYS = 14

# --- Subscription / retention --------------------------------------------
# Monthly tiers with a flat monthly price (RUB). `plan` in transactions.csv.
PLANS = ["basic", "plus", "pro"]
P_PLAN = np.array([0.55, 0.30, 0.15])
PLAN_PRICE = {"basic": 490, "plus": 890, "pro": 1590}

# Cumulative survival to day t:  S(t) = floor + (1-floor) * (1 + t/30)^(-b).
# A power law with a long-term loyal floor -> a dropping retention curve that
# the LTV leg can fit as R(t) = a * t^(-b).
RET_B = 0.30
RET_FLOOR = 0.08
RET_T0 = 30.0
RENEW_CYCLE_DAYS = 30  # monthly billing

# ~3% of charges are refunded; refund rows carry a NEGATIVE amount.
REFUND_RATE = 0.03
REFUND_DELAY_DAYS = (2, 45)


# ===========================================================================
# 2. Helpers
# ===========================================================================
def _iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def survival(t_days: float) -> float:
    """Power-law cumulative survival with a loyal-customer floor."""
    return RET_FLOOR + (1.0 - RET_FLOOR) * (1.0 + t_days / RET_T0) ** (-RET_B)


def _add_days(d: date, days: int) -> date:
    return d + timedelta(days=days)


# ===========================================================================
# 3. Generate
# ===========================================================================
def generate() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(SEED)

    # ----- 3.1 signup timestamps (seasonal, with an hour-of-day shape) -----
    day_weights = np.empty(N_DAYS)
    for i in range(N_DAYS):
        d = _add_days(PERIOD_START, i)
        day_weights[i] = MONTH_SIGNUP_WEIGHT[d.month - 1]
    day_weights /= day_weights.sum()

    hour_shape = np.array(
        [
            1,
            1,
            1,
            1,
            1,
            2,
            3,
            5,
            8,
            11,
            13,
            14,
            13,
            12,
            12,
            13,
            14,
            16,
            18,
            19,
            17,
            13,
            7,
            3,
        ],
        dtype=float,
    )
    hour_shape /= hour_shape.sum()

    day_idx = rng.choice(N_DAYS, size=N_USERS, p=day_weights)
    hours = rng.choice(24, size=N_USERS, p=hour_shape)
    minutes = rng.integers(0, 60, size=N_USERS)
    seconds = rng.integers(0, 60, size=N_USERS)

    signup_dt = np.array(
        [
            datetime.combine(
                _add_days(PERIOD_START, int(day_idx[i])),
                datetime.min.time(),
            )
            + timedelta(
                hours=int(hours[i]), minutes=int(minutes[i]), seconds=int(seconds[i])
            )
            for i in range(N_USERS)
        ],
        dtype="datetime64[s]",
    )
    signup_dt = pd.to_datetime(signup_dt)

    # ----- 3.2 cohort attributes ------------------------------------------
    platform = rng.choice(PLATFORMS, size=N_USERS, p=P_PLATFORM)
    country = rng.choice(COUNTRIES, size=N_USERS, p=P_COUNTRY)
    channel = rng.choice(CHANNELS, size=N_USERS, p=P_CHANNEL)

    cac = np.array([CHANNEL_CAC[c] for c in channel], dtype=float)
    cac = np.clip(cac + rng.normal(0, CAC_NOISE_SD, size=N_USERS), *CAC_CLIP)

    # Sort by signup time and assign stable, human-readable ids.
    order = np.argsort(signup_dt.values, kind="stable")
    signup_dt = signup_dt[order]
    platform = platform[order]
    country = country[order]
    channel = channel[order]
    cac = cac[order]
    user_ids = np.array([f"u_{i + 1:06d}" for i in range(N_USERS)])

    signup_date = signup_dt.normalize()

    # ----- 3.3 experiment assignment (signup inside the A/B window) --------
    in_ab = (signup_dt >= pd.Timestamp(AB_START)) & (
        signup_dt < pd.Timestamp(AB_END) + pd.Timedelta(days=1)
    )
    exp_group = np.array(["none"] * N_USERS, dtype=object)
    treat_draw = rng.random(N_USERS) < 0.5  # 50/50 randomisation
    exp_group[in_ab & treat_draw] = "treatment"
    exp_group[in_ab & ~treat_draw] = "control"
    experiment_flag = in_ab.astype(int)

    # ----- 3.4 funnel per user --------------------------------------------
    events: list[dict] = []
    transactions: list[dict] = []

    end_ts = pd.Timestamp(PERIOD_END) + pd.Timedelta(hours=23, minutes=59, seconds=59)

    def emit(uid, ts, name, i):
        events.append(
            {
                "user_id": uid,
                "event_ts": _iso(ts),
                "event_name": name,
                "platform": platform[i],
                "country": country[i],
                "channel": channel[i],
                "experiment_group": exp_group[i],
                "experiment_flag": int(experiment_flag[i]),
            }
        )

    txn_counter = 0

    for i in range(N_USERS):
        uid = user_ids[i]
        su = signup_dt[i]
        emit(uid, su, "signup", i)

        # --- activation ---
        p_act = P_ACT_BY_PLATFORM[platform[i]]
        if rng.random() >= p_act:
            continue
        act_delay = min(
            rng.exponential(ACTIVATION_DELAY_MEAN_DAYS), ACTIVATION_DELAY_CAP_DAYS
        )
        act_ts = su + pd.Timedelta(days=float(act_delay))
        if act_ts > end_ts:
            continue
        emit(uid, act_ts, "activation", i)

        # --- activation -> paid (the A/B target metric) ---
        if exp_group[i] == "treatment":
            p_paid = P_PAID_TREATMENT
        else:
            p_paid = (
                P_PAID_CONTROL  # control AND non-experiment users share the baseline
            )
        p_paid = float(np.clip(p_paid * CHANNEL_INTENT[channel[i]], 0.02, 0.90))
        if rng.random() >= p_paid:
            continue

        paid_delay = rng.integers(PAID_DELAY_MIN_DAYS, PAID_DELAY_MAX_DAYS + 1)
        paid_ts = act_ts + pd.Timedelta(days=int(paid_delay))
        if paid_ts > end_ts:
            continue  # right-censored by the end of the observation window

        plan = rng.choice(PLANS, p=P_PLAN)
        price = PLAN_PRICE[plan]
        emit(uid, paid_ts, "paid_subscription", i)

        txn_counter += 1
        transactions.append(
            {
                "user_id": uid,
                "transaction_id": f"txn_{txn_counter:07d}",
                "transaction_date": paid_ts.strftime("%Y-%m-%d"),
                "amount": float(price),
                "plan": plan,
                "is_refund": False,
            }
        )
        charges = [(paid_ts, float(price))]

        # --- monthly renewals, power-law survival ---
        k = 1
        while True:
            renew_ts = paid_ts + pd.Timedelta(days=RENEW_CYCLE_DAYS * k)
            if renew_ts > end_ts:
                break
            cont_prob = survival(RENEW_CYCLE_DAYS * k) / survival(
                RENEW_CYCLE_DAYS * (k - 1)
            )
            if rng.random() >= cont_prob:
                emit(uid, renew_ts, "cancel", i)  # failed to renew -> churn
                break
            emit(uid, renew_ts, "renew", i)
            txn_counter += 1
            transactions.append(
                {
                    "user_id": uid,
                    "transaction_id": f"txn_{txn_counter:07d}",
                    "transaction_date": renew_ts.strftime("%Y-%m-%d"),
                    "amount": float(price),
                    "plan": plan,
                    "is_refund": False,
                }
            )
            charges.append((renew_ts, float(price)))
            k += 1

        # --- refunds on ~3% of charges (negative amounts) ---
        for charge_ts, charge_amt in charges:
            if rng.random() >= REFUND_RATE:
                continue
            delay = int(rng.integers(*REFUND_DELAY_DAYS))
            ref_ts = charge_ts + pd.Timedelta(days=delay)
            if ref_ts > end_ts:
                continue
            txn_counter += 1
            transactions.append(
                {
                    "user_id": uid,
                    "transaction_id": f"txn_{txn_counter:07d}",
                    "transaction_date": ref_ts.strftime("%Y-%m-%d"),
                    "amount": -charge_amt,
                    "plan": plan,
                    "is_refund": True,
                }
            )

    # ----- 3.5 frames ------------------------------------------------------
    events_df = pd.DataFrame(events)
    stage_rank = {
        "signup": 0,
        "activation": 1,
        "paid_subscription": 2,
        "renew": 3,
        "cancel": 4,
    }
    events_df["_stage"] = events_df["event_name"].map(stage_rank)
    events_df["_ts"] = pd.to_datetime(events_df["event_ts"])
    events_df = (
        events_df.sort_values(["_ts", "user_id", "_stage"], kind="mergesort")
        .drop(columns=["_stage", "_ts"])
        .reset_index(drop=True)
    )

    tx_df = pd.DataFrame(transactions)
    tx_df["_ts"] = pd.to_datetime(tx_df["transaction_date"])
    tx_df = (
        tx_df.sort_values(["_ts", "user_id", "transaction_id"], kind="mergesort")
        .drop(columns=["_ts"])
        .reset_index(drop=True)
    )

    users_df = pd.DataFrame(
        {
            "user_id": user_ids,
            "signup_date": signup_date.strftime("%Y-%m-%d"),
            "country": country,
            "channel": channel,
            "platform": platform,
            "experiment_group": exp_group,
            "acquisition_cost": np.round(cac, 2),
        }
    )
    users_df["_ts"] = pd.to_datetime(users_df["signup_date"])
    users_df = (
        users_df.sort_values(["_ts", "user_id"], kind="mergesort")
        .drop(columns=["_ts"])
        .reset_index(drop=True)
    )

    return events_df, tx_df, users_df


# ===========================================================================
# 4. Self-checks + write
# ===========================================================================
def main() -> None:
    here = Path(__file__).resolve().parent
    events_df, tx_df, users_df = generate()

    # --- sanity assertions (fail loudly rather than write a broken dataset) ---
    assert len(users_df) == N_USERS, "user count mismatch"
    assert (
        events_df["event_name"]
        .isin(["signup", "activation", "paid_subscription", "renew", "cancel"])
        .all()
    )
    assert events_df["user_id"].nunique() == N_USERS
    assert tx_df["user_id"].nunique() <= N_USERS
    assert set(tx_df["plan"]) <= set(PLANS)
    assert (tx_df.loc[~tx_df["is_refund"], "amount"] > 0).all()
    assert (tx_df.loc[tx_df["is_refund"], "amount"] < 0).all()
    assert events_df["experiment_group"].isin(["control", "treatment", "none"]).all()
    one_group = events_df.groupby("user_id")["experiment_group"].nunique().max() == 1
    assert one_group, "a user has more than one experiment_group"

    # experiment must show a real target-metric lift
    ev = events_df
    act = set(ev.loc[ev["event_name"] == "activation", "user_id"])
    paid = set(ev.loc[ev["event_name"] == "paid_subscription", "user_id"])
    exp_users = ev.loc[ev["experiment_group"] != "none"]
    grp_of = dict(zip(exp_users["user_id"], exp_users["experiment_group"], strict=False))
    rate = {}
    for g in ("control", "treatment"):
        arm = {u for u, gg in grp_of.items() if gg == g}
        arm_act = arm & act
        arm_paid = arm & paid
        rate[g] = len(arm_paid) / len(arm_act) if arm_act else float("nan")
    lift = rate["treatment"] / rate["control"] - 1.0

    events_df.to_csv(here / "events.csv", index=False)
    tx_df.to_csv(here / "transactions.csv", index=False)
    users_df.to_csv(here / "users.csv", index=False)

    print(f"Wrote {here}/events.csv        rows={len(events_df):,}")
    print(f"Wrote {here}/transactions.csv  rows={len(tx_df):,}")
    print(f"Wrote {here}/users.csv         rows={len(users_df):,}")
    print(
        f"period            : {users_df['signup_date'].min()} .. {users_df['signup_date'].max()}"
    )
    print(
        f"experiment cohort : {int((users_df['experiment_group'] != 'none').sum()):,} users"
    )
    print(
        f"control/treatment : {(users_df['experiment_group'] == 'control').sum()} / "
        f"{(users_df['experiment_group'] == 'treatment').sum()}"
    )
    print(
        f"activation->paid  : control={rate['control']:.4f}  treatment={rate['treatment']:.4f}  "
        f"lift={lift:+.1%} (relative)"
    )
    print(
        f"paid users        : {tx_df.loc[~tx_df['is_refund'], 'user_id'].nunique():,}"
    )
    print(f"gross revenue RUB : {tx_df['amount'].sum():,.0f}")


if __name__ == "__main__":
    main()
