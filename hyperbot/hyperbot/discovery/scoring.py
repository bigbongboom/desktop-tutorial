"""Two rankings over the same evidence.

EliteScorer  — "top traders": proven, durable, survivable operators.
RisingScorer — "on the come up": small accounts compounding a real edge, accelerating.

Design rule: good properties are AVERAGED (weighted sum), bad properties are
MULTIPLIED (penalties). A single disqualifying trait — a 60% drawdown, a month of
profit that was one lucky hour — must not be averaged away by six good ones.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import DiscoveryConfig
from ..util import clamp, safe_div
from .metrics import TraderMetrics

# Below this many usable periods, a window's statistics are not evidence.
MIN_SCORING_PERIODS = 5


def _log10_safe(value: float) -> float:
    import math

    return math.log10(value) if value > 1 else 0.0


def saturate(value: float, full: float, floor: float = 0.0) -> float:
    """Map value onto 0..1, reaching 1.0 at `full`. Negative values score 0."""
    if full <= floor:
        return 0.0
    return clamp((value - floor) / (full - floor), 0.0, 1.0)


@dataclass
class ScoreBreakdown:
    """Kept alongside the score so any ranking can be explained, not just trusted."""

    total: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    penalties: dict[str, float] = field(default_factory=dict)
    rejections: list[str] = field(default_factory=list)

    @property
    def qualified(self) -> bool:
        return not self.rejections

    def explain(self) -> str:
        parts = [f"{name}={value:.2f}" for name, value in sorted(self.components.items())]
        penalty_parts = [
            f"{name}x{value:.2f}" for name, value in sorted(self.penalties.items()) if value < 1
        ]
        return " ".join(parts + penalty_parts)


class BaseScorer:
    def __init__(self, config: DiscoveryConfig):
        self.config = config

    def _common_rejections(self, trader: TraderMetrics) -> list[str]:
        """Floors that disqualify an account from BOTH rosters."""
        rejections: list[str] = []
        config = self.config
        month = trader.window("month")
        all_time = trader.window("allTime")

        if trader.days_active < config.min_days_active:
            rejections.append(f"only {trader.days_active:.0f}d of history")
        if month.is_empty and all_time.is_empty:
            rejections.append("no usable equity curve")
        # An account with no recent PERP activity cannot be copied by a perp bot,
        # however good its all-time numbers look. Without this floor, a dormant
        # account is scored on history alone and ranks above live traders.
        if month.n_periods < MIN_SCORING_PERIODS:
            rejections.append(
                f"only {month.n_periods} usable perp periods this month (dormant)"
            )
        if trader.turnover > config.max_turnover:
            rejections.append(f"turnover {trader.turnover:.0f}x equity (churn/wash risk)")
        if trader.perp_capital < config.min_account_value:
            rejections.append(
                f"perp capital ${trader.perp_capital:,.0f} below minimum "
                f"(account value ${trader.account_value:,.0f} is mostly not on perps)"
            )
        return rejections


class EliteScorer(BaseScorer):
    """Proven performers. Weighted toward durability over raw return."""

    WEIGHTS = {
        "track_record": 0.15,
        "roi_month": 0.15,
        "sharpe": 0.15,
        "calmar": 0.12,
        "consistency": 0.13,
        "smoothness": 0.15,
        "profit_factor": 0.08,
        "longevity": 0.07,
    }

    def score(self, trader: TraderMetrics) -> ScoreBreakdown:
        breakdown = ScoreBreakdown()
        breakdown.rejections = self._common_rejections(trader)

        month = trader.window("month")
        all_time = trader.window("allTime")
        config = self.config

        if month.max_drawdown > config.max_drawdown:
            breakdown.rejections.append(f"month drawdown {month.max_drawdown:.0%}")
        if month.consistency < config.min_consistency:
            breakdown.rejections.append(f"consistency {month.consistency:.0%}")
        if month.r_squared < config.min_r_squared:
            breakdown.rejections.append(f"curve R2 {month.r_squared:.2f} (erratic)")
        if month.concentration > config.max_concentration:
            breakdown.rejections.append(
                f"{month.concentration:.0%} of profit from one period (one-hit wonder)"
            )
        if not month.reliable:
            breakdown.rejections.append("month curve too coarse to judge")
        if month.roi <= 0:
            breakdown.rejections.append("month not profitable")
        if trader.track_record <= 0 and month.roi <= 0:
            breakdown.rejections.append("no lifetime profit")

        components = {
            # Lifetime PnL in dollars, log-scaled: $1k -> 0, $1M -> 1.
            "track_record": saturate(_log10_safe(trader.track_record), 6.0, 3.0),
            "roi_month": saturate(month.roi, 0.50),
            "sharpe": saturate(max(month.sharpe, all_time.sharpe), 4.0),
            "calmar": saturate(max(month.calmar, all_time.calmar), 10.0),
            "consistency": clamp(month.consistency or all_time.consistency, 0.0, 1.0),
            "smoothness": max(month.r_squared, all_time.r_squared),
            "profit_factor": saturate(max(month.profit_factor, all_time.profit_factor), 3.0, 1.0),
            "longevity": saturate(trader.days_active, 180.0),
        }
        breakdown.components = components
        raw = sum(components[name] * weight for name, weight in self.WEIGHTS.items())

        breakdown.penalties = {
            "drawdown": _drawdown_penalty(month.max_drawdown, config.max_drawdown),
            "concentration": _concentration_penalty(month.concentration, config.max_concentration),
            "turnover": _turnover_penalty(trader.turnover, config.max_turnover),
        }
        for penalty in breakdown.penalties.values():
            raw *= penalty

        breakdown.total = round(clamp(raw, 0.0, 1.0) * 100, 2)
        return breakdown


class RisingScorer(BaseScorer):
    """'On the come up': small, accelerating, and smooth.

    Deliberately NOT a filter over EliteScorer. A trader can be a great climber
    and rank poorly on elite criteria simply because they have not been around
    long enough — that is the point of having a second roster.
    """

    WEIGHTS = {
        "acceleration": 0.24,
        "roi_month": 0.18,
        "smoothness": 0.18,
        "consistency": 0.15,
        "growth": 0.13,
        "sortino": 0.12,
    }

    def score(self, trader: TraderMetrics) -> ScoreBreakdown:
        breakdown = ScoreBreakdown()
        breakdown.rejections = self._common_rejections(trader)

        config = self.config
        month = trader.window("month")
        week = trader.window("week")
        equity = trader.perp_capital
        pace_ratio = trader.pace_ratio

        # The climber band. Above it, size caps returns; below it, it is noise.
        if equity < config.rising_min_equity:
            breakdown.rejections.append(
                f"below climber band ({equity:,.0f} < {config.rising_min_equity:,.0f})"
            )
        if equity > config.rising_max_equity:
            breakdown.rejections.append(
                f"above climber band ({equity:,.0f} > {config.rising_max_equity:,.0f})"
            )
        if trader.days_active < config.rising_min_days_active:
            breakdown.rejections.append(
                f"only {trader.days_active:.0f}d of history "
                f"(climbers need {config.rising_min_days_active}d)"
            )
        if pace_ratio < config.rising_min_pace_ratio:
            breakdown.rejections.append(
                f"fading - this week is running at {pace_ratio:.0%} of their monthly pace"
            )
        if month.max_drawdown > config.rising_max_drawdown:
            breakdown.rejections.append(f"month drawdown {month.max_drawdown:.0%}")
        if month.consistency < config.rising_min_consistency:
            breakdown.rejections.append(f"consistency {month.consistency:.0%}")
        if month.r_squared < config.rising_min_r_squared:
            breakdown.rejections.append(f"curve R2 {month.r_squared:.2f} (erratic)")
        if month.concentration > config.max_concentration:
            breakdown.rejections.append(
                f"{month.concentration:.0%} of profit from one period (one-hit wonder)"
            )
        if month.roi <= 0:
            breakdown.rejections.append("month not profitable")
        if week.roi <= 0:
            breakdown.rejections.append("week not profitable (no longer climbing)")
        if not month.reliable:
            breakdown.rejections.append("month curve too coarse to judge")

        growth = safe_div(month.end_equity, month.start_equity, 1.0) if month.start_equity else 1.0
        components = {
            "acceleration": saturate(pace_ratio, 1.5),
            "roi_month": saturate(month.roi, 1.00),
            "smoothness": max(month.r_squared, week.r_squared),
            "consistency": clamp(month.consistency, 0.0, 1.0),
            "growth": saturate(growth, 2.5, 1.0),
            "sortino": saturate(max(month.sortino, week.sortino), 6.0),
        }
        breakdown.components = components
        raw = sum(components[name] * weight for name, weight in self.WEIGHTS.items())

        breakdown.penalties = {
            "drawdown": _drawdown_penalty(month.max_drawdown, config.rising_max_drawdown),
            "concentration": _concentration_penalty(month.concentration, config.max_concentration),
            "turnover": _turnover_penalty(trader.turnover, config.max_turnover),
            # The small-account bonus: same percentage return is more impressive
            # (and more repeatable for a copier) on a smaller book.
            "size_bonus": _size_bonus(equity, config.rising_min_equity, config.rising_max_equity),
        }
        for penalty in breakdown.penalties.values():
            raw *= penalty

        breakdown.total = round(clamp(raw, 0.0, 1.0) * 100, 2)
        return breakdown


# --------------------------------------------------------------------------- #
# penalties
# --------------------------------------------------------------------------- #


def _drawdown_penalty(drawdown: float, limit: float) -> float:
    """Linear down to 0.4 at the limit, then falls off a cliff past it."""
    if limit <= 0:
        return 1.0
    ratio = drawdown / limit
    if ratio <= 1.0:
        return clamp(1.0 - 0.6 * ratio, 0.4, 1.0)
    return clamp(0.4 / ratio, 0.0, 0.4)


def _concentration_penalty(concentration: float, limit: float) -> float:
    """No penalty while profit is spread out; steep once one period dominates."""
    if concentration <= 0.35:
        return 1.0
    if limit <= 0.35:
        return 0.5
    return clamp(1.0 - 0.7 * ((concentration - 0.35) / (limit - 0.35)), 0.2, 1.0)


def _turnover_penalty(turnover: float, limit: float) -> float:
    if limit <= 0 or turnover <= limit * 0.5:
        return 1.0
    return clamp(1.0 - 0.5 * ((turnover - limit * 0.5) / (limit * 0.5)), 0.35, 1.0)


def _size_bonus(equity: float, low: float, high: float) -> float:
    """1.0 at the bottom of the climber band, decaying to 0.55 at the top (log scale)."""
    import math

    if equity <= low or high <= low:
        return 1.0
    position = (math.log10(equity) - math.log10(low)) / (math.log10(high) - math.log10(low))
    return clamp(1.0 - 0.45 * position, 0.55, 1.0)
