"""RFM customer segmentation — reusable core (CLI + importable).

Canonical pipeline: load -> clean -> RFM table -> quantile scoring -> segment -> export.

Scoring convention (industry standard, higher = better):
    R_Score: ``bins`` = most recent, 1 = longest ago
    F_Score: ``bins`` = most frequent, 1 = rarest
    M_Score: ``bins`` = highest spend, 1 = lowest
    RFMClass: ``bins``-digit string, e.g. "444" = best, "111" = worst
    RFMScore:  sum ``bins``..``3*bins``, higher = better

The default ``bins=4`` (quartiles, score 3..12) reproduces the reference
project ``rfm-analysis-of-bank-clients``; ``bins=5`` gives the classic
quintile RFM (score 3..15). Both are supported; only the bin count changes.

No hardcoded absolute paths, no network access. Depends only on the stack the
reference project already uses: pandas, numpy, openpyxl (optional Excel export).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Segments returned by assign_segment, in canonical value order.
SEGMENTS = (
    "Champions",
    "Loyal",
    "Potential Loyalists",
    "Need Attention",
    "At Risk",
    "Hibernating",
)


def load_transactions(csv_path: Path | str, *, date_col: str = "transaction_date") -> pd.DataFrame:
    """Load source transactions and parse the date column."""
    df = pd.read_csv(csv_path)
    df[date_col] = pd.to_datetime(df[date_col])
    return df


def drop_anomalies(df: pd.DataFrame, *, flag_col: str = "is_anomaly") -> pd.DataFrame:
    """Drop flagged anomaly transactions if the flag column exists."""
    if flag_col in df.columns:
        return df[df[flag_col] == 0].copy()
    return df.copy()


def build_rfm_table(
    df: pd.DataFrame,
    snapshot_date: pd.Timestamp | str | None = None,
    *,
    customer_col: str = "customer_id",
    date_col: str = "transaction_date",
    amount_col: str = "amount",
) -> pd.DataFrame:
    """Aggregate per-customer Recency / Frequency / Monetary value.

    Recency is whole days against a snapshot date; without an explicit one the
    snapshot is the day after the last transaction, so the most recent customer
    scores recency = 1, never 0.
    """
    if df.empty:
        raise ValueError("no transactions to score (empty input)")
    if snapshot_date is None:
        snapshot_date = df[date_col].max().normalize() + pd.Timedelta(days=1)
    else:
        snapshot_date = pd.Timestamp(snapshot_date).normalize()
    rfm = (
        df.groupby(customer_col)
        .agg(
            recency=(date_col, lambda x: (snapshot_date - x.max().normalize()).days),
            frequency=(date_col, "count"),
            monetary_value=(amount_col, "sum"),
        )
        .reset_index()
    )
    return rfm


def _quantile(series: pd.Series, higher_is_better: bool, bins: int) -> pd.Series:
    """Rank-based 1..bins score, total for any input size.

    ``pd.qcut`` raises "Bin edges must be unique" when quantile edges coincide
    (heavy ties, n < bins, a single-day window), so rank first and bin the ranks:
    equal-sized groups wherever possible, a defined answer everywhere else.
    """
    n = series.shape[0]
    if n == 0:
        return pd.Series([], dtype=int, index=series.index)
    ranks = series.rank(method="first").to_numpy()
    scores = np.clip(np.ceil(ranks / n * bins), 1, bins).astype(int)
    if not higher_is_better:
        scores = bins + 1 - scores
    return pd.Series(scores, index=series.index)


def score_rfm(rfm: pd.DataFrame, *, bins: int = 4) -> pd.DataFrame:
    """Assign 1..``bins`` scores. Higher = better for all three dimensions."""
    if bins < 2:
        raise ValueError("bins must be >= 2")
    scored = rfm.copy()
    # Recency: small value (recent) = good -> label `bins`
    scored["R_Score"] = _quantile(scored["recency"], higher_is_better=False, bins=bins)
    # Frequency, Monetary: large value = good -> label `bins`
    scored["F_Score"] = _quantile(scored["frequency"], higher_is_better=True, bins=bins)
    scored["M_Score"] = _quantile(scored["monetary_value"], higher_is_better=True, bins=bins)
    scored["RFMClass"] = (
        scored["R_Score"].astype(str)
        + scored["F_Score"].astype(str)
        + scored["M_Score"].astype(str)
    )
    scored["RFMScore"] = scored["R_Score"] + scored["F_Score"] + scored["M_Score"]
    return scored


# Backwards-compatible alias (the reference project names it score_quartiles).
score_quartiles = score_rfm


def assign_segment(row: pd.Series, *, bins: int = 4) -> str:
    """Map an R/F/M score combo to a named segment.

    Combo-based (not sum-based): preserves which dimension drives the label, so
    "444" and "233" do not collapse into one bucket. Thresholds scale with
    ``bins`` so the rules hold for quartiles and quintiles alike.
    """
    r, f, m = row["R_Score"], row["F_Score"], row["M_Score"]
    top = bins
    mid = bins - 1
    low = int(np.ceil(bins / 2))
    if r >= top and f >= top and m >= top:
        return "Champions"
    if r >= mid and f >= mid and m >= mid:
        return "Loyal"
    if r >= mid and f >= low and m >= mid:
        return "Potential Loyalists"
    if r <= low and f >= mid:
        return "At Risk"
    if r <= low and f <= low:
        return "Hibernating"
    return "Need Attention"


def segment_rfm(scored: pd.DataFrame, *, bins: int = 4) -> pd.DataFrame:
    scored = scored.copy()
    scored["Segment"] = scored.apply(assign_segment, axis=1, bins=bins)
    return scored


def segment_summary(scored: pd.DataFrame) -> pd.DataFrame:
    """Per-segment counts and mean RFM metrics, ordered by mean RFM score."""
    summary = (
        scored.groupby("Segment")
        .agg(
            customers=("customer_id", "count"),
            avg_recency=("recency", "mean"),
            avg_frequency=("frequency", "mean"),
            avg_monetary=("monetary_value", "mean"),
            avg_rfm_score=("RFMScore", "mean"),
        )
        .round(2)
        .reset_index()
        .sort_values("avg_rfm_score", ascending=False)
    )
    return summary


def segment_revenue_concentration(scored: pd.DataFrame) -> pd.DataFrame:
    """Per-segment share of customers vs share of revenue.

    ``concentration_ratio`` = revenue_share / customer_share. > 1 means the
    segment over-indexes on revenue; < 1 means it under-indexes. A segment with
    a high customer share but low revenue share is a cost-cutting candidate, not
    an investment one.
    """
    seg = (
        scored.groupby("Segment")
        .agg(customers=("customer_id", "count"), revenue=("monetary_value", "sum"))
        .reset_index()
    )
    seg["share_customers_%"] = (seg["customers"] / seg["customers"].sum() * 100).round(2)
    seg["share_revenue_%"] = (seg["revenue"] / seg["revenue"].sum() * 100).round(2)
    seg["concentration_ratio"] = (seg["share_revenue_%"] / seg["share_customers_%"]).round(2)
    return seg.sort_values("share_revenue_%", ascending=False)


def lorenz_gini(rfm: pd.DataFrame) -> tuple[float, np.ndarray, np.ndarray]:
    """Lorenz curve and Gini coefficient of monetary concentration.

    Top-X% of customers generating Y% of revenue verifies that segmentation has
    business sense (vs. uniform revenue).
    Returns (gini, cumulative_customers_share, cumulative_revenue_share).
    """
    m = np.sort(rfm["monetary_value"].to_numpy())
    n = m.size
    if n == 0 or m.sum() == 0:
        return 0.0, np.array([0.0]), np.array([0.0])
    # Both axes start at the (0, 0) origin; without that anchor the trapezoid
    # starts at 1/n and the Gini is biased (0.25 for two equal values).
    cum_customers = np.concatenate([[0.0], np.arange(1, n + 1) / n])
    cum_revenue = np.concatenate([[0.0], np.cumsum(m) / m.sum()])
    # Version-agnostic trapezoidal integral (np.trapz was removed in numpy 2.0).
    area = float(np.sum(np.diff(cum_customers) * (cum_revenue[:-1] + cum_revenue[1:]) / 2))
    gini = 1 - 2 * area
    return gini, cum_customers, cum_revenue


def export_excel(scored: pd.DataFrame, summary: pd.DataFrame, out_path: Path | str) -> Path:
    """Write a multi-sheet .xlsx: Overview + Segments + Customer_RFM."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    customer_cols = [
        "customer_id",
        "R_Score",
        "F_Score",
        "M_Score",
        "RFMClass",
        "RFMScore",
        "Segment",
    ]
    overview = pd.DataFrame(
        {
            "Metric": ["Total Customers", "Segments", "Avg RFM Score"],
            "Value": [
                len(scored),
                scored["Segment"].nunique(),
                round(float(scored["RFMScore"].mean()), 2),
            ],
        }
    )
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        overview.to_excel(writer, sheet_name="Overview", index=False)
        summary.to_excel(writer, sheet_name="Segments", index=False)
        scored[customer_cols].to_excel(writer, sheet_name="Customer_RFM", index=False)
    return out_path


