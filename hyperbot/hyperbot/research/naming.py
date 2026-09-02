"""Give each account a readable handle and a factual description.

These are BEHAVIOURAL labels derived from public trading data - "SOL Momentum
Scalper", not a claim about who anybody is. Nothing here infers or asserts a
real-world identity, and the description states only measured numbers.

The mapping is deterministic: the same profile always produces the same name, so
a handle stays stable between scans and can be recognised across sessions.
"""
from __future__ import annotations

from .profile import TraderProfile

# Coins whose ticker reads better with the venue prefix stripped.
def _coin_label(coin: str) -> str:
    if ":" in coin:  # equity perps arrive as "xyz:MSFT"
        return coin.split(":", 1)[1]
    return coin


def _scope(profile: TraderProfile) -> str:
    if not profile.coins:
        return "Idle"
    if profile.concentration >= 0.40:
        return _coin_label(profile.top_coin)
    if len(profile.coins) >= 8:
        return "Multi-Market"
    return "Rotational"


def _stance(profile: TraderProfile) -> str:
    return {
        "long-only": "Long-Side",
        "short-only": "Short-Side",
        "two-sided": "Two-Way",
    }.get(profile.direction_bias, "")


def _role(profile: TraderProfile) -> str:
    hold = profile.median_hold_hours
    if profile.activity == "high-frequency":
        return "Scalper"
    if hold is not None:
        if hold < 4:
            return "Intraday Trader"
        if hold < 48:
            return "Swing Trader"
        return "Position Holder"
    return {
        "active": "Active Trader",
        "swing": "Swing Trader",
        "position": "Position Holder",
    }.get(profile.activity, "Trader")


def build_name(profile: TraderProfile) -> str:
    """A short, stable handle, e.g. 'High-Leverage SOL Two-Way Scalper'."""
    if profile.trades == 0:
        return "Dormant Account"
    parts: list[str] = []
    if profile.leverage_band == "very high":
        parts.append("High-Leverage")
    parts.append(_scope(profile))
    stance = _stance(profile)
    if stance:
        parts.append(stance)
    parts.append(_role(profile))
    return " ".join(p for p in parts if p)


def build_description(profile: TraderProfile) -> str:
    """Two or three sentences, every number measured from public fills."""
    if profile.trades == 0:
        return "No closing orders in the visible fill window, so there is nothing to measure."

    coin_bit = (
        f"concentrated in {_coin_label(profile.top_coin)} "
        f"({profile.concentration:.0%} of exits)"
        if profile.concentration >= 0.30
        else f"spread across {len(profile.coins)} markets"
    )
    # Leverage is only observable on OPEN positions; a flat account has none to
    # report, and "up to 0x leverage" is worse than saying nothing.
    peak_leverage = max(profile.current_leverage, profile.max_position_leverage)
    leverage_bit = f", at up to {peak_leverage:.0f}x leverage" if peak_leverage >= 1 else ""
    lead = (
        f"Closed {profile.orders} orders over {profile.window_days:.0f} days, "
        f"{coin_bit}{leverage_bit}."
    )

    sides = []
    if profile.long.trades:
        sides.append(
            f"long {profile.long.win_rate:.0%} win rate on {profile.long.trades} exits "
            f"({_money(profile.long.pnl)})"
        )
    if profile.short.trades:
        sides.append(
            f"short {profile.short.win_rate:.0%} on {profile.short.trades} "
            f"({_money(profile.short.pnl)})"
        )
    split = "Sides: " + "; ".join(sides) + "." if sides else ""

    extra = []
    if profile.median_hold_hours is not None:
        extra.append(f"median hold {_duration(profile.median_hold_hours)}")
    if profile.payoff_ratio:
        extra.append(f"average win {profile.payoff_ratio:.1f}x the average loss")
    tail = (" " + ", ".join(extra).capitalize() + ".") if extra else ""

    caution = ""
    if profile.never_takes_losses:
        caution = (
            " Every realised exit is a win while the open book is under water - "
            "this account closes winners and holds losers, so its losses have not "
            "been taken yet."
        )
    elif profile.sample_quality in ("thin", "very thin"):
        caution = (
            f" Only {profile.orders} closing orders are visible, so treat the win "
            "rate as indicative rather than established."
        )
    return f"{lead} {split}{tail}{caution}".strip()


def _money(value: float) -> str:
    sign = "-" if value < 0 else "+"
    magnitude = abs(value)
    if magnitude >= 1_000_000:
        return f"{sign}${magnitude / 1_000_000:.1f}M"
    if magnitude >= 1_000:
        return f"{sign}${magnitude / 1_000:.0f}k"
    return f"{sign}${magnitude:.0f}"


def _duration(hours: float) -> str:
    if hours < 1:
        return f"{hours * 60:.0f} min"
    if hours < 48:
        return f"{hours:.1f} h"
    return f"{hours / 24:.1f} days"
