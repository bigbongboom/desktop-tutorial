"""Candle context: what the market was doing at the moment they entered.

This is the honest core of "strategy research". We cannot see anyone's rules or
their reasoning. What we CAN see is the price action at the exact minute they
opened a position, and across many entries that leaves a fingerprint: a trader
who consistently buys 3% below the 20-period average is buying dips whether or
not they would describe it that way.

Everything here is descriptive. Nothing predicts a price.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass

from ..util import safe_div, to_float


@dataclass
class Candle:
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def parse_candles(raw: list[dict]) -> list[Candle]:
    out = [
        Candle(
            time=int(c["t"]),
            open=to_float(c["o"]),
            high=to_float(c["h"]),
            low=to_float(c["l"]),
            close=to_float(c["c"]),
            volume=to_float(c.get("v")),
        )
        for c in raw or []
    ]
    out.sort(key=lambda c: c.time)
    return out


# --------------------------------------------------------------------------- #
# indicators
# --------------------------------------------------------------------------- #


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(value * k + out[-1] * (1 - k))
    return out


def rsi(values: list[float], period: int = 14) -> list[float]:
    """Wilder's RSI. Index-aligned with `values`; the warm-up region is 50."""
    if len(values) <= period:
        return [50.0] * len(values)
    gains, losses = [], []
    for previous, current in zip(values, values[1:]):
        change = current - previous
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out = [50.0] * (period + 1)
    for index in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[index]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index]) / period
        if avg_loss == 0:
            out.append(100.0)
        else:
            rs = avg_gain / avg_loss
            out.append(100.0 - 100.0 / (1 + rs))
    return out[: len(values)] + [out[-1]] * max(0, len(values) - len(out))


def atr(candles: list[Candle], period: int = 14) -> list[float]:
    if not candles:
        return []
    ranges = [candles[0].high - candles[0].low]
    for previous, current in zip(candles, candles[1:]):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return ema(ranges, period)


# --------------------------------------------------------------------------- #
# entry context
# --------------------------------------------------------------------------- #


@dataclass
class EntryContext:
    """Where one entry sat relative to the market at that moment."""

    coin: str
    is_long: bool
    time: int
    price: float
    trend_up: bool           # fast EMA above slow EMA
    with_trend: bool         # position agrees with that trend
    distance_from_ema: float # (price - EMA20) / EMA20, signed
    rsi: float
    breakout: bool           # took out the prior 20-bar extreme in their direction
    pullback: bool           # entered against the last few bars' drift
    volatility: float        # ATR as a share of price


def build_context(
    coin: str, is_long: bool, entry_time: int, entry_price: float, candles: list[Candle]
) -> EntryContext | None:
    """Locate an entry on the candle series and describe the setup."""
    if len(candles) < 30:
        return None
    times = [c.time for c in candles]
    index = bisect.bisect_right(times, entry_time) - 1
    if index < 25 or index >= len(candles):
        return None

    closes = [c.close for c in candles]
    fast = ema(closes, 20)
    slow = ema(closes, 50)
    strength = rsi(closes, 14)
    ranges = atr(candles, 14)

    window = candles[max(0, index - 20) : index]
    prior_high = max((c.high for c in window), default=entry_price)
    prior_low = min((c.low for c in window), default=entry_price)
    recent = candles[max(0, index - 3) : index + 1]
    drift = recent[-1].close - recent[0].open if recent else 0.0

    trend_up = fast[index] > slow[index]
    return EntryContext(
        coin=coin,
        is_long=is_long,
        time=entry_time,
        price=entry_price,
        trend_up=trend_up,
        with_trend=(trend_up == is_long),
        distance_from_ema=safe_div(entry_price - fast[index], fast[index], 0.0),
        rsi=strength[index],
        breakout=(entry_price > prior_high) if is_long else (entry_price < prior_low),
        # Buying while the last few bars fell (or shorting while they rose).
        pullback=(drift < 0) if is_long else (drift > 0),
        volatility=safe_div(ranges[index], entry_price, 0.0),
    )