def run(
    input_csv: Path | str,
    out_dir: Path | str,
    *,
    bins: int = 4,
    snapshot_date: str | None = None,
    exclude_anomalies: bool = True,
    make_excel: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Full pipeline: load -> clean -> RFM -> score -> segment -> export."""
    df = load_transactions(input_csv)
    df = drop_anomalies(df) if exclude_anomalies else df
    rfm = build_rfm_table(df, snapshot_date=snapshot_date)
    scored = segment_rfm(score_rfm(rfm, bins=bins), bins=bins)
    summary = segment_summary(scored)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    scored.to_csv(out_dir / "rfm_scored.csv", index=False)
    summary.to_csv(out_dir / "segment_summary.csv", index=False)
    if make_excel:
        export_excel(scored, summary, out_dir / "rfm_report.xlsx")
    return scored, summary


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rfm.py",
        description="RFM customer segmentation: score transactions and label segments.",
    )
    p.add_argument("--input", required=True, help="Path to the transactions CSV.")
    p.add_argument("--out", required=True, help="Output directory for CSV/XLSX artifacts.")
    p.add_argument(
        "--bins",
        type=int,
        default=4,
        help="Quantile bins per dimension: 4 = quartiles (default), 5 = quintiles.",
    )
    p.add_argument(
        "--snapshot",
        default=None,
        help="Recency snapshot date (ISO). Default: last transaction day + 1.",
    )
    p.add_argument(
        "--keep-anomalies",
        action="store_true",
        help="Do not drop rows flagged is_anomaly == 1.",
    )
    p.add_argument("--excel", action="store_true", help="Also write a multi-sheet .xlsx report.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    scored, summary = run(
        args.input,
        args.out,
        bins=args.bins,
        snapshot_date=args.snapshot,
        exclude_anomalies=not args.keep_anomalies,
        make_excel=args.excel,
    )
    gini, _, _ = lorenz_gini(scored)
    conc = segment_revenue_concentration(scored)
    print("\nSegment revenue concentration (customers vs revenue):")
    print(conc.to_string(index=False))
    print(f"\nGini coefficient (monetary concentration): {gini:.3f}")
    print(f"Rows: {len(scored)} customers across {scored['Segment'].nunique()} segments")
    print(summary.to_string(index=False))
    print(f"\nCSV : {Path(args.out) / 'rfm_scored.csv'}")
    print(f"CSV : {Path(args.out) / 'segment_summary.csv'}")
    if args.excel:
        print(f"XLSX: {Path(args.out) / 'rfm_report.xlsx'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
