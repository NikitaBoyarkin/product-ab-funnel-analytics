#!/usr/bin/env python
"""RFM segmentation runner for product-ab-funnel-analytics (Leg 03: `rfm`).

Delegate, do not reimplement: the RFM maths live in the canonical core
``.claude/skills/rfm-segmentation/scripts/rfm.py`` (build_rfm_table, score_rfm,
segment_rfm, segment_summary, segment_revenue_concentration, lorenz_gini).
This file only wires the project's raw ledger into that core and writes the
project artifacts.

Produces:
  * data/processed/rfm_scored.csv    — 1 row per paying user: R/F/M + segment
  * data/processed/segment_summary.csv — per-segment users, revenue share, avg R/F/M
  * analysis/rfm_metrics.json        — every number cited in docs/insights/03-rfm.md
  * figures/rfm-segments.png         — customer share vs revenue share per segment
  * figures/rfm-lorenz.png           — Lorenz curve of monetary concentration

Decisions (documented, not silent):
  * Refunds: KEEP CHARGES ONLY (amount > 0). A refund is not a purchase, so it
    must not inflate `frequency`, and it must not net out a user who did pay.
    On disk: 16,044 charges + 433 refunds; 0 users have refunds without a charge,
    so the paying base stays at 3,384 users. Gross charges = 12,205,060 RUB.
  * Recency snapshot = 2024-12-31, the dataset max transaction date (== period end).
  * Scoring bins = 4 (quartiles), the core's default; scores 1..4, RFMScore 3..12.

Run:
    uv run --project "<env>" python analysis/run_03_rfm.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_DIR / "data" / "raw"
PROCESSED_DIR = PROJECT_DIR / "data" / "processed"
ANALYSIS_DIR = PROJECT_DIR / "analysis"
FIG_DIR = PROJECT_DIR / "figures"
SQL_PATH = PROJECT_DIR / "sql" / "03_rfm.sql"

# Canonical RFM core (delegated — never reimplemented here).
# The DSWoK skills root is env-overridable; the author's path is only the default.
SKILLS_ROOT = Path(
    os.environ.get(
        "DSWOK_SKILLS_ROOT",
        "/Users/nikitaboarkin/Desktop/00 obsidian/obsidian/.claude/skills",
    )
)
RFM_CORE_DIR = SKILLS_ROOT / "rfm-segmentation" / "scripts"
sys.path.insert(0, str(RFM_CORE_DIR))
import rfm  # noqa: E402

BINS = 4
SNAPSHOT_DATE = "2024-12-31"  # dataset max transaction date == analysis period end

DTYPES_TXN = {
    "user_id": "str",
    "transaction_id": "str",
    "transaction_date": "str",
    "amount": "float64",
    "plan": "str",
    "is_refund": "str",
}

PALETTE = {
    "customers": "#1f77b4",
    "revenue": "#2ca02c",
    "line": "#d62728",
    "muted": "#7f7f7f",
}


# --------------------------------------------------------------------------- load
def load_transactions() -> pd.DataFrame:
    """Read the ledger with explicit dtypes; parse date; derive refund flag."""
    txn = pd.read_csv(RAW_DIR / "transactions.csv", dtype=DTYPES_TXN)
    txn["transaction_date"] = pd.to_datetime(txn["transaction_date"], format="%Y-%m-%d")
    txn["is_refund_bool"] = txn["is_refund"].eq("True")
    return txn


# --------------------------------------------------------------------------- core
def build_scored(txn: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Run the delegated core pipeline and return (scored, input_facts)."""
    n_total = len(txn)
    n_refunds = int(txn["is_refund_bool"].sum())
    n_charges = n_total - n_refunds

    # Rule: keep charges only (amount > 0).
    charges = txn[txn["amount"] > 0]

    users_all = set(txn["user_id"])
    users_charged = set(charges["user_id"])

    facts = {
        "transactions_total": n_total,
        "charges_kept": n_charges,
        "refunds_excluded": n_refunds,
        "refund_share_pct": round(100 * n_refunds / n_total, 2),
        "users_with_charge": len(users_charged),
        "users_only_refunds": len(users_all - users_charged),
        "gross_charges_revenue_rub": float(charges["amount"].sum()),
        "net_revenue_rub": float(txn["amount"].sum()),
        "max_transaction_date": str(txn["transaction_date"].max().date()),
        "min_transaction_date": str(txn["transaction_date"].min().date()),
        "snapshot_date": SNAPSHOT_DATE,
        "bins": BINS,
    }

    # The core hardcodes the column name `customer_id` in its aggregation and
    # summary helpers; map our `user_id` to it here (single documented shim).
    work = charges.rename(columns={"user_id": "customer_id"})

    rfm_table = rfm.build_rfm_table(
        work,
        snapshot_date=SNAPSHOT_DATE,
        customer_col="customer_id",
        date_col="transaction_date",
        amount_col="amount",
    )
    scored = rfm.segment_rfm(rfm.score_rfm(rfm_table, bins=BINS), bins=BINS)
    return scored, facts


