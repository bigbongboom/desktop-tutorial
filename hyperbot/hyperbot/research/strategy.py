"""Infer a trading archetype from entry fingerprints, then test that inference.

TWO CLAIMS, KEPT SEPARATE
-------------------------
1. WHAT THEY DID: measured from their entries against real candles. "68% of
   their longs opened below the 20-EMA" is an observation.
2. WHETHER THAT RULE WORKS: a backtest of OUR OWN reconstruction of the pattern,
   run independently on the same market and period.

(2) is never evidence about (1). We cannot see anyone's rules, their sizing, their
stops, or the discretion behind an entry, and a mechanical rule that shares one
statistical property with a trader is not their strategy. The backtest says
whether the pattern we extracted has an edge on its own - which is worth knowing,
and is not the same question as whether this trader is good.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..util import safe_div
from .candles import Candle, EntryContext, atr, ema, rsi

# Below this many entries the shares are noise, not a fingerprint.
MIN_ENTRIES_FOR_ARCHETYPE = 6


@dataclass
class Fingerprint:
    """Aggregate statistics over a trader's entries."""

    entries: int = 0
    with_trend: float = 0.0
    breakout: float = 0.0
    pullback: float = 0.0
    mean_rsi: float = 50.0
    mean_distance_from_ema: float = 0.0
    mean_volatility: float = 0.0

    @property
    def usable(self) -> bool:
        return self.entries >= MIN_ENTRIES_FOR_ARCHETYPE


@dataclass
class Archetype:
    name: str
    summary: str
    confidence: str  # low | moderate | high
    evidence: list[str] = field(default_factory=list)


@dataclass
class BacktestResult:
    rule: str
    coin: str
    interval: str
    trades: int = 0
    wins: int = 0
    total_return: float = 0.0
    max_drawdown: float = 0.0
    gross_win: float = 0.0
    gross_loss: float = 0.0
    bars: int = 0

    @property
    def win_rate(self) -> float:
        return safe_div(self.wins, self.trades, 0.0)

    @property
    def profit_factor(self) -> float | None:
        if self.gross_loss == 0:
            return None if self.gross_win > 0 else 0.0
        return self.gross_win / abs(self.gross_loss)


def fingerprint(contexts: list[EntryContext]) -> Fingerprint:
    result = Fingerprint(entries=len(contexts))
    if not contexts:
        return result
    n = len(contexts)
    result.with_trend = sum(1 for c in contexts if c.with_trend) / n
    result.breakout = sum(1 for c in contexts if c.breakout) / n
    result.pullback = sum(1 for c in contexts if c.pullback) / n
    result.mean_rsi = sum(c.rsi for c in contexts) / n
    # Signed toward the trade: a long below the EMA and a short above it are the
    # same behaviour (entering against the recent move), so fold direction in.
    result.mean_distance_from_ema = sum(
        c.distance_from_ema if c.is_long else -c.distance_from_ema for c in contexts
    ) / n
    result.mean_volatility = sum(c.volatility for c in contexts) / n
    return result


