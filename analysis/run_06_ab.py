"""
run_06_ab.py — нога `a/b` проекта product-ab-funnel-analytics.

Что делает:
  1. Гоняет sql/06_ab.sql через DuckDB -> sql/results/06_ab_counts.csv
     (per-group numerator/denominator таблица).
  2. Делегирует статистику в канонный модуль скилла ab-test-analysis
     (srm_check, analyze_binary_metric, format_report) — НЕ переписывает его.
  3. Считает PRIMARY (paid given activated), SECONDARY (activation rate),
     robustness (paid per signup), guardrails (refund / cancel по группам)
     и достигнутую мощность.
  4. Рисует figures/ab-lift.png и пишет analysis/ab_metrics.json.
  5. Печатает человекочитаемый отчёт.

Запуск:
    uv run --project "<env>" python analysis/run_06_ab.py
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import stats

# --- делегирование: канонный анализатор скилла ab-test-analysis ---------------
# В репозитории лежит вендорная копия (vendor/), поэтому чистый клон работает;
# DSWOK_SKILLS_ROOT переопределяет её живой версией скилла.
SKILLS_ROOT = os.environ.get("DSWOK_SKILLS_ROOT")
SKILL_SCRIPTS = (
    Path(SKILLS_ROOT) / "ab-test-analysis" / "scripts"
    if SKILLS_ROOT
    else Path(__file__).resolve().parent.parent / "vendor"
)
sys.path.insert(0, str(SKILL_SCRIPTS))
from ab_test_analyzer import (  # noqa: E402
    analyze_binary_metric,
    format_report,
    srm_check,
)

ROOT = Path(__file__).resolve().parent.parent
SQL_FILE = ROOT / "sql" / "06_ab.sql"
CSV_OUT = ROOT / "sql" / "results" / "06_ab_counts.csv"
FIG_OUT = ROOT / "figures" / "ab-lift.png"
JSON_OUT = ROOT / "analysis" / "ab_metrics.json"

ALPHA = 0.05

# Кириллица в matplotlib
plt.rcParams["font.family"] = "DejaVu Sans"
plt.rcParams["axes.unicode_minus"] = False


def run_sql() -> object:
    """Выполняет SQL-файл, возвращает pandas.DataFrame и сохраняет CSV."""
    con = duckdb.connect()
    con.execute("SET timezone='UTC';")
    con.execute(f"SET file_search_path = '{ROOT}'")
    df = con.execute(SQL_FILE.read_text(encoding="utf-8")).fetchdf()
    con.close()
    CSV_OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(CSV_OUT, index=False)
    return df


def achieved_power(
    p1: float, n1: int, p2: float, n2: int, alpha: float = ALPHA
) -> float:
    """Достигнутая мощность двустороннего z-теста пропорций при наблюдённом эффекте."""
    d = abs(p2 - p1)
    p_pool = (p1 * n1 + p2 * n2) / (n1 + n2)
    se_null = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
    se_alt = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    z_crit = stats.norm.ppf(1 - alpha / 2)
    z_beta = (d - z_crit * se_null) / se_alt
    return float(stats.norm.cdf(z_beta))


def mde_abs(
    p1: float, n1: int, n2: int, power: float = 0.80, alpha: float = ALPHA
) -> float:
    """Минимально детектируемый абсолютный эффект (MDE) при заданной мощности."""
    z_a = stats.norm.ppf(1 - alpha / 2)
    z_b = stats.norm.ppf(power)
    se = math.sqrt(p1 * (1 - p1) * (1 / n1 + 1 / n2))
    return (z_a + z_b) * se


def rate_ci(conv: int, n: int, alpha: float = ALPHA) -> tuple[float, float]:
    """95% доверительный интервал для одной доли (нормальная аппроксимация)."""
    p = conv / n
    se = math.sqrt(p * (1 - p) / n)
    z = stats.norm.ppf(1 - alpha / 2)
    return (p - z * se, p + z * se)


def build_figure(rows: dict) -> None:
    """Бар-чарт PRIMARY-метрики по группам с 95% CI."""
    groups = ["control", "treatment"]
    labels = ["control\n(контроль)", "treatment\n(вариант)"]
    rates = [rows[g]["primary_rate"] for g in groups]
    cis = [rows[g]["primary_ci"] for g in groups]
    err_lo = [r - c[0] for r, c in zip(rates, cis, strict=True)]
    err_hi = [c[1] - r for r, c in zip(rates, cis, strict=True)]

    fig, ax = plt.subplots(figsize=(7.2, 5.2))
    colors = ["#8a8f98", "#2f6f4e"]
    bars = ax.bar(labels, rates, color=colors, width=0.55, zorder=3)
    ax.errorbar(
        labels,
        rates,
        yerr=[err_lo, err_hi],
        fmt="none",
        ecolor="#1a1a1a",
        elinewidth=1.6,
        capsize=6,
        zorder=4,
    )
    for bar, r in zip(bars, rates, strict=True):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            r + 0.012,
            f"{r:.1%}",
            ha="center",
            va="bottom",
            fontsize=12,
            fontweight="bold",
        )

    rel = rows["treatment"]["primary_relative_lift_pct"]
    p = rows["treatment"]["primary_p_value"]
    ax.set_ylim(0, 0.55)
    ax.set_ylabel("paid given activated, %")
    ax.set_title(
        "PRIMARY: конверсия activation → paid по группам\n"
        f"относительный lift {rel:+.1f}%, p = {p:.2e} (двусторонний z-тест)",
        fontsize=12.5,
        fontweight="bold",
    )
    ax.grid(axis="y", alpha=0.25, zorder=0)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.text(
        0.01,
        0.01,
        "Знаменатель — активировавшиеся (1055 / 1035), не вся группа. "
        "Усы — 95% CI доли. Данные синтетические (seed=42).",
        fontsize=8,
        color="#555555",
    )
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    FIG_OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIG_OUT, dpi=300)
    plt.close(fig)


def main() -> None:
    df = run_sql()
    rec = {row["experiment_group"]: row for row in df.to_dict("records")}
    c, t = rec["control"], rec["treatment"]

    # --- SRM ---
    srm = srm_check(int(c["users"]), int(t["users"]), expected_split=0.5)

    # --- PRIMARY: paid given activated ---
    primary = analyze_binary_metric(
        n_control=int(c["paid_given_activated_den"]),
        conv_control=int(c["paid_given_activated_num"]),
        n_treatment=int(t["paid_given_activated_den"]),
        conv_treatment=int(t["paid_given_activated_num"]),
        alpha=ALPHA,
    )

    # --- SECONDARY: activation rate (signup -> activation) ---
    secondary = analyze_binary_metric(
        n_control=int(c["activation_den"]),
        conv_control=int(c["activation_num"]),
        n_treatment=int(t["activation_den"]),
        conv_treatment=int(t["activation_num"]),
        alpha=ALPHA,
    )

    # --- ROBUSTNESS (ITT-стиль): paid per signup ---
    robustness = analyze_binary_metric(
        n_control=int(c["users"]),
        conv_control=int(c["paid_per_signup_num"]),
        n_treatment=int(t["users"]),
        conv_treatment=int(t["paid_per_signup_num"]),
        alpha=ALPHA,
    )

    # --- мощность и MDE по PRIMARY ---
    power = achieved_power(
        primary["rate_control"],
        int(c["paid_given_activated_den"]),
        primary["rate_treatment"],
        int(t["paid_given_activated_den"]),
    )
    mde = mde_abs(
        primary["rate_control"],
        int(c["paid_given_activated_den"]),
        int(t["paid_given_activated_den"]),
    )

    # --- guardrails ---
    guard = {}
    for g, row in rec.items():
        charges, refunds = int(row["charges"]), int(row["refunds"])
        payers, cancelled = int(row["payers"]), int(row["cancelled"])
        guard[g] = {
            "refund_rate": refunds / charges,
            "refund_rate_ci": rate_ci(refunds, charges),
            "refunds": refunds,
            "charges": charges,
            "payers_with_refund_share": int(row["payers_with_refund"]) / payers,
            "cancel_rate_payers": cancelled / payers,
            "cancel_rate_payers_ci": rate_ci(cancelled, payers),
            "cancelled": cancelled,
            "payers": payers,
        }

    # --- 95% CI относительного lift по PRIMARY ---
    rel_ci = (
        primary["ci_lower"] / primary["rate_control"],
        primary["ci_upper"] / primary["rate_control"],
    )

    # --- вердикт ---
    if primary["significant"] and primary["relative_lift_pct"] > 0 and power >= 0.80:
        verdict = "SHIP"
    elif primary["significant"] and primary["relative_lift_pct"] > 0:
        verdict = "EXTEND"
    else:
        verdict = "STOP"

    rows = {
        "control": {
            "primary_rate": primary["rate_control"],
            "primary_ci": rate_ci(
                int(c["paid_given_activated_num"]), int(c["paid_given_activated_den"])
            ),
            "primary_relative_lift_pct": primary["relative_lift_pct"],
            "primary_p_value": primary["p_value"],
        },
        "treatment": {
            "primary_rate": primary["rate_treatment"],
            "primary_ci": rate_ci(
                int(t["paid_given_activated_num"]), int(t["paid_given_activated_den"])
            ),
            "primary_relative_lift_pct": primary["relative_lift_pct"],
            "primary_p_value": primary["p_value"],
        },
    }
    build_figure(rows)

    metrics = {
        "leg": "a/b",
        "alpha": ALPHA,
        "cohort": {
            "n_total": int(c["users"]) + int(t["users"]),
            "n_control": int(c["users"]),
            "n_treatment": int(t["users"]),
        },
        "srm": srm,
        "primary": {
            "metric": "paid_given_activated",
            **primary,
            "relative_lift_ci95": list(rel_ci),
        },
        "secondary": {"metric": "activation_rate", **secondary},
        "robustness_itt": {"metric": "paid_per_signup", **robustness},
        "power": {
            "achieved_power": power,
            "mde_abs": mde,
            "mde_rel_pct": mde / primary["rate_control"] * 100,
        },
        "guardrails": guard,
        "per_group_counts": df.to_dict("records"),
        "verdict": verdict,
    }
    JSON_OUT.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # --- печать ---
    print("=" * 68)
    print("LEG a/b — product-ab-funnel-analytics")
    print("=" * 68)
    print(f"\nCSV: {CSV_OUT.relative_to(ROOT)}   FIG: {FIG_OUT.relative_to(ROOT)}")
    print(f"JSON: {JSON_OUT.relative_to(ROOT)}\n")
    print(format_report(srm, primary, "PRIMARY paid_given_activated"))
    print()
    print(format_report(srm, secondary, "SECONDARY activation_rate"))
    print()
    print(format_report(srm, robustness, "ROBUSTNESS (ITT) paid_per_signup"))
    print()
    print(f"Достигнутая мощность (PRIMARY): {power:.3f}")
    print(
        f"MDE @80% (PRIMARY, abs):        {mde:.4f} "
        f"({mde / primary['rate_control'] * 100:.2f}% относительных)"
    )
    print(
        f"95% CI относительного lift:     "
        f"[{rel_ci[0] * 100:+.2f}%, {rel_ci[1] * 100:+.2f}%]"
    )
    print("\n--- Guardrails ---")
    for g in ("control", "treatment"):
        gg = guard[g]
        print(
            f"  {g:9s} refund_rate={gg['refund_rate']:.4%} "
            f"({gg['refunds']}/{gg['charges']})  "
            f"cancel_rate(payers)={gg['cancel_rate_payers']:.4%} "
            f"({gg['cancelled']}/{gg['payers']})"
        )
    print(f"\nVERDICT: {verdict}")


if __name__ == "__main__":
    main()
