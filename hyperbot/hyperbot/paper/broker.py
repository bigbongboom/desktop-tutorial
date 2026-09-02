"""A simulated account, so "what would $1,000 have done" has a real answer.

Dry-run answers "what orders would it send". This answers "what would my money
have done", which is the question that matters before funding anything. It fills
at live marks, charges the costs a small copier actually pays, and marks to
market every cycle so the equity curve is honest rather than aspirational.

THE THREE COSTS THAT DECIDE A SMALL ACCOUNT
-------------------------------------------
* Taker fees. Every mirror order crosses the book: 4.5 bps by default.
* Slippage. A copier is never first; the price has already moved.
* Funding. Measured live, most of these markets pay +11% annualised and the
  tracked cohort is 95% long - so the copier PAYS. At 3x gross that is roughly
  33% of capital a year bleeding out before any trade is right or wrong. Ignore
  it and a paper test flatters itself into meaninglessness.

Nothing here touches an exchange. It cannot place an order or move a coin.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..log import get_logger
from ..util import now_ms, safe_div, sign

log = get_logger("paper")

# Hyperliquid's standard taker fee at the base tier.
DEFAULT_TAKER_FEE_BPS = 4.5
# Maintenance margin is roughly half the initial requirement.
MAINTENANCE_FRACTION = 0.5
# A residual worth less than this is rounding, not a position. Closing a leg can
# overshoot by a fraction of a unit and leave a "position" on the opposite side;
# left alone those accumulate and show up as phantom shorts.
DUST_USD = 1.0
# Annualising a cost measured over seconds produces nonsense (one run reported
# 64,578%/yr after 40 seconds). Below this, report the raw cost instead.
MIN_HOURS_FOR_ANNUALISING = 6.0


@dataclass
class PaperPosition:
    coin: str
    size: float = 0.0          # signed: positive long, negative short
    entry_price: float = 0.0   # size-weighted average
    leverage: float = 1.0

    def notional(self, mark: float) -> float:
        return abs(self.size) * mark

    def signed_notional(self, mark: float) -> float:
        return self.size * mark

    def unrealized(self, mark: float) -> float:
        return (mark - self.entry_price) * self.size


@dataclass
class PaperFill:
    ts_ms: int
    coin: str
    is_buy: bool
    size: float
    price: float
    fee: float
    realized: float
    reason: str = ""


@dataclass
class PaperState:
    starting_equity: float
    cash: float                      # collateral: realised P&L and costs
    positions: dict[str, PaperPosition] = field(default_factory=dict)
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    funding_paid: float = 0.0        # positive = paid out
    slippage_cost: float = 0.0
    trades: int = 0
    liquidated: bool = False
    high_water: float = 0.0
    started_ms: int = 0
    last_funding_ms: int = 0

    def equity(self, marks: dict[str, float]) -> float:
        unrealized = sum(
            p.unrealized(marks.get(coin, p.entry_price)) for coin, p in self.positions.items()
        )
        return self.cash + unrealized

    def gross_notional(self, marks: dict[str, float]) -> float:
        return sum(p.notional(marks.get(c, p.entry_price)) for c, p in self.positions.items())


class PaperBroker:
    """Simulates one account. Deterministic given the same marks and orders."""

    def __init__(
        self,
        starting_equity: float = 1000.0,
        *,
        taker_fee_bps: float = DEFAULT_TAKER_FEE_BPS,
        slippage_bps: float = 5.0,
    ):
        self.taker_fee_bps = taker_fee_bps
        self.slippage_bps = slippage_bps
        self.state = PaperState(
            starting_equity=starting_equity,
            cash=starting_equity,
            high_water=starting_equity,
            started_ms=now_ms(),
            last_funding_ms=now_ms(),
        )
        self.fills: list[PaperFill] = []

    # ---- execution -------------------------------------------------------- #

    def execute(
        self,
        coin: str,
        delta_notional: float,
        mark: float,
        *,
        leverage: float = 1.0,
        reason: str = "",
    ) -> PaperFill | None:
        """Apply a signed USD change to a position at `mark`, with costs."""
        state = self.state
        if state.liquidated or mark <= 0 or delta_notional == 0:
            return None

        is_buy = delta_notional > 0
        # A copier crosses the spread and arrives after the leader.
        fill_price = mark * (1 + (self.slippage_bps / 10_000.0) * (1 if is_buy else -1))
        size = abs(delta_notional) / fill_price
        if size <= 0:
            return None

        position = state.positions.get(coin) or PaperPosition(coin=coin)
        before = position.size
        after = before + (size if is_buy else -size)

        realized = 0.0
        if before != 0 and sign(before) != (1 if is_buy else -1):
            # Reducing or flipping: realise P&L on the closed portion.
            closed = min(abs(before), size)
            realized = (fill_price - position.entry_price) * closed * sign(before)

        if before == 0 or sign(before) == (1 if is_buy else -1):
            # Opening or adding: roll the weighted average entry.
            total = abs(before) + size
            position.entry_price = safe_div(
                position.entry_price * abs(before) + fill_price * size, total, fill_price
            )
        elif abs(after) > 1e-12 and sign(after) != sign(before):
            # Flipped through zero: the new leg starts here.
            position.entry_price = fill_price

        position.size = after
        position.leverage = max(1.0, leverage)

        fee = abs(delta_notional) * (self.taker_fee_bps / 10_000.0)
        slip = abs(fill_price - mark) * size

        state.cash += realized - fee
        state.realized_pnl += realized
        state.fees_paid += fee
        state.slippage_cost += slip
        state.trades += 1

        if abs(position.size) * fill_price < DUST_USD:
            # Realise whatever the stub is worth and drop it.
            if abs(position.size) > 0:
                stub = (fill_price - position.entry_price) * position.size
                state.cash += stub
                state.realized_pnl += stub
            state.positions.pop(coin, None)
        else:
            state.positions[coin] = position

        fill = PaperFill(
            ts_ms=now_ms(), coin=coin, is_buy=is_buy, size=size, price=fill_price,
            fee=fee, realized=realized, reason=reason,
        )
        self.fills.append(fill)
        del self.fills[:-500]
        return fill

    # ---- carrying costs --------------------------------------------------- #

    def apply_funding(self, marks: dict[str, float], rates: dict[str, float]) -> float:
        """Charge funding for the elapsed time. Longs pay a positive rate."""
        state = self.state
        now = now_ms()
        hours = (now - state.last_funding_ms) / 3_600_000
        if hours <= 0 or not state.positions:
            state.last_funding_ms = now
            return 0.0

        total = 0.0
        for coin, position in state.positions.items():
            mark = marks.get(coin)
            rate = rates.get(coin)
            if not mark or rate is None:
                continue
            # Positive rate: longs pay shorts.
            total += position.signed_notional(mark) * rate * hours

        state.cash -= total
        state.funding_paid += total
        state.last_funding_ms = now
        return total

    # ---- risk ------------------------------------------------------------- #

    def check_liquidation(self, marks: dict[str, float]) -> bool:
        """Flat approximation of cross-margin maintenance.

        Not the venue's exact formula - it is a floor that catches the case that
        actually matters at $1,000 and 20x, where a 5% adverse move is fatal.
        """
        state = self.state
        if state.liquidated or not state.positions:
            return False
        requirement = 0.0
        for coin, position in state.positions.items():
            mark = marks.get(coin, position.entry_price)
            requirement += position.notional(mark) / max(position.leverage, 1.0)
        equity = state.equity(marks)
        if equity <= requirement * MAINTENANCE_FRACTION:
            state.liquidated = True
            log.critical(
                "PAPER LIQUIDATION: equity $%.2f below maintenance $%.2f",
                equity, requirement * MAINTENANCE_FRACTION,
            )
            state.positions.clear()
            state.cash = max(0.0, equity)
        return state.liquidated

    def mark(self, marks: dict[str, float]) -> float:
        equity = self.state.equity(marks)
        self.state.high_water = max(self.state.high_water, equity)
        return equity

    # ---- reporting -------------------------------------------------------- #

    def summary(self, marks: dict[str, float]) -> dict:
        state = self.state
        equity = state.equity(marks)
        gross = state.gross_notional(marks)
        elapsed_hours = (now_ms() - state.started_ms) / 3_600_000
        elapsed_days = max(elapsed_hours / 24.0, 1e-9)
        costs = state.fees_paid + state.funding_paid + state.slippage_cost
        # Only annualise once the sample covers enough time to mean anything.
        drag = (
            safe_div(costs, state.starting_equity, 0.0) * (365.0 / elapsed_days)
            if elapsed_hours >= MIN_HOURS_FOR_ANNUALISING
            else None
        )
        return {
            "starting_equity": state.starting_equity,
            "equity": equity,
            "pnl": equity - state.starting_equity,
            "return_pct": safe_div(equity - state.starting_equity, state.starting_equity, 0.0),
            "realized_pnl": state.realized_pnl,
            "unrealized_pnl": equity - state.cash,
            "fees_paid": state.fees_paid,
            "funding_paid": state.funding_paid,
            "slippage_cost": state.slippage_cost,
            "total_costs": costs,
            # None until the run is long enough for the figure to mean anything.
            "cost_drag_annual_pct": drag,
            "cost_pct_of_capital": safe_div(costs, state.starting_equity, 0.0),
            "trades": state.trades,
            "gross_notional": gross,
            "leverage": safe_div(gross, equity, 0.0),
            "drawdown": safe_div(state.high_water - equity, state.high_water, 0.0),
            "high_water": state.high_water,
            "liquidated": state.liquidated,
            "days_running": (now_ms() - state.started_ms) / 86_400_000,
            "positions": [
                {
                    "coin": coin,
                    "side": "LONG" if p.size > 0 else "SHORT",
                    "size": p.size,
                    "entry": p.entry_price,
                    "mark": marks.get(coin, p.entry_price),
                    "notional": p.signed_notional(marks.get(coin, p.entry_price)),
                    "unrealized": p.unrealized(marks.get(coin, p.entry_price)),
                    "leverage": p.leverage,
                }
                for coin, p in sorted(
                    state.positions.items(),
                    key=lambda kv: -kv[1].notional(marks.get(kv[0], kv[1].entry_price)),
                )
            ],
            "recent_fills": [
                {
                    "ts": f.ts_ms, "coin": f.coin, "side": "BUY" if f.is_buy else "SELL",
                    "size": f.size, "price": f.price, "fee": f.fee,
                    "realized": f.realized, "reason": f.reason,
                }
                for f in self.fills[-20:][::-1]
            ],
        }

    # ---- persistence ------------------------------------------------------ #

    def to_dict(self) -> dict:
        state = self.state
        return {
            "starting_equity": state.starting_equity,
            "cash": state.cash,
            "realized_pnl": state.realized_pnl,
            "fees_paid": state.fees_paid,
            "funding_paid": state.funding_paid,
            "slippage_cost": state.slippage_cost,
            "trades": state.trades,
            "liquidated": state.liquidated,
            "high_water": state.high_water,
            "started_ms": state.started_ms,
            "last_funding_ms": state.last_funding_ms,
            "positions": [
                {"coin": p.coin, "size": p.size, "entry_price": p.entry_price,
                 "leverage": p.leverage}
                for p in state.positions.values()
            ],
            "fills": [
                {"ts_ms": f.ts_ms, "coin": f.coin, "is_buy": f.is_buy, "size": f.size,
                 "price": f.price, "fee": f.fee, "realized": f.realized, "reason": f.reason}
                for f in self.fills[-200:]
            ],
        }

    @classmethod
    def from_dict(cls, data: dict, *, taker_fee_bps: float = DEFAULT_TAKER_FEE_BPS,
                  slippage_bps: float = 5.0) -> "PaperBroker":
        broker = cls(data.get("starting_equity", 1000.0),
                     taker_fee_bps=taker_fee_bps, slippage_bps=slippage_bps)
        state = broker.state
        for key in ("cash", "realized_pnl", "fees_paid", "funding_paid", "slippage_cost",
                    "trades", "liquidated", "high_water", "started_ms", "last_funding_ms"):
            if key in data:
                setattr(state, key, data[key])
        for raw in data.get("positions", []):
            state.positions[raw["coin"]] = PaperPosition(
                coin=raw["coin"], size=raw["size"],
                entry_price=raw["entry_price"], leverage=raw.get("leverage", 1.0),
            )
        broker.fills = [PaperFill(**raw) for raw in data.get("fills", [])]
        return broker