# --------------------------------------------------------------------------- metrics
def build_metrics(scored: pd.DataFrame, facts: dict) -> dict:
    """Aggregate every headline number the report cites."""
    gini, cum_customers, cum_revenue = rfm.lorenz_gini(scored)

    # Top-X% revenue share from the Lorenz curve (X% most valuable customers).
    # `cum_revenue` is ASCENDING (bottom → top, per the core's `lorenz_gini`),
    # so the top-pct share is the complement of the bottom-(1−pct) cumulative
    # share — NOT `cum_revenue[idx]` itself (that is the bottom share).
    def top_share(pct: float) -> float:
        n = len(cum_revenue) - 1  # cum_revenue carries an extra (0, 0) anchor
        k = int(round(pct * n))  # number of top customers
        idx = n - k  # bottom (n − k) customers
        return round(100 * (1 - float(cum_revenue[idx])), 2)

    summary = rfm.segment_summary(scored)
    conc = rfm.segment_revenue_concentration(scored)
    merged = summary.merge(conc, on=["Segment", "customers"], how="left")
    merged = merged.sort_values("share_revenue_%", ascending=False)

    segments = [
        {
            "segment": r["Segment"],
            "customers": int(r["customers"]),
            "share_customers_pct": float(r["share_customers_%"]),
            "revenue_rub": float(r["revenue"]),
            "share_revenue_pct": float(r["share_revenue_%"]),
            "concentration_ratio": float(r["concentration_ratio"]),
            "avg_recency_days": float(r["avg_recency"]),
            "avg_frequency": float(r["avg_frequency"]),
            "avg_monetary_rub": float(r["avg_monetary"]),
            "avg_rfm_score": float(r["avg_rfm_score"]),
        }
        for r in merged.to_dict("records")
    ]

    champ = next(s for s in segments if s["segment"] == "Champions")
    top_rev_seg = segments[0]
    champ_loyal = sum(
        s["revenue_rub"] for s in segments if s["segment"] in ("Champions", "Loyal")
    )
    atrisk_hibern = sum(
        s["revenue_rub"] for s in segments if s["segment"] in ("At Risk", "Hibernating")
    )
    atrisk_hibern_users = sum(
        s["customers"] for s in segments if s["segment"] in ("At Risk", "Hibernating")
    )
    total_rev = float(scored["monetary_value"].sum())

    metrics = {
        "leg": "rfm",
        "inputs": facts,
        "users_scored": int(len(scored)),
        "segments_count": int(scored["Segment"].nunique()),
        "avg_rfm_score": round(float(scored["RFMScore"].mean()), 2),
        "median_recency_days": float(scored["recency"].median()),
        "median_frequency": float(scored["frequency"].median()),
        "median_monetary_rub": float(scored["monetary_value"].median()),
        "gini": round(float(gini), 4),
        "top10pct_revenue_share_pct": top_share(0.10),
        "top20pct_revenue_share_pct": top_share(0.20),
        "champions_revenue_share_pct": champ["share_revenue_pct"],
        "champions_users": champ["customers"],
        "top_revenue_segment": top_rev_seg["segment"],
        "top_revenue_segment_share_pct": top_rev_seg["share_revenue_pct"],
        "champions_loyal_revenue_share_pct": round(100 * champ_loyal / total_rev, 2),
        "atrisk_hibernating_revenue_share_pct": round(
            100 * atrisk_hibern / total_rev, 2
        ),
        "atrisk_hibernating_users": int(atrisk_hibern_users),
        "total_revenue_rub": total_rev,
        "segments": segments,
    }
    return metrics, cum_customers, cum_revenue


