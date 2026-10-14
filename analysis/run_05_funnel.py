#!/usr/bin/env python
"""Funnel leg (05) for product-ab-funnel-analytics.

Produces:
  * ``sql/results/05_funnel_steps.csv``     — per-step funnel metrics (main + renew variant)
  * ``sql/results/05_funnel_segments.csv``  — segment ranking by overall conversion
  * ``analysis/05_funnel_metrics.json``     — every number cited in ``docs/insights/05-funnel.md``
  * ``figures/funnel.png``                  — funnel + segment chart (dpi=300)

Run:
    uv run --project "<env>" python analysis/run_05_funnel.py
    uv run --project "<env>" python analysis/run_05_funnel.py --no-figures

Design notes (house style):
  * the funnel core is NOT reimplemented — ``funnel_analyzer.py`` from the
    ``funnel-analysis`` skill is imported (``analyze_funnel``, ``biggest_dropoff``,
    ``analyze_segments``, ``format_funnel_table``);
  * all counts come from DuckDB executing ``sql/05_funnel.sql`` — the same file a
    reviewer can run standalone;
  * every number in the report is emitted to JSON, so it is checkable by literal search.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import duckdb
import pandas as pd

# --------------------------------------------------------------------------- paths
PROJECT_DIR = Path(__file__).resolve().parent.parent
SQL_DIR = PROJECT_DIR / "sql"
RES_DIR = SQL_DIR / "results"
FIG_DIR = PROJECT_DIR / "figures"
OUT_DIR = PROJECT_DIR / "analysis"

# --------------------------------------------------------------------------- delegated core
# Canonical funnel core (skill `funnel-analysis`). The DSWoK skills root is
# env-overridable; the author's path is only the default.
SKILLS_ROOT = Path(
    os.environ.get(
        "DSWOK_SKILLS_ROOT",
        "/Users/nikitaboarkin/Desktop/00 obsidian/obsidian/.claude/skills",
    )
)
FUNNEL_CORE_DIR = SKILLS_ROOT / "funnel-analysis" / "scripts"
sys.path.insert(0, str(FUNNEL_CORE_DIR))
from funnel_analyzer import (  # noqa: E402
    analyze_funnel,
    analyze_segments,
    biggest_dropoff,
    format_funnel_table,
)

# --------------------------------------------------------------------------- constants
STEPS_MAIN = ["signup", "activation", "paid_subscription"]
STEPS_RENEW = ["signup", "activation", "paid_subscription", "renew"]
STEP_RU = {
    "signup": "регистрация",
    "activation": "активация",
    "paid_subscription": "первый платёж",
    "renew": "продление",
}
SEGMENT_TYPES = ["channel", "platform", "country", "experiment_group"]


# --------------------------------------------------------------------------- sql
def run_sql(sql_path: Path) -> pd.DataFrame:
    """Execute a .sql file through DuckDB, resolving data paths from the project root."""
    con = duckdb.connect()
    con.execute(f"SET file_search_path = '{PROJECT_DIR}'")
    sql = sql_path.read_text(encoding="utf-8")
    return con.execute(sql).df()


# --------------------------------------------------------------------------- steps
def build_steps(df: pd.DataFrame) -> tuple[list[dict], list[dict], dict]:
    """Return (main_funnel_rows, renew_variant_rows, steps_by_scope)."""
    overall = df[df["scope_type"] == "overall"].sort_values("step_index")
    counts = {row["step"]: int(row["users"]) for _, row in overall.iterrows()}

    main_rows = analyze_funnel(STEPS_MAIN, [counts[s] for s in STEPS_MAIN])
    renew_rows = analyze_funnel(STEPS_RENEW, [counts[s] for s in STEPS_RENEW])
    return main_rows, renew_rows, counts


def steps_dataframe(main_rows: list[dict], renew_rows: list[dict]) -> pd.DataFrame:
    cols = [
        "step_index",
        "step",
        "users",
        "step_conversion",
        "step_conversion_pct",
        "overall_conversion",
        "overall_conversion_pct",
        "dropoff_n",
        "dropoff_pct",
        "impact_score",
    ]
    main = pd.DataFrame(main_rows)[cols]
    main.insert(0, "funnel", "main")
    renew = pd.DataFrame(renew_rows)[cols]
    renew.insert(0, "funnel", "with_renew")
    return pd.concat([main, renew], ignore_index=True)


# --------------------------------------------------------------------------- segments
def build_segments(df: pd.DataFrame) -> tuple[dict, dict[str, str]]:
    """Build {display_name: (steps, counts)} for the main funnel + a name->type map."""
    seg_input: dict[str, tuple[list[str], list[int]]] = {}
    name_to_type: dict[str, str] = {}
    for stype in SEGMENT_TYPES:
        sub = df[df["scope_type"] == stype]
        for value, grp in sub.groupby("scope_value", observed=True):
            grp = grp.sort_values("step_index")
            counts = {row["step"]: int(row["users"]) for _, row in grp.iterrows()}
            name = f"{stype}: {value}"
            seg_input[name] = (STEPS_MAIN, [counts[s] for s in STEPS_MAIN])
            name_to_type[name] = stype
    return seg_input, name_to_type


def segments_dataframe(
    seg_rows: list[dict], name_to_type: dict[str, str]
) -> pd.DataFrame:
    records = []
    for s in seg_rows:
        if "error" in s:
            continue
        rows = s["rows"]
        by_step = {r["step"]: r for r in rows}
        records.append(
            {
                "segment_type": name_to_type[s["segment"]],
                "segment": s["segment"].split(": ", 1)[1],
                "signup_users": by_step["signup"]["users"],
                "activation_users": by_step["activation"]["users"],
                "paid_users": by_step["paid_subscription"]["users"],
                "activation_rate_pct": by_step["activation"]["step_conversion_pct"],
                "paid_given_activated_pct": by_step["paid_subscription"][
                    "step_conversion_pct"
                ],
                "overall_conversion_pct": s["overall_conversion_pct"],
                "biggest_dropoff_step": s["biggest_dropoff_step"],
            }
        )
    out = pd.DataFrame(records)
    return out.sort_values(
        ["segment_type", "overall_conversion_pct"], ascending=[True, False]
    ).reset_index(drop=True)


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
            "axes.titlesize": 13,
            "axes.titleweight": "bold",
            "axes.labelsize": 11,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "figure.facecolor": "white",
            "figure.dpi": 100,
        }
    )


def figure(main_rows: list[dict], seg_df: pd.DataFrame) -> str:
    _style()
    import matplotlib.pyplot as plt

    FIG_DIR.mkdir(parents=True, exist_ok=True)
    path = FIG_DIR / "funnel.png"

    fig, (ax1, ax2) = plt.subplots(
        1,
        2,
        figsize=(14, 6),
        layout="constrained",
        gridspec_kw={"width_ratios": [1.15, 1]},
    )

    # --- panel 1: main funnel, users + step conversion
    labels = [STEP_RU[r["step"]] for r in main_rows]
    users = [r["users"] for r in main_rows]
    bars = ax1.bar(range(len(users)), users, color="#1f77b4", width=0.62)
    ax1.bar_label(
        bars,
        labels=[f"{u:,}".replace(",", " ") for u in users],
        padding=4,
        fontweight="bold",
        fontsize=11,
    )
    for i, r in enumerate(main_rows):
        if i > 0:
            ax1.text(
                i,
                users[i] * 0.5,
                f"→ {r['step_conversion_pct']:.1f}%\n−{r['dropoff_n']:,}".replace(
                    ",", " "
                ),
                ha="center",
                va="center",
                color="white",
                fontweight="bold",
                fontsize=10.5,
            )
    ax1.set(
        title="Главный drop-off — активация → первый платёж: теряется 6 239 из 9 623",
        xlabel="Шаг воронки",
        ylabel="Уникальных пользователей",
        xticks=range(len(labels)),
        xticklabels=labels,
    )
    ax1.set_ylim(0, max(users) * 1.18)

    # --- panel 2: channel ranking by overall conversion (signup → paid)
    ch = seg_df[seg_df["segment_type"] == "channel"].sort_values(
        "overall_conversion_pct"
    )
    colors = [
        "#2ca02c" if v == ch["overall_conversion_pct"].max() else "#1f77b4"
        for v in ch["overall_conversion_pct"]
    ]
    bars2 = ax2.barh(
        ch["segment"], ch["overall_conversion_pct"], color=colors, height=0.62
    )
    ax2.bar_label(
        bars2,
        labels=[f"{v:.1f}%" for v in ch["overall_conversion_pct"]],
        padding=4,
        fontweight="bold",
        fontsize=10.5,
    )
    ax2.set(
        title="Канал: overall-конверсия signup → первый платёж",
        xlabel="Overall conversion, %",
    )
    ax2.set_xlim(0, ch["overall_conversion_pct"].max() * 1.25)

    fig.savefig(path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return str(path.relative_to(PROJECT_DIR))


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="Funnel leg (05)")
    ap.add_argument("--no-figures", action="store_true", help="skip PNG rendering")
    args = ap.parse_args()

    RES_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    df = run_sql(SQL_DIR / "05_funnel.sql")

    main_rows, renew_rows, counts = build_steps(df)
    steps_df = steps_dataframe(main_rows, renew_rows)
    steps_df.to_csv(RES_DIR / "05_funnel_steps.csv", index=False, encoding="utf-8")

    seg_input, name_to_type = build_segments(df)
    seg_rows = analyze_segments(seg_input, value_per_user=1.0)
    seg_df = segments_dataframe(seg_rows, name_to_type)
    seg_df.to_csv(RES_DIR / "05_funnel_segments.csv", index=False, encoding="utf-8")

    top = biggest_dropoff(main_rows)
    top_renew = biggest_dropoff(renew_rows)

    def seg_lookup(stype: str) -> list[dict]:
        sub = seg_df[seg_df["segment_type"] == stype]
        return sub.to_dict("records")

    metrics = {
        "meta": {
            "source": "data/raw/events.csv + data/raw/users.csv (synthetic, seed=42)",
            "engine": f"duckdb {duckdb.__version__}",
            "pandas_version": pd.__version__,
            "sql": "sql/05_funnel.sql",
            "value_per_user": 1.0,
            "impact_score_note": "impact_score = число потерянных пользователей (value_per_user=1.0)",
        },
        "funnel_main": {
            "steps": STEPS_MAIN,
            "counts": {s: counts[s] for s in STEPS_MAIN},
            "rows": main_rows,
            "biggest_dropoff": top,
            "overall_conversion_pct": main_rows[-1]["overall_conversion_pct"],
        },
        "funnel_with_renew": {
            "steps": STEPS_RENEW,
            "counts": {s: counts[s] for s in STEPS_RENEW},
            "rows": renew_rows,
            "biggest_dropoff": top_renew,
            "overall_conversion_pct": renew_rows[-1]["overall_conversion_pct"],
            "ordering_rule": "renew засчитан только если t_renew >= t_paid (0 нарушений в данных)",
        },
        "segments": {stype: seg_lookup(stype) for stype in SEGMENT_TYPES},
    }

    if not args.no_figures:
        metrics["figure"] = figure(main_rows, seg_df)

    (OUT_DIR / "05_funnel_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # ---- human-readable digest
    print("=" * 78)
    print("FUNNEL LEG 05 — product-ab-funnel-analytics")
    print(f"{metrics['meta']['engine']} | steps -> sql/results/05_funnel_steps.csv")
    print("=" * 78)
    print(
        "\n-- MAIN FUNNEL (signup -> activation -> paid) -----------------------------"
    )
    print(format_funnel_table(main_rows, "Main funnel"))
    print(
        "\n-- VARIANT WITH RENEW -----------------------------------------------------"
    )
    print(
        "  renew given paid = "
        f"{renew_rows[3]['step_conversion_pct']:.2f}% | overall = "
        f"{renew_rows[3]['overall_conversion_pct']:.2f}%"
    )
    print(
        "\n-- SEGMENTS (overall conversion, main funnel) -----------------------------"
    )
    print(seg_df.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