def classify(print_: Fingerprint, median_hold_hours: float | None) -> Archetype:
    if not print_.usable:
        return Archetype(
            name="Not enough visible entries",
            summary=(
                f"Only {print_.entries} entries fall inside the fill window, which is "
                "too few to read a pattern from. Their exits are still measured."
            ),
            confidence="low",
        )

    evidence = [
        f"{print_.with_trend:.0%} of entries aligned with the 20/50 EMA trend",
        f"{print_.breakout:.0%} broke the prior 20-bar extreme",
        f"{print_.pullback:.0%} entered against the last three bars",
        f"average RSI at entry {print_.mean_rsi:.0f}",
        f"average entry {print_.mean_distance_from_ema:+.2%} from the 20-EMA",
    ]

    fast = median_hold_hours is not None and median_hold_hours < 4
    if print_.breakout >= 0.40 and print_.with_trend >= 0.55:
        name, summary = (
            "Breakout momentum",
            "Enters as price takes out recent highs (or lows, when short) in the "
            "direction the trend is already moving - buying strength, not value.",
        )
        confidence = "high" if print_.entries >= 20 else "moderate"
    elif print_.pullback >= 0.55 and print_.mean_distance_from_ema < 0:
        name, summary = (
            "Pullback / dip entry",
            "Enters after price moves against the intended direction, typically "
            "below the short-term average - buying weakness and waiting for reversion.",
        )
        confidence = "high" if print_.entries >= 20 else "moderate"
    elif print_.with_trend >= 0.65:
        name, summary = (
            "Trend following",
            "Enters in the direction of the prevailing trend without needing a "
            "breakout - adding to a move already underway.",
        )
        confidence = "moderate"
    elif print_.with_trend <= 0.40:
        name, summary = (
            "Counter-trend / fading",
            "Consistently enters against the prevailing trend, betting on "
            "exhaustion rather than continuation.",
        )
        confidence = "moderate"
    elif fast:
        name, summary = (
            "Intraday scalping",
            "Short holds with no strong directional or breakout bias - taking "
            "small moves rather than expressing a view.",
        )
        confidence = "low"
    else:
        name, summary = (
            "Mixed / discretionary",
            "No single entry condition dominates, which is what discretionary "
            "trading looks like from the outside.",
        )
        confidence = "low"
    return Archetype(name=name, summary=summary, confidence=confidence, evidence=evidence)


# --------------------------------------------------------------------------- #
# replication backtest
# --------------------------------------------------------------------------- #


def backtest(archetype: str, candles: list[Candle], coin: str, interval: str) -> BacktestResult:
    """Test a plain rule matching the archetype. Long-only, no lookahead.

    Signals are read on the close of bar i and the position is taken at bar i+1's
    OPEN - never that bar's close. Exit is a 2xATR target or 1xATR stop, with the
    stop checked first on any bar where both were touched, so an ambiguous bar
    counts as a loss rather than a win.
    """
    rule_names = {
        "Breakout momentum": "buy a 20-bar high while EMA20 > EMA50",
        "Pullback / dip entry": "buy when price is under EMA20 and RSI < 40, in an EMA20 > EMA50 uptrend",
        "Trend following": "buy when EMA20 crosses above EMA50",
        "Counter-trend / fading": "buy when RSI < 30",
        "Intraday scalping": "buy when price closes below the lower band of a 20-bar range",
    }
    result = BacktestResult(
        rule=rule_names.get(archetype, "no mechanical rule for this archetype"),
        coin=coin,
        interval=interval,
        bars=len(candles),
    )
    if archetype not in rule_names or len(candles) < 80:
        return result

    closes = [c.close for c in candles]
    fast, slow = ema(closes, 20), ema(closes, 50)
    strength = rsi(closes, 14)
    ranges = atr(candles, 14)

    def signal(i: int) -> bool:
        window = candles[max(0, i - 20) : i]
        if not window:
            return False
        if archetype == "Breakout momentum":
            return closes[i] > max(c.high for c in window) and fast[i] > slow[i]
        if archetype == "Pullback / dip entry":
            return closes[i] < fast[i] and strength[i] < 40 and fast[i] > slow[i]
        if archetype == "Trend following":
            return fast[i] > slow[i] and fast[i - 1] <= slow[i - 1]
        if archetype == "Counter-trend / fading":
            return strength[i] < 30
        return closes[i] < min(c.low for c in window) * 1.001

    equity, peak, position = 1.0, 1.0, None
    for i in range(60, len(candles) - 1):
        if position is None:
            if signal(i):
                entry = candles[i + 1].open
                risk = max(ranges[i], entry * 0.002)
                position = (entry, entry - risk, entry + 2 * risk)
            continue

        entry, stop, target = position
        bar = candles[i]
        # Stop first: if a single bar spans both levels we cannot know the order,
        # so assume the worse one.
        if bar.low <= stop:
            change = (stop - entry) / entry
        elif bar.high >= target:
            change = (target - entry) / entry
        else:
            continue

        result.trades += 1
        if change > 0:
            result.wins += 1
            result.gross_win += change
        else:
            result.gross_loss += change
        equity *= 1 + change
        peak = max(peak, equity)
        result.max_drawdown = max(result.max_drawdown, (peak - equity) / peak)
        position = None

    result.total_return = equity - 1.0
    return result
