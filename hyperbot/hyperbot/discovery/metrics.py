"""Equity-curve statistics — the evidence the scorers reason over.

THE DEPOSIT PROBLEM
-------------------
`accountValueHistory` moves when a trader deposits or withdraws, so raw equity
change is NOT performance: a $1M deposit looks like a heroic day, a withdrawal
looks like a 40% drawdown. Hyperliquid also returns `pnlHistory` (cumulative
realised+unrealised PnL), so every metric here is computed from a SYNTHETIC
curve built by compounding per-period returns:

    return_t    = (pnl_t - pnl_(t-1)) / equity_(t-1)
    synthetic_t = synthetic_(t-1) * (1 + return_t)

That curve is deposit-neutral, which is the whole point: it measures the trader,
not their bank transfers.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..util import safe_div, to_float

MS_PER_YEAR = 365.25 * 24 * 60 * 60 * 1000
MS_PER_DAY = 24 * 60 * 60 * 1000


@dataclass
class CurveMetrics:
    """Statistics for one window (day / week / month / allTime)."""

    window: str
    n_periods: int = 0
    start_equity: float = 0.0
    end_equity: float = 0.0
    pnl: float = 0.0
    volume: float = 0.0
    avg_capital: float = 0.0

    roi: float = 0.0  # deposit-neutral, compounded
    max_drawdown: float = 0.0  # 0..1, peak-to-trough on the synthetic curve
    sharpe: float = 0.0  # annualised
    sortino: float = 0.0  # annualised, downside deviation
    calmar: float = 0.0  # roi / max_drawdown
    consistency: float = 0.0  # fraction of periods with a positive return
    r_squared: float = 0.0  # linearity of log-equity: smooth compounding -> 1.0
    profit_factor: float = 0.0  # gains / |losses|
    concentration: float = 0.0  # biggest single period's share of total gains
    clamped_periods: int = 0
    best_period: float = 0.0
    worst_period: float = 0.0
    span_days: float = 0.0

    @property
    def is_empty(self) -> bool:
        return self.n_periods < 2

    @property
    def reliable(self) -> bool:
        """False when the reconstruction hit its backstop - shape stats are then
        artefacts of coarse bucketing, not descriptions of the trader."""
        return self.n_periods >= 5 and self.clamped_periods == 0


@dataclass
class TraderMetrics:
    """Everything known about one candidate account."""

    address: str
    display_name: str = ""
    account_value: float = 0.0
    windows: dict[str, CurveMetrics] = field(default_factory=dict)
    # Headline numbers straight from the leaderboard (used for cheap pre-filtering).
    lb_roi: dict[str, float] = field(default_factory=dict)
    lb_pnl: dict[str, float] = field(default_factory=dict)
    lb_volume: dict[str, float] = field(default_factory=dict)
    days_active: float = 0.0
    open_positions: int = 0
    turnover: float = 0.0  # allTime volume / account value

    # Filled in by the scorers.
    elite_score: float = 0.0
    rising_score: float = 0.0
    rejections: list[str] = field(default_factory=list)

    def window(self, name: str) -> CurveMetrics:
        return self.windows.get(name) or CurveMetrics(window=name)

    @property
    def perp_capital(self) -> float:
        """Capital actually deployed on perps, not headline account value.

        We mirror perp positions as a fraction of the leader's perp equity, so
        that is the size that matters. An account can show $390k of account value
        while trading $5k of perp capital; sizing off the $390k would copy a book
        the trader never had.
        """
        month = self.window("month")
        week = self.window("week")
        return month.avg_capital or week.avg_capital or self.account_value

    @property
    def label(self) -> str:
        return self.display_name or f"{self.address[:6]}…{self.address[-4:]}"

    def reported_roi(self, window: str) -> float:
        """The venue's headline ROI. DISPLAY AND PREFILTERING ONLY - not ranking.

        Measured against our reconstruction it is not a return on deployed
        capital: over the month window the two disagree by a median of ~900
        percentage points, and the leaderboard routinely reports figures like
        61,000% for accounts that merely started the month with a tiny balance.
        It is a fine coarse sort key over 44k rows; it is not evidence about a
        trader, so nothing in the scorers reads it.
        """
        return self.lb_roi.get(window, 0.0)

    def curve_roi(self, window: str) -> float:
        """Deposit-neutral ROI from our own reconstruction - the ranking number."""
        return self.window(window).roi

    @property
    def track_record(self) -> float:
        """Lifetime PnL in DOLLARS.

        A sum, not a ratio, so it is immune to the denominator problem that makes
        long-horizon ROI unusable. This is how an account earns credibility for
        size and persistence.
        """
        return self.lb_pnl.get("allTime", 0.0)

    @property
    def acceleration(self) -> float:
        """Recent pace minus established pace, in ROI points. Display only."""
        return self.curve_roi("week") * (30.0 / 7.0) - self.curve_roi("month")

    @property
    def pace_ratio(self) -> float:
        """Recent pace as a MULTIPLE of established pace: week ROI annualised to
        30 days, over month ROI.

        A raw difference is meaningless across this population - an account up
        1200% on the month has an 'acceleration' of -500 points while still
        compounding beautifully. The ratio is scale-free: 1.0 means this week
        matched the month's pace, >1 is genuinely accelerating, <1 is fading.
        """
        month = self.curve_roi("month")
        if month <= 0:
            return 0.0
        return (self.curve_roi("week") * (30.0 / 7.0)) / month


# --------------------------------------------------------------------------- #
# curve maths
# --------------------------------------------------------------------------- #


def _series(raw: Sequence[Any]) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    for point in raw or []:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            out.append((int(point[0]), to_float(point[1])))
    out.sort(key=lambda item: item[0])
    return out


# Dust guard only: the denominator below is already robust to capital changes.
MIN_CAPITAL_BASE = 1.0
MIN_BASE_FRACTION = 0.05


def _capital_base(previous_equity: float, current_equity: float, pnl_delta: float) -> float:
    """Average capital deployed across a period.

    Using the period's STARTING equity as the denominator breaks whenever capital
    moves during the period, and Hyperliquid's long windows are coarse (allTime
    buckets run ~167 hours), so it breaks constantly. A real case: equity went
    $495 -> $5,000,000 by deposit inside one bucket that lost $5,219. Dividing by
    $495 reports -1053% for a week in which the trader actually lost 0.1%; the
    compounded curve then dies and never recovers, which is why ~46% of accounts
    scored an allTime ROI of exactly -100%.

    `current_equity - pnl_delta` is the end-of-period balance with this period's
    trading result backed out - i.e. capital after deposits/withdrawals but before
    performance. Averaging it with the opening balance estimates the capital that
    was actually at risk.
    """
    deposit_adjusted_end = current_equity - pnl_delta
    base = (previous_equity + deposit_adjusted_end) / 2.0
    # A negative average means the readings disagree (a withdrawal larger than the
    # closing balance, say). The capital at risk is then unknowable, so report 0
    # and let the caller drop the period rather than invent a small denominator.
    return base if base > 0 else 0.0


def period_returns(
    equity: Sequence[tuple[int, float]], pnl: Sequence[tuple[int, float]]
) -> tuple[list[float], list[float], list[int], list[float]]:
    """Deposit-neutral per-period returns, PnL deltas, timestamps and capital bases."""
    count = min(len(equity), len(pnl))
    raw: list[tuple[int, float, float]] = []  # (timestamp, pnl delta, capital base)
    for index in range(1, count):
        delta = pnl[index][1] - pnl[index - 1][1]
        base = _capital_base(equity[index - 1][1], equity[index][1], delta)
        raw.append((equity[index][0], delta, base))

    # Periods whose capital base is tiny next to the window's own norm are the
    # account's ramp-up, not its trading. Left in, they produce both -99% and
    # +40000% periods that dominate every compounded statistic.
    positive_bases = sorted(base for _, _, base in raw if base > 0)
    if not positive_bases:
        return [], [], [], []
    typical = positive_bases[len(positive_bases) // 2]
    floor = max(MIN_CAPITAL_BASE, MIN_BASE_FRACTION * typical)

    returns: list[float] = []
    deltas: list[float] = []
    stamps: list[int] = []
    bases: list[float] = []
    for timestamp, delta, base in raw:
        if base < floor:
            continue
        # Backstop: a period cannot lose more than the capital that was at risk.
        returns.append(max(-0.99, delta / base))
        deltas.append(delta)
        stamps.append(timestamp)
        bases.append(base)
    return returns, deltas, stamps, bases


def _max_drawdown(curve: Sequence[float]) -> float:
    peak = -math.inf
    worst = 0.0
    for value in curve:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def _r_squared(curve: Sequence[float]) -> float:
    """R² of the equity curve against time — how straight the accumulation is.

    A steady edge draws a near-straight line (R² -> 1). A curve that is flat and
    then jumps once fits a line badly (R² -> 0). This is the best skill-vs-luck
    discriminator available from public data alone. The fit is linear because the
    curve is additive (return on deployed capital), not compounded.
    """
    points = [(index, value) for index, value in enumerate(curve)]
    n = len(points)
    if n < 3:
        return 0.0
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in points)
    sxx = sum((x - mean_x) ** 2 for x, _ in points)
    syy = sum((y - mean_y) ** 2 for _, y in points)
    if sxx <= 0 or syy <= 0:
        return 0.0
    return max(0.0, min(1.0, (sxy * sxy) / (sxx * syy)))


def _stdev(values: Sequence[float], mean: float) -> float:
    if len(values) < 2:
        return 0.0
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def compute_curve_metrics(window: str, data: dict[str, Any]) -> CurveMetrics:
    equity = _series(data.get("accountValueHistory", []))
    pnl = _series(data.get("pnlHistory", []))
    metrics = CurveMetrics(window=window)
    if len(equity) < 2 or len(pnl) < 2:
        return metrics

    active = [point for point in equity if point[1] >= MIN_CAPITAL_BASE]
    metrics.start_equity = active[0][1] if active else equity[0][1]
    metrics.end_equity = equity[-1][1]
    metrics.pnl = pnl[-1][1] - pnl[0][1]
    # Measure from first real funding, so a wallet that sat empty for a year does
    # not claim a year of trading history.
    metrics.span_days = (
        (active[-1][0] - active[0][0]) / MS_PER_DAY if len(active) >= 2 else 0.0
    )
    volume = data.get("vlm")
    metrics.volume = to_float(volume[-1] if isinstance(volume, list) and volume else volume)

    returns, deltas, stamps, bases = period_returns(equity, pnl)
    metrics.n_periods = len(returns)
    if not returns:
        return metrics

    # RETURN ON DEPLOYED CAPITAL - additive, not compounded.
    #
    # Compounding per-period returns is wrong for accounts with capital flows,
    # and it fails loudest on the best-looking ones. A real case: a trader who
    # withdraws profits continuously kept ~$5k of perp capital, made $9,424 over
    # the month, and ended the month SMALLER than they started. Each bucket's
    # gain divided by that small base, and the product of 47 such buckets
    # reported 281,531% - an account that 2,815x'd, which plainly never happened.
    #
    # Normalising cumulative PnL by the average capital deployed states what
    # actually occurred: $9,424 earned on ~$5k of capital. It is bounded by the
    # dollars, so no sequence of periods can invent wealth.
    metrics.avg_capital = sum(bases) / len(bases)
    cumulative = 0.0
    curve = [1.0]
    for delta in deltas:
        cumulative += delta
        curve.append(1.0 + cumulative / metrics.avg_capital)
    metrics.roi = curve[-1] - 1.0
    metrics.max_drawdown = _max_drawdown(curve)
    metrics.r_squared = _r_squared(curve)  # linear fit: the curve is additive now
    metrics.consistency = sum(1 for value in returns if value > 0) / len(returns)
    metrics.clamped_periods = sum(1 for value in returns if value <= -0.98)
    metrics.best_period = max(returns)
    metrics.worst_period = min(returns)

    mean = sum(returns) / len(returns)
    deviation = _stdev(returns, mean)
    downside = [value for value in returns if value < 0]
    downside_deviation = (
        math.sqrt(sum(value**2 for value in downside) / len(downside)) if downside else 0.0
    )

    # Annualise using the actual sampling interval, which differs per window
    # (the 'week' window is ~2.5h buckets, 'month' is ~14h).
    if len(stamps) >= 2:
        intervals = [b - a for a, b in zip(stamps, stamps[1:]) if b > a]
        median_interval = sorted(intervals)[len(intervals) // 2] if intervals else 0
        periods_per_year = MS_PER_YEAR / median_interval if median_interval else 0.0
    else:
        periods_per_year = 0.0

    if deviation > 0 and periods_per_year > 0:
        metrics.sharpe = (mean / deviation) * math.sqrt(periods_per_year)
    if downside_deviation > 0 and periods_per_year > 0:
        metrics.sortino = (mean / downside_deviation) * math.sqrt(periods_per_year)
    elif not downside and mean > 0:
        metrics.sortino = 99.0  # no losing period in the window

    metrics.calmar = safe_div(metrics.roi, metrics.max_drawdown, 0.0)

    gains = [value for value in deltas if value > 0]
    losses = [value for value in deltas if value < 0]
    total_gain = sum(gains)
    total_loss = abs(sum(losses))
    metrics.profit_factor = safe_div(total_gain, total_loss, 99.0 if total_gain else 0.0)
    # One-hit-wonder detector: how much of the profit came from a single period.
    metrics.concentration = safe_div(max(gains, default=0.0), total_gain, 0.0)
    return metrics


def build_trader_metrics(
    address: str,
    portfolio: dict[str, dict[str, Any]],
    *,
    display_name: str = "",
    account_value: float = 0.0,
    leaderboard_roi: dict[str, float] | None = None,
    leaderboard_pnl: dict[str, float] | None = None,
    leaderboard_volume: dict[str, float] | None = None,
    open_positions: int = 0,
) -> TraderMetrics:
    metrics = TraderMetrics(
        address=address,
        display_name=display_name,
        account_value=account_value,
        lb_roi=dict(leaderboard_roi or {}),
        lb_pnl=dict(leaderboard_pnl or {}),
        lb_volume=dict(leaderboard_volume or {}),
        open_positions=open_positions,
    )
    # Prefer the perp-only series where present: this bot mirrors perps, so spot
    # holdings drifting in value should not count as trading performance.
    for window in ("day", "week", "month", "allTime"):
        source = portfolio.get(f"perp{window[0].upper()}{window[1:]}") or portfolio.get(window)
        if source:
            metrics.windows[window] = compute_curve_metrics(window, source)

    all_time = metrics.window("allTime")
    metrics.days_active = all_time.span_days
    metrics.turnover = safe_div(all_time.volume or metrics.lb_volume.get("allTime", 0.0),
                                account_value, 0.0)
    return metrics
