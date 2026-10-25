"""Power-law retention + cohort LTV math — Python reference port.

Faithful port of the retention-calculator math core
(https://github.com/cosmiksoul/retention-calculator):

  src/lib/powerLaw.js      -> fit_power_law / predict / retention_curve / retention_band / extrapolation_level
  src/lib/calc.js          -> funnel_cascade / cohort_ltv / cohort_ltv_band / payback / period helpers
  src/lib/industryAdjusted -> geometric_mean / adjust_fit_to_benchmark

Period is just a t-axis label ('day' | 'week' | 'month' | 'year'); the math
is identical regardless of which one is selected. Funnel is an optional
n-step conversion cascade between cohort and paying base.

Retention model: R(t) = a * t^(-b), fit by OLS in log-space
(ln r = ln a - b * ln t) -> plain linear regression on (ln t, ln r).

This module is pure (no I/O, no plotting) and unit-testable. Use it as the
computational backbone for retention/LTV analyses; bring your own pandas
for data wrangling and matplotlib/plotly for charts.

Requires: Python 3.10+. No third-party deps (math only).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal, Sequence

Period = Literal["day", "week", "month", "year"]

PERIOD_ABBR = {"day": "D", "week": "W", "month": "M", "year": "Y"}
PERIOD_UNIT = {"day": "day", "week": "week", "month": "month", "year": "year"}
PERIOD_TICKS = {
    "day": [1, 7, 14, 30, 60, 90, 180, 365],
    "week": [1, 4, 8, 13, 26, 39, 52],
    "month": [1, 3, 6, 9, 12, 18, 24, 36],
    "year": [1, 2, 3, 5, 7, 10],
}


# --------------------------------------------------------------------------- #
# Period helpers
# --------------------------------------------------------------------------- #
def period_abbr(period: Period) -> str:
    """Single-letter prefix: D / W / M / Y."""
    return PERIOD_ABBR.get(period, "D")


def period_unit(period: Period) -> str:
    """Lowercase singular unit name: day / week / month / year."""
    return PERIOD_UNIT.get(period, "day")


def period_label(t: int, period: Period) -> str:
    """Composite label like 'D7', 'W4', 'M12', 'Y3'."""
    return f"{period_abbr(period)}{t}"


def period_ticks(period: Period, horizon: int) -> list[int]:
    """Reasonable X-axis tick positions for the period, capped to horizon."""
    return [t for t in PERIOD_TICKS.get(period, PERIOD_TICKS["day"]) if t <= horizon]


# --------------------------------------------------------------------------- #
# Power-law fit
# --------------------------------------------------------------------------- #
@dataclass
class PowerLawFit:
    """R(t) = a * t^(-b) fit result.

    a         ln-intercept exponentiated (R(1) estimate when t=1).
    b         decay exponent (positive => retention decays).
    r_squared coefficient of determination, computed in log space.
    se        standard error of the slope (drives the confidence band).
    n         number of input points.
    """

    a: float
    b: float
    r_squared: float
    se: float
    n: int


def fit_power_law(points: Sequence[dict]) -> PowerLawFit:
    """Fit R(t) = a * t^(-b) by OLS in log space.

    points: sequence of dicts with numeric 't' (>0) and 'r' (fraction in (0, 1]).
    Returns PowerLawFit. Raises ValueError on bad input.
    """
    if not isinstance(points, (list, tuple)) or len(points) < 2:
        raise ValueError("fit_power_law: need at least 2 points")

    xs: list[float] = []
    ys: list[float] = []
    for p in points:
        t = p["t"]
        r = p["r"]
        if not (t > 0):
            raise ValueError(f"fit_power_law: t must be > 0, got {t}")
        if not (0 < r <= 1):
            raise ValueError(f"fit_power_law: r must be in (0, 1], got {r}")
        xs.append(math.log(t))
        ys.append(math.log(r))

    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n

    sxy = sxx = 0.0
    for x, y in zip(xs, ys):
        dx = x - mean_x
        sxy += dx * (y - mean_y)
        sxx += dx * dx
    if sxx == 0:
        raise ValueError("fit_power_law: zero variance in t (all points share the same period)")

    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    b = -slope
    a = math.exp(intercept)

    ss_res = ss_tot = 0.0
    for x, y in zip(xs, ys):
        yhat = intercept + slope * x
        ss_res += (y - yhat) ** 2
        ss_tot += (y - mean_y) ** 2
    r_squared = 1.0 if ss_tot == 0 else 1.0 - ss_res / ss_tot

    se = math.sqrt(ss_res / (n - 2) / sxx) if n > 2 else 0.0
    return PowerLawFit(a=a, b=b, r_squared=r_squared, se=se, n=n)


def predict(t: float, fit: PowerLawFit) -> float:
    """Predicted retention at period t (fraction, typically in (0, 1])."""
    if not (t > 0):
        raise ValueError("predict: t must be > 0")
    return fit.a * (t ** -fit.b)


def retention_curve(fit: PowerLawFit, horizon: int) -> list[dict]:
    """Build [{t, r}] series for t = 1..horizon."""
    if not (horizon >= 1):
        raise ValueError("retention_curve: horizon must be >= 1")
    return [{"t": t, "r": predict(t, fit)} for t in range(1, horizon + 1)]


def retention_band(
    fit: PowerLawFit, horizon: int, k_sigma: float = 1.0
) -> list[dict]:
    """+-k*sigma confidence band around the power-law prediction.

    In log-space the regression line has standard error `se` on the slope, so
    the k-sigma envelope at period t is a * t^(-(b ∓ k*se)). Capped to [0, 1].
    Width grows monotonically with t (in log-space), by construction.

    k_sigma typically 1 (~68% Gaussian) or 2 (~95%).

    NOTE: slope-only band. Does NOT account for the intercept's SE; for the
    calculator's audience the slope error dominates the visible width and an
    asymmetric envelope would be more confusing than informative.
    """
    if not (horizon >= 1):
        raise ValueError("retention_band: horizon must be >= 1")
    if not (k_sigma > 0):
        raise ValueError("retention_band: k_sigma must be > 0")
    half = fit.se * k_sigma
    out = []
    for t in range(1, horizon + 1):
        upper = min(1.0, fit.a * (t ** -(fit.b - half)))
        lower = max(0.0, fit.a * (t ** -(fit.b + half)))
        out.append({"t": t, "lower": lower, "upper": upper})
    return out


def extrapolation_level(last_t: int, horizon: int) -> str:
    """Caution level for forecasting `horizon` past the last real point.

    ratio > 10 => 'severe' (red)
    ratio >  3 => 'caution' (amber)
    otherwise  => 'none'
    """
    if not (last_t > 0) or not (horizon > 0):
        return "none"
    ratio = horizon / last_t
    if ratio > 10:
        return "severe"
    if ratio > 3:
        return "caution"
    return "none"


# --------------------------------------------------------------------------- #
# Funnel cascade
# --------------------------------------------------------------------------- #
@dataclass
class FunnelCascadeResult:
    steps: list[dict]
    acquired_at_zero: float
    one_time_revenue: float


def funnel_cascade(
    *,
    cohort_size: float,
    funnel: Sequence[dict] | None = None,
    retention: Sequence[dict] | None = None,
    period: Period = "day",
) -> FunnelCascadeResult:
    """Generic n-step funnel cascade.

    funnel: array of {label, conversion_pct, one_time_fee_usd?}; each entry's
      count is previous_count * conversion_pct / 100. A step may carry an
      optional one_time_fee_usd (per-user fee paid by everyone reaching that
      step, e.g. paid-trial price).
    retention: array of {t, r} fractions (0..1) — active count at period t is
      acquired_at_zero * r.

    Steps emitted:
      - step 0: the cohort itself (label='Cohort (acquired users)', dropoff_pct=None)
      - one step per funnel entry (label, count, dropoff_pct=1-conv/100,
        one_time_fee_usd / one_time_revenue when fee > 0)
      - one step per retention checkpoint (label='Active at {period}{t}',
        count = acquired_at_zero * r, dropoff vs previous step)

    acquired_at_zero = count after the last funnel entry = people who actually
      start paying/engaging. When funnel=[] it equals cohort_size (DAU semantics).
    """
    if not (cohort_size >= 0):
        raise ValueError("funnel_cascade: cohort_size must be >= 0")

    funnel = list(funnel or [])
    retention = list(retention or [])

    steps: list[dict] = [
        {"label": "Cohort (acquired users)", "count": cohort_size, "dropoff_pct": None}
    ]
    prev_count = cohort_size
    one_time_revenue = 0.0

    for entry in funnel:
        conv = entry["conversion_pct"] / 100.0
        count = prev_count * conv
        fee = entry.get("one_time_fee_usd")
        fee = fee if (isinstance(fee, (int, float)) and math.isfinite(fee) and fee > 0) else 0
        step_fee_revenue = count * fee
        one_time_revenue += step_fee_revenue
        step = {"label": entry["label"], "count": count, "dropoff_pct": 1 - conv}
        if fee > 0:
            step["one_time_fee_usd"] = fee
            step["one_time_revenue"] = step_fee_revenue
        steps.append(step)
        prev_count = count

    acquired_at_zero = prev_count

    for point in retention:
        count = acquired_at_zero * point["r"]
        steps.append(
            {
                "label": f"Active at {period_label(point['t'], period)}",
                "count": count,
                "dropoff_pct": (1 - count / prev_count) if prev_count > 0 else 0,
            }
        )
        prev_count = count

    return FunnelCascadeResult(steps=steps, acquired_at_zero=acquired_at_zero,
                               one_time_revenue=one_time_revenue)


# --------------------------------------------------------------------------- #
# Cohort LTV
# --------------------------------------------------------------------------- #
@dataclass
class CohortLtvRow:
    t: int
    retention: float
    active: float
    revenue: float
    cum_revenue: float
    cum_ltv_per_cohort: float
    cum_ltv_per_acquired: float


def cohort_ltv(
    *,
    fit: PowerLawFit,
    acquired_at_zero: float,
    arpu_per_period: float,
    cohort_size: float,
    horizon: int,
    one_time_revenue: float = 0.0,
    refund_rate: float = 0.0,
) -> list[CohortLtvRow]:
    """Per-period revenue / LTV series for the cohort.

    Retention is clamped to R(1) — physically retention is non-increasing, and
    a poorly-fit power law (b <= 0) might briefly produce R(t) > R(1) in the
    first few periods otherwise. Lower bound is 0.

    one_time_revenue lumps non-recurring funnel revenue into period 1
    (typically the sum of paid-trial / activation fees for users who reach
    those funnel steps). Modeled as paid right after acquisition, in the same
    window as R(1). Compounds into cum_revenue and therefore both LTV reads
    and payback.

    refund_rate: fraction in [0, 1] applied uniformly to gross revenue — both
    recurring and one-time. Net = gross * (1 - refund_rate). Retention curve
    is unchanged: refunds claw back money but the user was still "active".

    Output fields:
      active              = acquired_at_zero * R(t)
      revenue             = net per-period revenue after refunds
      cum_revenue         = sum(revenue), total cohort, net of refunds
      cum_ltv_per_cohort  = cum_revenue / cohort_size   (per cohort entrant —
                            matches DAU/ARPDAU semantics when funnel=[])
      cum_ltv_per_acquired = cum_revenue / acquired_at_zero (per paying user;
                            equals cum_ltv_per_cohort when funnel=[])
    """
    if not (horizon >= 1):
        raise ValueError("cohort_ltv: horizon must be >= 1")
    if not math.isfinite(arpu_per_period):
        raise ValueError("cohort_ltv: arpu_per_period must be finite")
    if not math.isfinite(acquired_at_zero):
        raise ValueError("cohort_ltv: acquired_at_zero must be finite")
    if not (cohort_size > 0):
        raise ValueError("cohort_ltv: cohort_size must be > 0")
    if not (math.isfinite(one_time_revenue) and one_time_revenue >= 0):
        raise ValueError("cohort_ltv: one_time_revenue must be a non-negative finite number")
    if not (math.isfinite(refund_rate) and 0 <= refund_rate <= 1):
        raise ValueError("cohort_ltv: refund_rate must be a fraction in [0, 1]")

    net_multiplier = 1 - refund_rate
    r1 = predict(1, fit)
    out: list[CohortLtvRow] = []
    cum_rev = 0.0
    for t in range(1, horizon + 1):
        retention = max(0.0, min(predict(t, fit), r1))
        active = acquired_at_zero * retention
        recurring = active * arpu_per_period
        gross = recurring + (one_time_revenue if t == 1 else 0.0)
        revenue = gross * net_multiplier
        cum_rev += revenue
        out.append(
            CohortLtvRow(
                t=t,
                retention=retention,
                active=active,
                revenue=revenue,
                cum_revenue=cum_rev,
                cum_ltv_per_cohort=cum_rev / cohort_size,
                cum_ltv_per_acquired=(cum_rev / acquired_at_zero) if acquired_at_zero > 0 else 0.0,
            )
        )
    return out


def cohort_ltv_band(
    fit: PowerLawFit,
    per_period_rate: float,
    horizon: int,
    k_sigma: float = 1.0,
    one_time_offset: float = 0.0,
    refund_rate: float = 0.0,
) -> list[dict]:
    """+-k*sigma confidence band on cumulative cohort revenue.

    Integrates `per_period_rate * R+- (t)` cumulatively, where R+- (t) is the
    retention band edge (capped to [0, 1]). The caller picks what
    per_period_rate represents:
      - arpu_per_period * acquired_at_zero              => total-cohort revenue band
      - arpu_per_period * acquired_at_zero / cohort_size => per-cohort-entrant band

    one_time_offset: deterministic lump-sum added at t=1 on both bounds — same
    units as per_period_rate * t. Used for paid-trial / activation fees, which
    carry no power-law uncertainty.

    refund_rate scales both bounds and the offset uniformly — same multiplicative
    effect refunds have on the mean series, so band stays consistent with
    cohort_ltv output. Width grows monotonically with t in log-space.
    """
    if not (horizon >= 1):
        raise ValueError("cohort_ltv_band: horizon must be >= 1")
    if not math.isfinite(per_period_rate):
        raise ValueError("cohort_ltv_band: per_period_rate must be finite")
    if not (k_sigma > 0):
        raise ValueError("cohort_ltv_band: k_sigma must be > 0")
    if not (math.isfinite(one_time_offset) and one_time_offset >= 0):
        raise ValueError("cohort_ltv_band: one_time_offset must be a non-negative finite number")
    if not (math.isfinite(refund_rate) and 0 <= refund_rate <= 1):
        raise ValueError("cohort_ltv_band: refund_rate must be a fraction in [0, 1]")

    net_multiplier = 1 - refund_rate
    half = fit.se * k_sigma
    cum_low = 0.0
    cum_up = 0.0
    out = []
    for t in range(1, horizon + 1):
        up_r = min(1.0, fit.a * (t ** -(fit.b - half)))
        lo_r = max(0.0, fit.a * (t ** -(fit.b + half)))
        cum_up += per_period_rate * up_r * net_multiplier
        cum_low += per_period_rate * lo_r * net_multiplier
        if t == 1:
            net_offset = one_time_offset * net_multiplier
            cum_up += net_offset
            cum_low += net_offset
        out.append({"t": t, "lower": cum_low, "upper": cum_up})
    return out


def payback(series: list[CohortLtvRow], cac: float | None) -> int | None:
    """First period T (1-indexed) where cum_ltv_per_cohort >= CAC.

    Conventions:
      cac null/undefined/NaN/Infinity -> None  (caller decides what to render)
      cac <= 0                        -> 1    (any revenue covers zero cost)
      cac never reached               -> None
    """
    if cac is None or not math.isfinite(cac):
        return None
    if cac <= 0:
        return 1
    for row in series:
        if row.cum_ltv_per_cohort >= cac:
            return row.t
    return None


# --------------------------------------------------------------------------- #
# Industry-adjusted fit (benchmark shape rescaling)
# --------------------------------------------------------------------------- #
def geometric_mean(values: Sequence[float]) -> float:
    """Geometric mean of strictly positive numbers.

    Non-positive values are filtered out (their log is undefined). Returns 1
    if no usable values — a neutral identity for multiplicative scaling.
    """
    positives = [v for v in values if v > 0]
    if not positives:
        return 1.0
    return math.exp(sum(math.log(v) for v in positives) / len(positives))


def adjust_fit_to_benchmark(
    user_points: Sequence[dict], benchmark_fit: PowerLawFit | None
) -> PowerLawFit | None:
    """Rescale a benchmark curve by the user's average over/under-performance.

    When the user has only a couple of input points but wants a long-horizon
    forecast, fitting a power law to those few points is fragile — the slope
    is poorly constrained. Industry-adjusted mode borrows the *shape* of the
    benchmark curve and rescales it by how much better/worse the user's
    product is on average:

      ratio(t)        = R_user(t) / R_benchmark(t)        per user point
      k               = geometric mean of ratios         (multiplicative scale)
      R_adjusted(t)   = k * R_benchmark(t)

    Geometric mean is chosen because retention ratios are multiplicative —
    arithmetic mean would over-weight the tails (a 2x spike at one point pulls
    the average up much more than a 0.5x dip pulls it down, even though they
    represent symmetric deviations).

    user_points: [{t, percent}] (percent in 0..100, like the UI input).
    se is set to 0 (see retention_band note for why a fake CI here would mislead).

    Returns a synthetic PowerLawFit (shape = benchmark, level = scaled), or
    None if benchmark_fit is None or no usable ratios could be computed.
    """
    if benchmark_fit is None:
        return None
    ratios: list[float] = []
    for p in user_points:
        t = p.get("t")
        percent = p.get("percent")
        if not (t and t > 0):
            continue
        user_r = (percent / 100.0) if percent is not None else None
        if user_r is None or not (0 < user_r <= 1):
            continue
        bench_r = benchmark_fit.a * (t ** -benchmark_fit.b)
        if not (bench_r > 0):
            continue
        ratios.append(user_r / bench_r)
    if not ratios:
        return None
    avg_ratio = geometric_mean(ratios)
    return PowerLawFit(
        a=benchmark_fit.a * avg_ratio,
        b=benchmark_fit.b,
        r_squared=0.0,
        se=0.0,
        n=len(ratios),
    )


# --------------------------------------------------------------------------- #
# End-to-end convenience runner
# --------------------------------------------------------------------------- #
@dataclass
class LtvReport:
    fit: PowerLawFit
    series: list[CohortLtvRow]
    ltv_per_acquired: float          # cum_ltv_per_cohort at horizon (pair to CAC)
    ltv_per_paid: float               # cum_ltv_per_acquired at horizon (clean UE)
    ltv_cac: float | None
    payback_period: int | None
    extrapolation: str
    band: list[dict] = field(default_factory=list)


def run_ltv(
    *,
    retention_points: Sequence[dict],   # [{t, r}] fractions, OR [{t, percent}]
    cohort_size: float,
    arpu_per_period: float,
    horizon: int,
    period: Period = "day",
    funnel: Sequence[dict] | None = None,
    cac: float | None = None,
    refund_rate: float = 0.0,
    k_sigma: float = 1.0,
    benchmark_fit: PowerLawFit | None = None,
    use_industry_adjusted: bool = False,
) -> LtvReport:
    """End-to-end: fit -> (optional industry-adjust) -> cascade -> LTV -> payback.

    retention_points accept BOTH forms:
      - [{t, r}]       where r is a fraction in (0, 1]   (canonical)
      - [{t, percent}] where percent is in [0, 100]      (UI-style; auto-converted)

    Returns a LtvReport with the fit, the per-period series, both LTV reads at
    horizon, LTV/CAC, payback period, extrapolation level, and the +-k*sigma
    cumulative revenue band.
    """
    # Normalize percent -> fraction
    norm: list[dict] = []
    for p in retention_points:
        if "r" in p:
            norm.append({"t": p["t"], "r": p["r"]})
        else:
            norm.append({"t": p["t"], "r": p["percent"] / 100.0})

    fit = fit_power_law(norm)

    if use_industry_adjusted and benchmark_fit is not None:
        adj = adjust_fit_to_benchmark(
            [{"t": p["t"], "percent": p["r"] * 100.0} for p in norm], benchmark_fit
        )
        if adj is not None:
            fit = adj

    cascade = funnel_cascade(
        cohort_size=cohort_size, funnel=funnel, retention=retention_curve(fit, horizon), period=period
    )
    series = cohort_ltv(
        fit=fit,
        acquired_at_zero=cascade.acquired_at_zero,
        arpu_per_period=arpu_per_period,
        cohort_size=cohort_size,
        horizon=horizon,
        one_time_revenue=cascade.one_time_revenue,
        refund_rate=refund_rate,
    )

    last_t = max((p["t"] for p in norm), default=0)
    extrap = extrapolation_level(last_t, horizon)

    ltv_cac = (series[-1].cum_ltv_per_cohort / cac) if (cac and cac > 0) else None
    pb = payback(series, cac)

    band = cohort_ltv_band(
        fit,
        per_period_rate=arpu_per_period * cascade.acquired_at_zero / cohort_size,
        horizon=horizon,
        k_sigma=k_sigma,
        one_time_offset=cascade.one_time_revenue / cohort_size,
        refund_rate=refund_rate,
    )

    return LtvReport(
        fit=fit,
        series=series,
        ltv_per_acquired=series[-1].cum_ltv_per_cohort,
        ltv_per_paid=series[-1].cum_ltv_per_acquired,
        ltv_cac=ltv_cac,
        payback_period=pb,
        extrapolation=extrap,
        band=band,
    )