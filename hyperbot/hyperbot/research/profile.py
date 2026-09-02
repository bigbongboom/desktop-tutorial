"""Turn a trade history into the numbers a copier actually cares about.

Everything here is measured, never estimated. Where the fill window cannot
support a statistic (hold time without opening fills), the field stays None and
the UI says so rather than showing a number built from eight trades.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from ..api.info import AccountState
from ..util import safe_div
from .trades import ExitEvent, TradeHistory


@dataclass
class SideStats:
    """Performance on one side of the market."""

    label: str
    trades: int = 0
    wins: int = 0
    pnl: float = 0.0
    gross_win: float = 0.0
    gross_loss: float = 0.0
    volume: float = 0.0

    @property
    def win_rate(self) -> float:
        return safe_div(self.wins, self.trades, 0.0)

    @property
    def profit_factor(self) -> float | None:
        """None when there are no losses - undefined, not zero."""
        if self.gross_loss == 0:
            return None if self.gross_win > 0 else 0.0
        return self.gross_win / abs(self.gross_loss)

    @property
    def avg_pnl(self) -> float:
        return safe_div(self.pnl, self.trades, 0.0)

    @property
    def roi_on_volume(self) -> float:
        """P&L per dollar traded - comparable across account sizes."""
        return safe_div(self.pnl, self.volume, 0.0)


@dataclass
class TraderProfile:
    address: str

    # Always available (computed from closing fills).
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_fees: float = 0.0
    gross_win: float = 0.0
    gross_loss: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    long: SideStats = field(default_factory=lambda: SideStats("long"))
    short: SideStats = field(default_factory=lambda: SideStats("short"))
    coins: list[tuple[str, int]] = field(default_factory=list)
    window_days: float = 0.0
    trades_per_day: float = 0.0

    # Live position data.
    current_leverage: float = 0.0
    max_position_leverage: float = 0.0
    open_positions: int = 0
    account_value: float = 0.0

    settlement_pnl: float = 0.0  # perp expiry, not a trading decision
    spot_pnl: float = 0.0        # spot legs, not perp trading
    orders: int = 0            # closing orders = decisions to realise
    closing_fills: int = 0     # the pieces those orders filled in
    unrealized_pnl: float = 0.0

    # Only when opening fills were inside the window.
    coverage: float = 0.0
    has_entry_data: bool = False
    median_hold_hours: float | None = None
    mean_hold_hours: float | None = None
    median_return_pct: float | None = None

    # ---- derived ---------------------------------------------------------- #

    @property
    def win_rate(self) -> float:
        return safe_div(self.wins, self.trades, 0.0)

    @property
    def profit_factor(self) -> float | None:
        """None means undefined: they have never realised a loss."""
        if self.gross_loss == 0:
            return None if self.gross_win > 0 else 0.0
        return self.gross_win / abs(self.gross_loss)

    @property
    def avg_win(self) -> float:
        return safe_div(self.gross_win, self.wins, 0.0)

    @property
    def avg_loss(self) -> float:
        return safe_div(abs(self.gross_loss), self.losses, 0.0)

    @property
    def payoff_ratio(self) -> float:
        """Average win divided by average loss."""
        return safe_div(self.avg_win, self.avg_loss, 0.0)

    @property
    def expectancy(self) -> float:
        """Expected P&L per trade."""
        return safe_div(self.total_pnl, self.trades, 0.0)

    @property
    def long_share(self) -> float:
        return safe_div(self.long.trades, self.trades, 0.0)

    @property
    def direction_bias(self) -> str:
        share = self.long_share
        if self.trades < 5:
            return "unknown"
        if share >= 0.75:
            return "long-only"
        if share <= 0.25:
            return "short-only"
        return "two-sided"

    @property
    def top_coin(self) -> str:
        return self.coins[0][0] if self.coins else ""

    @property
    def concentration(self) -> float:
        """Share of trades in the single most-traded coin."""
        return safe_div(self.coins[0][1], self.trades, 0.0) if self.coins else 0.0

    @property
    def activity(self) -> str:
        if self.trades_per_day >= 20:
            return "high-frequency"
        if self.trades_per_day >= 4:
            return "active"
        if self.trades_per_day >= 0.7:
            return "swing"
        return "position"

    @property
    def never_takes_losses(self) -> bool:
        """Realised win rate at or near 100% while sitting on unrealised losses.

        This reads as a perfect record and is the opposite: the account closes
        winners and holds losers, so the losses are all still on the book. It is
        a risk flag, not an achievement, and it is why win rate is never shown
        here without the order count and the unrealised number beside it.
        """
        # A thin sample is reported separately by sample_quality; this flag is
        # specifically about losses sitting unrealised on the book.
        return self.trades >= 3 and self.win_rate >= 0.98 and self.unrealized_pnl < 0

    @property
    def sample_quality(self) -> str:
        """How much weight the win rate can carry."""
        if self.trades >= 60:
            return "strong"
        if self.trades >= 25:
            return "fair"
        if self.trades >= 10:
            return "thin"
        return "very thin"

    @property
    def leverage_band(self) -> str:
        peak = max(self.current_leverage, self.max_position_leverage)
        if peak >= 20:
            return "very high"
        if peak >= 10:
            return "high"
        if peak >= 4:
            return "moderate"
        return "low"


def build_profile(
    address: str,
    history: TradeHistory,
    state: AccountState | None = None,
) -> TraderProfile:
    profile = TraderProfile(address=address)
    exits: list[ExitEvent] = history.exits
    profile.window_days = history.fill_window_days
    profile.coverage = history.coverage
    profile.has_entry_data = history.has_entry_data

    profile.orders = len(exits)
    profile.closing_fills = history.closing_fills
    profile.settlement_pnl = history.settlement_pnl
    profile.spot_pnl = history.spot_pnl

    if state:
        profile.account_value = state.account_value
        profile.open_positions = len(state.positions)
        profile.unrealized_pnl = sum(p.unrealized_pnl for p in state.positions.values())
        profile.current_leverage = state.leverage
        profile.max_position_leverage = max(
            (p.leverage for p in state.positions.values()), default=0.0
        )

    if not exits:
        return profile

    counts: dict[str, int] = {}
    for exit_event in exits:
        profile.trades += 1
        profile.total_pnl += exit_event.pnl
        profile.total_fees += exit_event.fees
        counts[exit_event.coin] = counts.get(exit_event.coin, 0) + 1

        side = profile.long if exit_event.is_long else profile.short
        side.trades += 1
        side.pnl += exit_event.pnl
        side.volume += exit_event.notional

        if exit_event.is_win:
            profile.wins += 1
            profile.gross_win += exit_event.pnl
            side.wins += 1
            side.gross_win += exit_event.pnl
        else:
            profile.losses += 1
            profile.gross_loss += exit_event.pnl
            side.gross_loss += exit_event.pnl

        profile.best_trade = max(profile.best_trade, exit_event.pnl)
        profile.worst_trade = min(profile.worst_trade, exit_event.pnl)

    profile.coins = sorted(counts.items(), key=lambda kv: -kv[1])
    profile.trades_per_day = safe_div(profile.trades, max(profile.window_days, 1.0), 0.0)

    if history.has_entry_data:
        holds = [t.hold_seconds / 3600 for t in history.round_trips if t.hold_seconds > 0]
        returns = [t.return_pct for t in history.round_trips if t.entry_price > 0]
        profile.has_entry_data = True
        if holds:
            profile.median_hold_hours = statistics.median(holds)
            profile.mean_hold_hours = statistics.fmean(holds)
        if returns:
            profile.median_return_pct = statistics.median(returns)
    return profile
