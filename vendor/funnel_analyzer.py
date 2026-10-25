"""Funnel Analyzer.

Takes a list of funnel steps + user counts at each step, optionally per segment,
and produces:
- per-step table: step_conversion, overall_conversion, dropoff_n, dropoff_pct
- biggest drop-off by impact score (default: absolute dropoff)
- segment ranking by overall conversion
- markdown or JSON output

Usage:
    uv run python funnel_analyzer.py --demo
    uv run python funnel_analyzer.py --steps "Open Editor,Execute,Save,Share" \
        --counts "12000,7200,5500,1200"
    uv run python funnel_analyzer.py --csv funnel.csv --steps-col step \
        --counts-col users --segment-col segment --output report.md
    uv run python funnel_analyzer.py --json funnel_data.json --output report.md
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any


def analyze_funnel(
    steps: list[str], counts: list[int], value_per_user: float = 1.0
) -> list[dict[str, Any]]:
    """Compute per-step funnel metrics. Pure function, no I/O."""
    if len(steps) != len(counts):
        raise ValueError(
            f"steps ({len(steps)}) and counts ({len(counts)}) must have equal length"
        )
    if not steps:
        raise ValueError("Funnel must have at least one step")
    if counts[0] <= 0:
        raise ValueError("Step 1 count must be > 0 (defines the funnel denominator)")

    top = counts[0]
    results: list[dict[str, Any]] = []
    for i, (step, count) in enumerate(zip(steps, counts, strict=True)):
        prev = counts[i - 1] if i > 0 else count
        step_conv = count / prev if prev > 0 else 0
        overall = count / top if top > 0 else 0
        dropoff = prev - count if i > 0 else 0
        results.append(
            {
                "step_index": i + 1,
                "step": step,
                "users": count,
                "step_conversion": round(step_conv, 6),
                "step_conversion_pct": round(step_conv * 100, 2),
                "overall_conversion": round(overall, 6),
                "overall_conversion_pct": round(overall * 100, 2),
                "dropoff_n": dropoff,
                "dropoff_pct": round((1 - step_conv) * 100, 2) if i > 0 else 0,
                "impact_score": round(dropoff * value_per_user, 2),
            }
        )
    return results


def biggest_dropoff(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the step with the largest impact score."""
    if len(rows) < 2:
        return None
    return max(rows[1:], key=lambda r: r["impact_score"])


def analyze_segments(
    segments: dict[str, tuple[list[str], list[int]]], value_per_user: float = 1.0
) -> list[dict[str, Any]]:
    """Run funnel per segment, rank by overall conversion.

    segments: { "Segment A": (["s1", "s2"], [100, 70]), ... }
    """
    out: list[dict[str, Any]] = []
    for name, (steps, counts) in segments.items():
        try:
            funnel = analyze_funnel(steps, counts, value_per_user=value_per_user)
        except ValueError as e:
            out.append({"segment": name, "error": str(e)})
            continue
        out.append(
            {
                "segment": name,
                "step1_users": funnel[0]["users"],
                "final_users": funnel[-1]["users"],
                "overall_conversion_pct": funnel[-1]["overall_conversion_pct"],
                "biggest_dropoff_step": (
                    biggest_dropoff(funnel)["step"] if len(funnel) > 1 else None
                ),
                "rows": funnel,
            }
        )
    out.sort(key=lambda s: s.get("overall_conversion_pct", -1), reverse=True)
    return out


def format_funnel_table(rows: list[dict[str, Any]], title: str = "Funnel Analysis") -> str:
    """Markdown table + summary."""
    lines = [
        f"## {title}",
        "",
        "| # | Step | Users | Step conv | Overall conv | Drop-off |",
        "|---|------|------:|----------:|-------------:|---------:|",
    ]
    for r in rows:
        if r["step_index"] == 1:
            dropoff_str = "—"
            step_conv_str = "—"
        else:
            dropoff_str = f"−{r['dropoff_n']:,} (−{r['dropoff_pct']:.1f}%)"
            step_conv_str = f"{r['step_conversion_pct']:.1f}%"
        lines.append(
            f"| {r['step_index']} | {r['step']} | {r['users']:,} | "
            f"{step_conv_str} | {r['overall_conversion_pct']:.1f}% | {dropoff_str} |"
        )
    top = biggest_dropoff(rows)
    lines += ["", f"**Overall conversion:** {rows[-1]['overall_conversion_pct']:.1f}%"]
    if top:
        lines += [
            "",
            f"**Highest impact drop-off:** {top['step']} (step {top['step_index']})",
            f"- Drop-off: {top['dropoff_n']:,} users ({top['dropoff_pct']:.1f}% of prior step)",
            f"- Impact score: {top['impact_score']:,.2f}",
        ]
    return "\n".join(lines)


