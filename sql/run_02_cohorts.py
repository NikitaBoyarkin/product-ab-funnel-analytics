#!/usr/bin/env python
"""Cohort retention matrix for product-ab-funnel-analytics (Leg 02: `cohorts`).

Runs the DuckDB query in `sql/02_cohorts.sql` (two variants: activity retention
and paid retention), then writes every number cited in
`docs/insights/02-cohorts.md`:

  * `sql/results/02_cohort_matrix.csv`   — activity-retention matrix (M0…M11, %)
  * `sql/results/02_cohort_metrics.json` — all machine-checkable numbers
  * `figures/cohort-heatmap.png`         — activity-retention heatmap (dpi=300)

Run:
    uv run --project "<env>" python sql/run_02_cohorts.py
    uv run --project "<env>" python sql/run_02_cohorts.py --no-figures

Design notes (house style):
  * the SQL file is the single source of truth — this runner only executes it;
  * cells beyond the observation window stay NULL (right-censoring), never 0;
  * pooled headline retention is user-weighted (sum active / sum cohort size),
    not a naive average of cohort rows.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import duckdb
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent
SQL_DIR = PROJECT_DIR / "sql"
RESULTS_DIR = SQL_DIR / "results"
FIG_DIR = PROJECT_DIR / "figures"
SQL_FILE = SQL_DIR / "02_cohorts.sql"

M_COLS = [f"M{i}" for i in range(12)]


# --------------------------------------------------------------------------- SQL
def load_queries() -> dict[str, str]:
    """Split `02_cohorts.sql` on `-- @QUERY: <name>` markers."""
    text = SQL_FILE.read_text(encoding="utf-8")
    parts = re.split(r"^-- @QUERY:\s*(\w+)\s*$", text, flags=re.MULTILINE)
    queries = {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}
    if not queries:
        raise RuntimeError(f"no `-- @QUERY:` markers found in {SQL_FILE}")
    return queries


def run_query(con: duckdb.DuckDBPyConnection, sql: str) -> pd.DataFrame:
    return con.execute(sql).df()


# --------------------------------------------------------------------------- metrics
def matrix_records(df: pd.DataFrame) -> dict[str, dict[str, float | None]]:
    """cohort_month -> {M0..M11: pct|None}."""
    out: dict[str, dict[str, float | None]] = {}
    for _, row in df.iterrows():
        out[str(row["cohort_month"])] = {
            m: (None if pd.isna(row[m]) else float(row[m])) for m in M_COLS
        }
    return out


def pooled_retention(df: pd.DataFrame, n_col: str = "cohort_size") -> dict[str, dict]:
    """User-weighted retention per month index: sum(active)/sum(size) over the
    cohorts where that month is observable. `active` is recovered from
    pct * cohort_size."""
    out: dict[str, dict] = {}
    for m in M_COLS:
        sub = df[df[m].notna()]
        if sub.empty:
            out[m] = {"pct": None, "cohorts": 0, "active": 0, "size": 0}
            continue
        size = float(sub[n_col].sum())
        active = float(((sub[m] / 100.0) * sub[n_col]).sum())
        out[m] = {
            "pct": round(active / size * 100, 2),
            "cohorts": int(len(sub)),
            "active": int(round(active)),
            "size": int(size),
        }
    return out


def headline(act: pd.DataFrame, paid: pd.DataFrame) -> dict:
    """Headline numbers cited in prose."""
    pooled_act = pooled_retention(act)
    pooled_paid = pooled_retention(paid)

    # best / worst cohort by M3 retention among cohorts that can observe M3
    m3 = act[act["M3"].notna()].copy()
    best = m3.loc[m3["M3"].idxmax()]
    worst = m3.loc[m3["M3"].idxmin()]

    # drop M0 -> M1 (largest single-month drop), user-weighted
    m0, m1 = pooled_act["M0"]["pct"], pooled_act["M1"]["pct"]
    drop_m0_m1_pp = round(m0 - m1, 2) if (m0 is not None and m1 is not None) else None

    # flattening: delta between the last two observable pooled points
    obs = [
        (m, pooled_act[m]["pct"]) for m in M_COLS if pooled_act[m]["pct"] is not None
    ]
    tail_delta_pp = round(obs[-2][1] - obs[-1][1], 2) if len(obs) >= 2 else None

    # activity vs paid gap at M3 (pooled)
    gap_m3_pp = (
        round(pooled_act["M3"]["pct"] - pooled_paid["M3"]["pct"], 2)
        if pooled_act["M3"]["pct"] is not None and pooled_paid["M3"]["pct"] is not None
        else None
    )

    return {
        "activity_pooled": pooled_act,
        "paid_pooled": pooled_paid,
        "best_cohort_by_M3": {
            "cohort_month": str(best["cohort_month"]),
            "M3_pct": float(best["M3"]),
            "cohort_size": int(best["cohort_size"]),
        },
        "worst_cohort_by_M3": {
            "cohort_month": str(worst["cohort_month"]),
            "M3_pct": float(worst["M3"]),
            "cohort_size": int(worst["cohort_size"]),
        },
        "drop_M0_to_M1_pp": drop_m0_m1_pp,
        "tail_flatten_delta_pp": tail_delta_pp,
        "activity_minus_paid_M3_pp": gap_m3_pp,
        "max_observable_index": obs[-1][0],
        "M12_observable": False,
        "dec_cohort_size": int(
            act.loc[act["cohort_month"] == "2024-12", "cohort_size"].iloc[0]
        ),
    }


# --------------------------------------------------------------------------- figure
def _style() -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.family": ["DejaVu Sans", "sans-serif"],
            "axes.unicode_minus": False,
            "font.size": 12,
            "axes.titlesize": 14,
            "axes.titleweight": "bold",
            "axes.labelsize": 12,
            "figure.facecolor": "white",
            "figure.dpi": 100,
        }
    )


def figure(act: pd.DataFrame) -> str:
    _style()
    import matplotlib.pyplot as plt
    import numpy as np

    mat = act[M_COLS].to_numpy(dtype=float)
    masked = np.ma.masked_invalid(mat)

    fig, ax = plt.subplots(figsize=(12.5, 6.4), layout="constrained")
    cmap = plt.get_cmap("YlGnBu").copy()
    cmap.set_bad(color="#f0f0f0")
    im = ax.imshow(masked, aspect="auto", cmap=cmap, vmin=0, vmax=100)

    ax.set_xticks(range(len(M_COLS)), M_COLS)
    ax.set_yticks(
        range(len(act)),
        [f"{c}  (n={n})" for c, n in zip(act["cohort_month"], act["cohort_size"])],
    )
    for i in range(mat.shape[0]):
        for j in range(mat.shape[1]):
            if not np.isnan(mat[i, j]):
                ax.text(
                    j,
                    i,
                    f"{mat[i, j]:.0f}",
                    ha="center",
                    va="center",
                    fontsize=8.5,
                    color="white" if mat[i, j] > 55 else "#222222",
                )
    ax.set(
        title="Когортное удержание падает на M0→M1 и выходит на плато к M6; "
        "поздние когорты обрезаны цензурированием (серое)",
        xlabel="Месяц с регистрации (M0 = месяц регистрации)",
        ylabel="Месяц регистрации (когорта)",
    )
    cbar = fig.colorbar(im, ax=ax, fraction=0.035, pad=0.02)
    cbar.set_label("Удержание, % активных (любое событие)")
    p = FIG_DIR / "cohort-heatmap.png"
    fig.savefig(p, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return str(p.relative_to(PROJECT_DIR))


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Cohort matrix — product-ab-funnel-analytics"
    )
    ap.add_argument("--no-figures", action="store_true", help="skip PNG rendering")
    args = ap.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    queries = load_queries()

    os.chdir(PROJECT_DIR)  # so relative read_csv_auto('data/raw/*.csv') paths resolve
    con = duckdb.connect()
    act = run_query(con, queries["activity_retention"])
    paid = run_query(con, queries["paid_retention"])
    con.close()

    # ---- deliverable CSV: activity-retention matrix
    csv_path = RESULTS_DIR / "02_cohort_matrix.csv"
    act.to_csv(csv_path, index=False, na_rep="", float_format="%.2f")

    head = headline(act, paid)
    metrics = {
        "meta": {
            "duckdb_version": duckdb.__version__,
            "pandas_version": pd.__version__,
            "sql_file": "sql/02_cohorts.sql",
            "source_files": {
                "users": "data/raw/users.csv",
                "events": "data/raw/events.csv",
            },
            "cohort_definition": "месяц регистрации (users.signup_date)",
            "activity_definition": "любое событие в месяце (events.event_ts)",
            "paid_definition": "событие paid_subscription или renew в месяце",
            "note_censoring": "данные кончаются 2024-12-31; M12 недостижим ни для одной когорты",
        },
        "cohorts": int(len(act)),
        "cohort_sizes": {
            str(r["cohort_month"]): int(r["cohort_size"]) for _, r in act.iterrows()
        },
        "activity_matrix_pct": matrix_records(act),
        "paid_matrix_pct": matrix_records(paid),
        "headline": head,
    }

    json_path = RESULTS_DIR / "02_cohort_metrics.json"
    json_path.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    made_fig = None
    if not args.no_figures:
        made_fig = figure(act)

    # ---- human-readable digest
    print("=" * 78)
    print("COHORT MATRIX — product-ab-funnel-analytics (Leg 02)")
    print(f"duckdb {duckdb.__version__} | csv -> {csv_path.relative_to(PROJECT_DIR)}")
    print("=" * 78)
    print("\n-- ACTIVITY RETENTION MATRIX (% активных в месяце M) --------------------")
    print("cohort   n   " + " ".join(f"{m:>6}" for m in M_COLS))
    for _, r in act.iterrows():
        cells = " ".join(
            (f"{r[m]:>6.1f}" if pd.notna(r[m]) else f"{'—':>6}") for m in M_COLS
        )
        print(f"{r['cohort_month']} {int(r['cohort_size']):>4}   {cells}")
    print("\n-- POOLED (user-weighted) RETENTION -------------------------------------")
    for m in M_COLS:
        a = head["activity_pooled"][m]
        p = head["paid_pooled"][m]
        if a["pct"] is None:
            print(f"  {m:>3}  activity —      | paid —")
        else:
            print(
                f"  {m:>3}  activity {a['pct']:>6.2f}%  (n_cohorts={a['cohorts']:>2}, "
                f"size={a['size']:>5}) | paid {p['pct']:>6.2f}%"
            )
    print("\n-- HEADLINE -------------------------------------------------------------")
    print(f"  drop M0->M1 (pp)            {head['drop_M0_to_M1_pp']}")
    print(f"  best cohort by M3           {head['best_cohort_by_M3']}")
    print(f"  worst cohort by M3          {head['worst_cohort_by_M3']}")
    print(f"  tail flatten delta (pp)     {head['tail_flatten_delta_pp']}")
    print(f"  activity - paid @ M3 (pp)   {head['activity_minus_paid_M3_pp']}")
    print(f"  max observable index        {head['max_observable_index']}")
    print(f"  Dec cohort size             {head['dec_cohort_size']}")
    if made_fig:
        print(
            f"\n-- FIGURE ---------------------------------------------------------------\n  {made_fig}"
        )
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