def sql_parity(scored: pd.DataFrame) -> dict:
    """Cross-check the DuckDB SQL port against the Python core (Ratchet)."""
    out = {"checked": False}
    try:
        import duckdb
    except ImportError:
        return out
    sql_text = SQL_PATH.read_text()
    body = "\n".join(
        line for line in sql_text.splitlines() if not line.strip().startswith("--")
    )
    stmt = body.split(";")[0].strip()
    sql_df = duckdb.connect().execute(stmt).df()
    py_counts = scored["Segment"].value_counts().to_dict()
    sql_counts = sql_df["segment"].value_counts().to_dict()
    out = {
        "checked": True,
        "rows_python": int(len(scored)),
        "rows_sql": int(len(sql_df)),
        "segment_counts_match": py_counts == sql_counts,
    }
    return out


# --------------------------------------------------------------------------- write
def write_artifacts(scored: pd.DataFrame, metrics: dict) -> dict:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Per-user scored table (rename core's `customer_id` back to `user_id`).
    per_user = scored.rename(
        columns={"customer_id": "user_id", "monetary_value": "monetary"}
    )[
        [
            "user_id",
            "recency",
            "frequency",
            "monetary",
            "R_Score",
            "F_Score",
            "M_Score",
            "RFMClass",
            "RFMScore",
            "Segment",
        ]
    ].sort_values("RFMScore", ascending=False)
    per_user.to_csv(PROCESSED_DIR / "rfm_scored.csv", index=False)

    # 2. Segment summary: users, revenue share, avg R/F/M.
    summary = rfm.segment_summary(scored)
    conc = rfm.segment_revenue_concentration(scored)
    seg = summary.merge(conc, on=["Segment", "customers"], how="left")
    seg = seg.sort_values("share_revenue_%", ascending=False).rename(
        columns={
            "Segment": "segment",
            "customers": "users",
            "avg_recency": "avg_recency_days",
            "avg_frequency": "avg_frequency",
            "avg_monetary": "avg_monetary_rub",
            "avg_rfm_score": "avg_rfm_score",
            "revenue": "revenue_rub",
        }
    )
    seg.to_csv(PROCESSED_DIR / "segment_summary.csv", index=False)

    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    with (ANALYSIS_DIR / "rfm_metrics.json").open("w") as fh:
        json.dump(metrics, fh, ensure_ascii=False, indent=2)

    return {"rfm_scored": str(per_user.shape), "segment_summary": str(seg.shape)}


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
    metrics: dict, cum_customers: np.ndarray, cum_revenue: np.ndarray
) -> list[str]:
    _style()
    import matplotlib.pyplot as plt

    FIG_DIR.mkdir(parents=True, exist_ok=True)
    made: list[str] = []
    segs = metrics["segments"]
    labels = [s["segment"] for s in segs]
    cust = [s["share_customers_pct"] for s in segs]
    rev = [s["share_revenue_pct"] for s in segs]

    # 1. Customer share vs revenue share per segment.
    y = np.arange(len(labels))
    fig, ax = plt.subplots(figsize=(11, 6.5), layout="constrained")
    h = 0.38
    b1 = ax.barh(
        y - h / 2, cust, height=h, color=PALETTE["customers"], label="доля клиентов, %"
    )
    b2 = ax.barh(
        y + h / 2, rev, height=h, color=PALETTE["revenue"], label="доля выручки, %"
    )
    ax.bar_label(b1, fmt="%.1f", padding=3, fontsize=10)
    ax.bar_label(b2, fmt="%.1f", padding=3, fontsize=10, fontweight="bold")
    ax.set_yticks(y, labels)
    ax.invert_yaxis()
    ax.set_xlabel("доля, %")
    ax.set_title(
        f"Выручку держат Loyal и Champions: {metrics['champions_loyal_revenue_share_pct']:.1f}% "
        f"при {sum(s['share_customers_pct'] for s in segs if s['segment'] in ('Champions', 'Loyal')):.1f}% клиентов"
    )
    ax.legend(loc="lower right", frameon=False)
    ax.grid(axis="x", alpha=0.25)
    p = FIG_DIR / "rfm-segments.png"
    fig.savefig(p, dpi=300, bbox_inches="tight")
    plt.close(fig)
    made.append(str(p.relative_to(PROJECT_DIR)))

    # 2. Lorenz curve of monetary concentration.
    fig, ax = plt.subplots(figsize=(7.5, 7), layout="constrained")
    ax.plot(
        [0, 1],
        [0, 1],
        "--",
        color=PALETTE["muted"],
        linewidth=1.2,
        label="равномерность",
    )
    ax.plot(
        cum_customers,
        cum_revenue,
        color=PALETTE["line"],
        linewidth=2.2,
        label="факт (Lorenz)",
    )
    ax.fill_between(
        cum_customers, cum_revenue, cum_customers, color=PALETTE["line"], alpha=0.12
    )
    ax.set_xlabel("накопленная доля клиентов")
    ax.set_ylabel("накопленная доля выручки")
    ax.set_title(f"Концентрация выручки: Gini = {metrics['gini']:.3f}")
    ax.text(
        0.05,
        0.82,
        f"топ-10% клиентов → {metrics['top10pct_revenue_share_pct']:.1f}% выручки\n"
        f"топ-20% клиентов → {metrics['top20pct_revenue_share_pct']:.1f}% выручки",
        fontsize=11,
        color="#333333",
    )
    ax.legend(loc="upper left", frameon=False)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.grid(alpha=0.25)
    p = FIG_DIR / "rfm-lorenz.png"
    fig.savefig(p, dpi=300, bbox_inches="tight")
    plt.close(fig)
    made.append(str(p.relative_to(PROJECT_DIR)))

    return made


