"""Reconstruct trading decisions from a public fill stream.

WHAT THE DATA ACTUALLY SUPPORTS
-------------------------------
`userFills` returns at most 2000 fills, newest first, and three things measured
on live accounts shape everything here:

1. For a trader who unwinds in pieces, that window can be ALL closes. One real
   account returned 2000/2000 fills labelled "Close Long"; its opening fills were
   outside the window and paging back did not recover them. So hold time and
   entry context cannot be required - they have to be optional.

2. Positions rarely return to flat. The same account crossed zero 3 times in 2000
   fills; it scales in and out continuously. Defining a "trade" as flat-to-flat
   would report 3 trades for a month of work.

3. Grouping closes by a time window is worse than useless: at 15 minutes it
   collapsed those 2000 fills into 20 "trades" and merged wins with losses until
   every one looked positive - a fabricated 100% win rate.

So the unit of realisation here is the ORDER (`oid`): all partial fills of one
order are one decision, and separate orders stay separate. That merges the 381
pieces of a single unwind without merging two different decisions, and it is
defined for every trader. Flat-to-flat round trips are tracked separately, using
`startPosition`, purely to recover hold time and entry price where they exist.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..api.info import Fill
from ..util import safe_div

FLAT = 1e-9


@dataclass
class ExitEvent:
    """One closing ORDER: realised P&L for a single decision to take money off."""

    coin: str
    is_long: bool  # the side that was closed
    time: int
    exit_price: float
    size: float
    pnl: float
    fees: float
    fills: int = 1
    order_id: int = 0

    @property
    def is_win(self) -> bool:
        return self.pnl > 0

    @property
    def notional(self) -> float:
        return self.exit_price * self.size


@dataclass
class RoundTrip:
    """A position tracked from first open to flat. Gives hold time and entry."""

    coin: str
    is_long: bool
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    size: float
    pnl: float

    @property
    def hold_seconds(self) -> float:
        return max(0.0, (self.exit_time - self.entry_time) / 1000.0)

    @property
    def return_pct(self) -> float:
        """Price move in the trade's favour, before leverage."""
        if self.entry_price <= 0:
            return 0.0
        raw = (self.exit_price - self.entry_price) / self.entry_price
        return raw if self.is_long else -raw


@dataclass
class TradeHistory:
    exits: list[ExitEvent] = field(default_factory=list)
    round_trips: list[RoundTrip] = field(default_factory=list)
    first_time: int = 0
    last_time: int = 0
    total_fills: int = 0
    closing_fills: int = 0
    fill_window_days: float = 0.0
    # Realised P&L that is NOT a trading decision: perp-expiry settlements and
    # spot sells. Excluded from win rate on purpose, but tracked so the totals
    # reconcile against the raw feed instead of quietly losing money.
    settlement_pnl: float = 0.0
    spot_pnl: float = 0.0

    @property
    def coverage(self) -> float:
        """Share of closing orders we could pair with a visible entry."""
        return safe_div(len(self.round_trips), len(self.exits), 0.0)

    @property
    def has_entry_data(self) -> bool:
        """Enough round trips to talk about hold time or entry timing at all."""
        return len(self.round_trips) >= 8

    @property
    def fills_per_order(self) -> float:
        return safe_div(self.closing_fills, len(self.exits), 0.0)


def _is_close(direction: str) -> bool:
    return "Close" in direction or ">" in direction


def _is_open(direction: str) -> bool:
    return "Open" in direction or ">" in direction


def reconstruct(fills: list[Fill]) -> TradeHistory:
    history = TradeHistory()
    # `f.time` alone would be falsy at timestamp 0 and silently drop the fill.
    ordered = sorted((f for f in fills if f.coin), key=lambda f: f.time)
    if not ordered:
        return history

    history.total_fills = len(ordered)
    history.first_time = ordered[0].time
    history.last_time = ordered[-1].time
    history.fill_window_days = (history.last_time - history.first_time) / 86_400_000

    # ---- exits: one per closing order --------------------------------------
    by_order: dict[tuple[str, int, bool], ExitEvent] = {}
    for fill in ordered:
        direction = fill.direction or ""
        if not _is_close(direction):
            if direction == "Settlement":
                history.settlement_pnl += fill.closed_pnl
            elif direction in ("Sell", "Buy", "Spot Dust Conversion"):
                history.spot_pnl += fill.closed_pnl
            continue
        history.closing_fills += 1
        is_long = "Long" in direction
        # An order id of 0 would collapse unrelated fills, so fall back to the
        # fill's own trade id space by keying on time.
        key = (fill.coin, fill.oid or -fill.time, is_long)
        existing = by_order.get(key)
        if existing is None:
            by_order[key] = ExitEvent(
                coin=fill.coin, is_long=is_long, time=fill.time,
                exit_price=fill.price, size=fill.size, pnl=fill.closed_pnl,
                fees=fill.fee, order_id=fill.oid,
            )
        else:
            total = existing.size + fill.size
            existing.exit_price = safe_div(
                existing.exit_price * existing.size + fill.price * fill.size,
                total, fill.price,
            )
            existing.size = total
            existing.pnl += fill.closed_pnl
            existing.fees += fill.fee
            existing.time = max(existing.time, fill.time)
            existing.fills += 1
    history.exits = sorted(by_order.values(), key=lambda e: e.time)

    # ---- round trips: flat -> flat, using startPosition ---------------------
    # startPosition is authoritative even when the opening fills are missing, so
    # a position that opens inside the window can still be followed to flat.
    entry_cost: dict[str, float] = {}
    entry_size: dict[str, float] = {}
    entry_time: dict[str, int] = {}
    realised: dict[str, float] = {}

    for fill in ordered:
        direction = fill.direction or ""
        if not (_is_open(direction) or _is_close(direction)):
            continue
        coin = fill.coin
        before = fill.start_position
        signed = fill.size if fill.is_buy else -fill.size
        after = before + signed

        if abs(before) < FLAT and abs(after) > FLAT:
            # A position opens here: this is the only entry we can trust.
            entry_cost[coin] = fill.price * fill.size
            entry_size[coin] = fill.size
            entry_time[coin] = fill.time
            realised[coin] = 0.0
        elif _is_open(direction) and coin in entry_time:
            entry_cost[coin] = entry_cost.get(coin, 0.0) + fill.price * fill.size
            entry_size[coin] = entry_size.get(coin, 0.0) + fill.size

        if _is_close(direction):
            realised[coin] = realised.get(coin, 0.0) + fill.closed_pnl

        if abs(after) < FLAT and coin in entry_time and entry_size.get(coin, 0) > 0:
            history.round_trips.append(
                RoundTrip(
                    coin=coin,
                    is_long=before > 0,
                    entry_time=entry_time[coin],
                    exit_time=fill.time,
                    entry_price=safe_div(entry_cost[coin], entry_size[coin], 0.0),
                    exit_price=fill.price,
                    size=entry_size[coin],
                    pnl=realised.get(coin, 0.0),
                )
            )
            for store in (entry_cost, entry_size, entry_time, realised):
                store.pop(coin, None)

    history.round_trips.sort(key=lambda t: t.exit_time)
    return history