def format_segments_table(
    segments: list[dict[str, Any]], title: str = "Segment Breakdown"
) -> str:
    lines = [
        f"## {title}",
        "",
        "| Segment | Step1 users | Final users | Overall conv | Biggest drop-off step |",
        "|---------|------------:|------------:|-------------:|----------------------|",
    ]
    for s in segments:
        if "error" in s:
            lines.append(
                f"| {s['segment']} | — | — | — | error: {s['error']} |"
            )
            continue
        lines.append(
            f"| {s['segment']} | {s['step1_users']:,} | {s['final_users']:,} | "
            f"{s['overall_conversion_pct']:.1f}% | {s['biggest_dropoff_step'] or '—'} |"
        )
    return "\n".join(lines)


def _check_monotonic(counts: list[int]) -> None:
    """Warn if counts increase between steps (likely data issue)."""
    for i in range(1, len(counts)):
        if counts[i] > counts[i - 1]:
            print(
                f"[warn] Step {i + 1} count ({counts[i]:,}) > step {i} count ({counts[i - 1]:,}). "
                "Counts should be non-increasing for a valid funnel.",
                file=sys.stderr,
            )


def load_from_csv(
    path: str,
    steps_col: str,
    counts_col: str,
    segment_col: str | None = None,
) -> tuple[list[str], list[int]] | dict[str, tuple[list[str], list[int]]]:
    """Returns (steps, counts) if no segment_col; else dict[segment] -> (steps, counts)."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"CSV file is empty: {path}")
    if steps_col not in rows[0]:
        raise ValueError(f"Missing steps column '{steps_col}' in {path}")
    if counts_col not in rows[0]:
        raise ValueError(f"Missing counts column '{counts_col}' in {path}")
    if segment_col and segment_col not in rows[0]:
        raise ValueError(f"Missing segment column '{segment_col}' in {path}")

    if segment_col:
        by_seg: dict[str, tuple[list[str], list[int]]] = {}
        for row in rows:
            seg = row[segment_col].strip() if row[segment_col] else "<unknown>"
            steps, counts = by_seg.setdefault(seg, ([], []))
            steps.append(row[steps_col].strip())
            try:
                counts.append(int(row[counts_col]))
            except ValueError as exc:
                raise ValueError(
                    f"Non-integer count in segment '{seg}': {row[counts_col]!r}"
                ) from exc
        return by_seg

    steps = [row[steps_col].strip() for row in rows]
    try:
        counts = [int(row[counts_col]) for row in rows]
    except ValueError as exc:
        raise ValueError(f"Non-integer count in column '{counts_col}'") from exc
    return steps, counts


def load_from_json(path: str) -> dict[str, Any]:
    """JSON shape: {steps, counts, segments?, value_per_user?}"""
    with open(path, encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    required = {"steps", "counts"}
    missing = required - set(data.keys())
    if missing:
        raise ValueError(f"Missing required JSON keys: {missing}")
    if not isinstance(data["steps"], list) or not isinstance(data["counts"], list):
        raise ValueError("'steps' and 'counts' must be lists")
    if len(data["steps"]) != len(data["counts"]):
        raise ValueError("'steps' and 'counts' must have equal length")
    return data


def _render_report(
    rows: list[dict[str, Any]],
    segments: list[dict[str, Any]] | None,
    title: str,
    output_format: str,
) -> str:
    if output_format == "json":
        out = {
            "funnel": rows,
            "segments": segments,
            "biggest_dropoff": biggest_dropoff(rows),
        }
        return json.dumps(out, indent=2, ensure_ascii=False)

    parts = [format_funnel_table(rows, title)]
    if segments:
        parts.append(format_segments_table(segments))
    return "\n\n".join(parts)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Analyze a conversion funnel with optional segments."
    )
    p.add_argument("--demo", action="store_true", help="Run a baked-in demo")
    p.add_argument("--steps", help="Comma-separated step names")
    p.add_argument(
        "--counts", help="Comma-separated user counts, same length as --steps"
    )
    p.add_argument("--csv", dest="csv_path", help="CSV with funnel data (one row per step)")
    p.add_argument("--steps-col", default="step")
    p.add_argument("--counts-col", default="users")
    p.add_argument(
        "--segment-col", default=None, help="If CSV has this column, run per-segment funnel"
    )
    p.add_argument(
        "--json", dest="json_path", help="JSON: {steps, counts, segments?, value_per_user?}"
    )
    p.add_argument(
        "--value-per-user",
        type=float,
        default=1.0,
        help="Weight per user (for impact score)",
    )
    p.add_argument("--title", default="Funnel Analysis")
    p.add_argument("--output", help="Write markdown report to file")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    args = p.parse_args()

    if args.demo:
        steps = [
            "Registration",
            "Onboarding Step 1",
            "Onboarding Step 2",
            "Onboarding Complete",
            "Subscription Started",
        ]
        counts = [3_000, 3_000, 2_120, 1_785, 1_200]
        segments = {
            "Channel: Organic": (steps, [900, 900, 680, 590, 420]),
            "Channel: Paid": (steps, [750, 750, 540, 460, 340]),
            "Channel: Referral": (steps, [620, 620, 420, 340, 210]),
            "Channel: Unknown": (steps, [730, 730, 480, 395, 230]),
        }
        rows = analyze_funnel(steps, counts, args.value_per_user)
        seg_rows = analyze_segments(segments, args.value_per_user)
        print(_render_report(rows, seg_rows, "Demo: onboarding → subscription funnel", args.format))
        return

    rows: list[dict[str, Any]] | None = None
    segments: list[dict[str, Any]] | None = None
    if args.json_path:
        data = load_from_json(args.json_path)
        rows = analyze_funnel(
            data["steps"], data["counts"], data.get("value_per_user", args.value_per_user)
        )
        if "segments" in data:
            seg_input = {
                name: (data["steps"], counts)
                for name, counts in data["segments"].items()
            }
            segments = analyze_segments(
                seg_input, data.get("value_per_user", args.value_per_user)
            )
    elif args.csv_path:
        loaded = load_from_csv(
            args.csv_path, args.steps_col, args.counts_col, args.segment_col
        )
        if args.segment_col:
            seg_input = {
                name: (steps, counts) for name, (steps, counts) in loaded.items()
            }
            # Aggregate counts by step name across segments to preserve alignment.
            all_steps = next(iter(seg_input.values()))[0]
            step_totals: dict[str, int] = dict.fromkeys(all_steps, 0)
            for steps, counts in seg_input.values():
                for step, count in zip(steps, counts, strict=True):
                    step_totals[step] += count
            agg_steps = list(step_totals.keys())
            agg_counts = list(step_totals.values())
            rows = analyze_funnel(agg_steps, agg_counts, args.value_per_user)
            segments = analyze_segments(seg_input, args.value_per_user)
        else:
            steps, counts = loaded  # type: ignore[misc]
            rows = analyze_funnel(steps, counts, args.value_per_user)
    elif args.steps and args.counts:
        steps = [s.strip() for s in args.steps.split(",")]
        counts = [int(c.strip()) for c in args.counts.split(",")]
        rows = analyze_funnel(steps, counts, args.value_per_user)
    else:
        p.error("Provide --steps + --counts, --csv, --json, or --demo")

    if rows is None:
        p.error("No funnel data could be loaded")

    _check_monotonic([r["users"] for r in rows])
    text = _render_report(rows, segments, args.title, args.format)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"Report written to {args.output}")
    else:
        print(text)


if __name__ == "__main__":
    main()
