"""Translate leader books into a target book for our account.

The unit of copying is a POSITION FRACTION: what share of their own equity a
leader has committed to a coin, long or short. Copying notional directly would
make a $10M whale's $2M position unrepresentable in a $2k account; copying
fractions scales cleanly at any size.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..api.info import AccountState
from ..config import CopyConfig, RiskConfig
from ..log import get_logger
from ..util import clamp, safe_div

log = get_logger("copy.sizing")


@dataclass
class LeaderView:
    """One leader's book, expressed as fractions of their own equity."""

    address: str
    label: str
    equity: float
    weights: dict[str, float] = field(default_factory=dict)  # coin -> signed fraction

    @classmethod
    def from_state(cls, state: AccountState, label: str = "") -> "LeaderView":
        view = cls(address=state.address, label=label or state.address, equity=state.account_value)
        if state.account_value <= 0:
            return view
        for coin, position in state.positions.items():
            view.weights[coin] = position.signed_notional / state.account_value
        return view


@dataclass
class TargetPosition:
    coin: str
    weight: float  # signed fraction of OUR equity
    notional: float  # signed USD
    contributors: dict[str, float] = field(default_factory=dict)

    @property
    def direction(self) -> str:
        if self.notional > 0:
            return "LONG"
        return "SHORT" if self.notional < 0 else "FLAT"


@dataclass
class TargetBook:
    positions: dict[str, TargetPosition] = field(default_factory=dict)
    equity: float = 0.0
    gross_scale: float = 1.0  # <1 when gross exposure had to be scaled back
    clamped: list[str] = field(default_factory=list)

    def notional(self, coin: str) -> float:
        position = self.positions.get(coin)
        return position.notional if position else 0.0

    @property
    def gross(self) -> float:
        return sum(abs(position.notional) for position in self.positions.values())

    @property
    def net(self) -> float:
        return sum(position.notional for position in self.positions.values())


def build_target_book(
    leaders: dict[str, LeaderView],
    allocations: dict[str, float],
    our_equity: float,
    copy_config: CopyConfig,
    risk_config: RiskConfig,
) -> TargetBook:
    """Blend leader books into the position set we want to hold.

    target_weight[coin] = sum over leaders of (allocation * leader_weight[coin])

    Longs and shorts are symmetric: a negative blended weight is a short, and two
    leaders on opposite sides of the same coin net off, which is the correct
    outcome - we hold the roster's actual consensus, not both sides at once.
    """
    book = TargetBook(equity=our_equity)
    if our_equity <= 0 or not leaders:
        return book

    blended: dict[str, float] = {}
    contributors: dict[str, dict[str, float]] = {}
    for address, view in leaders.items():
        allocation = allocations.get(address, 0.0)
        if allocation <= 0 or view.equity <= 0:
            continue
        for coin, weight in view.weights.items():
            blended[coin] = blended.get(coin, 0.0) + allocation * weight
            contributors.setdefault(coin, {})[view.label] = weight

    allow = {coin.upper() for coin in risk_config.coin_allowlist}
    deny = {coin.upper() for coin in risk_config.coin_denylist}

    for coin, weight in blended.items():
        if deny and coin.upper() in deny:
            continue
        if allow and coin.upper() not in allow:
            continue
        scaled = weight * copy_config.exposure_multiplier
        capped = clamp(scaled, -risk_config.max_position_pct, risk_config.max_position_pct)
        if capped != scaled:
            book.clamped.append(coin)
        if capped == 0:
            continue
        book.positions[coin] = TargetPosition(
            coin=coin,
            weight=capped,
            notional=capped * our_equity,
            contributors=contributors.get(coin, {}),
        )

    _apply_gross_cap(book, risk_config, our_equity)
    _apply_position_limit(book, risk_config)
    return book


def _apply_gross_cap(book: TargetBook, risk_config: RiskConfig, our_equity: float) -> None:
    """Scale the whole book down proportionally rather than dropping names.

    Proportional scaling preserves the roster's relative conviction; dropping the
    smallest positions would quietly change the strategy into something else.
    """
    limit = risk_config.max_gross_exposure * our_equity
    gross = book.gross
    if limit <= 0 or gross <= limit:
        return
    scale = limit / gross
    book.gross_scale = scale
    for position in book.positions.values():
        position.weight *= scale
        position.notional *= scale
    log.info("gross exposure %.2fx capped to %.2fx (scaled %.0f%%)",
             safe_div(gross, our_equity), risk_config.max_gross_exposure, scale * 100)


def _apply_position_limit(book: TargetBook, risk_config: RiskConfig) -> None:
    """Keep only the largest N targets when over the concurrency limit."""
    limit = risk_config.max_concurrent_positions
    if limit <= 0 or len(book.positions) <= limit:
        return
    ranked = sorted(book.positions.values(), key=lambda p: abs(p.notional), reverse=True)
    keep = {position.coin for position in ranked[:limit]}
    dropped = [coin for coin in book.positions if coin not in keep]
    for coin in dropped:
        del book.positions[coin]
    log.info("position limit %d: dropped %s", limit, ", ".join(dropped))