# --------------------------------------------------------------------------- main
def main() -> int:
    txn = load_transactions()
    scored, facts = build_scored(txn)
    metrics, cum_customers, cum_revenue = build_metrics(scored, facts)
    metrics["sql_parity"] = sql_parity(scored)
    shapes = write_artifacts(scored, metrics)
    made = figures(metrics, cum_customers, cum_revenue)
    metrics["figures"] = made

    # Re-write metrics now that parity + figures are attached.
    with (ANALYSIS_DIR / "rfm_metrics.json").open("w") as fh:
        json.dump(metrics, fh, ensure_ascii=False, indent=2)

    print(
        f"users scored      : {metrics['users_scored']} across {metrics['segments_count']} segments"
    )
    print(
        f"refund rule       : charges only (amount>0); excluded {facts['refunds_excluded']} refunds "
        f"({facts['refund_share_pct']}%)"
    )
    print(f"snapshot (recency): {facts['snapshot_date']}")
    print(f"Gini              : {metrics['gini']}")
    print(
        f"Champions revenue : {metrics['champions_revenue_share_pct']}% "
        f"({metrics['champions_users']} users)"
    )
    print(f"SQL parity        : {metrics['sql_parity']}")
    print("artifacts         :", shapes)
    for m in made:
        print("figure            :", m)
    return 0


if __name__ == "__main__":
    sys.exit(main())
