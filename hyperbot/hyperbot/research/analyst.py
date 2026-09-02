"""Full research pass on one account: fills -> profile -> name -> strategy."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ..api.info import InfoClient
from ..log import get_logger
from ..util import safe_div
from .candles import EntryContext, build_context, parse_candles
from .copyability import CopyAssessment, assess
from .consensus import (
    FLOW_WINDOWS_HOURS,
    MIN_ACCOUNTS_FOR_FLOW,
    ConsensusReport,
    Stance,
    build_consensus,
    quality_weight,
)
from .naming import build_description, build_name
from .profile import TraderProfile, build_profile
from .strategy import Archetype, BacktestResult, Fingerprint, backtest, classify, fingerprint
from .trades import TradeHistory, reconstruct

log = get_logger("research")

# Hold time decides which candle resolution the entries are read against.
def pick_interval(median_hold_hours: float | None) -> str:
    if median_hold_hours is None:
        return "1h"
    if median_hold_hours < 2:
        return "5m"
    if median_hold_hours < 12:
        return "15m"
    if median_hold_hours < 96:
        return "1h"
    return "4h"


@dataclass
class TraderDossier:
    address: str
    name: str = ""
    description: str = ""
    profile: TraderProfile | None = None
    history: TradeHistory | None = None
    contexts: list[EntryContext] = field(default_factory=list)
    fingerprint: Fingerprint | None = None
    archetype: Archetype | None = None
    backtests: list[BacktestResult] = field(default_factory=list)
    interval: str = "1h"
    notes: list[str] = field(default_factory=list)
    # Current book, as fractions of THEIR equity - what a copier would mirror.
    positions_snapshot: list[dict[str, Any]] = field(default_factory=list)
    copyability: CopyAssessment | None = None

    def as_dict(self) -> dict[str, Any]:
        """Flat JSON for the dashboard."""
        p = self.profile
        if p is None:
            return {"address": self.address, "name": "unavailable"}
        return {
            "address": self.address,
            "name": self.name,
            "description": self.description,
            "notes": self.notes,
            "stats": {
                "orders": p.orders,
                "closing_fills": p.closing_fills,
                "win_rate": p.win_rate,
                "profit_factor": p.profit_factor,
                "total_pnl": p.total_pnl,
                "unrealized_pnl": p.unrealized_pnl,
                "settlement_pnl": p.settlement_pnl,
                "spot_pnl": p.spot_pnl,
                "fees": p.total_fees,
                "expectancy": p.expectancy,
                "avg_win": p.avg_win,
                "avg_loss": p.avg_loss,
                "payoff_ratio": p.payoff_ratio,
                "best_trade": p.best_trade,
                "worst_trade": p.worst_trade,
                "window_days": p.window_days,
                "trades_per_day": p.trades_per_day,
                "sample_quality": p.sample_quality,
                "never_takes_losses": p.never_takes_losses,
                "account_value": p.account_value,
                "current_leverage": p.current_leverage,
                "max_leverage": p.max_position_leverage,
                "leverage_band": p.leverage_band,
                "open_positions": p.open_positions,
                "activity": p.activity,
                "direction_bias": p.direction_bias,
                "concentration": p.concentration,
                "median_hold_hours": p.median_hold_hours,
                "coverage": p.coverage,
            },
            "sides": {
                side.label: {
                    "trades": side.trades,
                    "win_rate": side.win_rate,
                    "pnl": side.pnl,
                    "profit_factor": side.profit_factor,
                    "roi_on_volume": side.roi_on_volume,
                    "volume": side.volume,
                }
                for side in (p.long, p.short)
            },
            "coins": [{"coin": c, "trades": n} for c, n in p.coins[:10]],
            "fingerprint": (
                {
                    "entries": self.fingerprint.entries,
                    "with_trend": self.fingerprint.with_trend,
                    "breakout": self.fingerprint.breakout,
                    "pullback": self.fingerprint.pullback,
                    "mean_rsi": self.fingerprint.mean_rsi,
                    "mean_distance_from_ema": self.fingerprint.mean_distance_from_ema,
                    "mean_volatility": self.fingerprint.mean_volatility,
                }
                if self.fingerprint
                else None
            ),
            "archetype": (
                {
                    "name": self.archetype.name,
                    "summary": self.archetype.summary,
                    "confidence": self.archetype.confidence,
                    "evidence": self.archetype.evidence,
                }
                if self.archetype
                else None
            ),
            "backtests": [
                {
                    "rule": b.rule, "coin": b.coin, "interval": b.interval,
                    "trades": b.trades, "win_rate": b.win_rate,
                    "profit_factor": b.profit_factor, "total_return": b.total_return,
                    "max_drawdown": b.max_drawdown, "bars": b.bars,
                }
                for b in self.backtests
            ],
            "positions_snapshot": self.positions_snapshot,
            "copyability": (
                {
                    "score": self.copyability.score,
                    "verdict": self.copyability.verdict,
                    "reasons": self.copyability.reasons,
                    "strengths": self.copyability.strengths,
                    "expressible": self.copyability.expressible,
                    "smallest_order_usd": self.copyability.smallest_order_usd,
                    "positions": self.copyability.positions,
                }
                if self.copyability
                else None
            ),
            "recent_exits": [
                {
                    "coin": e.coin, "side": "long" if e.is_long else "short",
                    "time": e.time, "price": e.exit_price, "size": e.size,
                    "pnl": e.pnl, "fills": e.fills,
                }
                for e in (self.history.exits[-25:] if self.history else [])
            ][::-1],
        }


class Analyst:
    def __init__(
        self,
        info: InfoClient,
        *,
        capital: float = 1000.0,
        exposure_multiplier: float = 0.25,
    ):
        self.info = info
        # Copyability is judged for a specific account size: what a $1,000
        # copier can express is not what a $1M copier can.
        self.capital = capital
        self.exposure_multiplier = exposure_multiplier

    async def study(self, address: str, *, with_candles: bool = True) -> TraderDossier:
        dossier = TraderDossier(address=address)
        fills, state = await asyncio.gather(
            self.info.user_fills(address),
            self.info.account_state(address),
            return_exceptions=False,
        )
        history = reconstruct(fills)
        profile = build_profile(address, history, state)
        dossier.history = history
        dossier.profile = profile
        dossier.name = build_name(profile)
        dossier.description = build_description(profile)

        if state and state.account_value > 0:
            dossier.positions_snapshot = [
                {
                    "coin": coin,
                    "side": "LONG" if position.is_long else "SHORT",
                    "fraction": position.signed_notional / state.account_value,
                    "notional": position.signed_notional,
                    "leverage": position.leverage,
                    "unrealized": position.unrealized_pnl,
                }
                for coin, position in sorted(
                    state.positions.items(), key=lambda kv: -kv[1].position_value
                )
            ]

        if history.total_fills >= 2000:
            dossier.notes.append(
                "The fill feed is capped at 2000 records, so this covers the most "
                f"recent {history.fill_window_days:.0f} days only."
            )
        if profile.settlement_pnl:
            dossier.notes.append(
                f"${profile.settlement_pnl:,.0f} came from contract settlement rather "
                "than a trading decision, and is excluded from the win rate."
            )
        if history.exits and not history.has_entry_data:
            dossier.notes.append(
                "Their opening fills fall outside the window, so hold time and entry "
                "timing cannot be measured - only what they realised on the way out."
            )

        if with_candles and history.has_entry_data:
            await self._add_strategy(dossier)
        else:
            dossier.fingerprint = fingerprint([])
            dossier.archetype = classify(dossier.fingerprint, profile.median_hold_hours)

        dossier.copyability = assess(
            dossier.as_dict(),
            capital=self.capital,
            exposure_multiplier=self.exposure_multiplier,
        )
        return dossier

    async def _add_strategy(self, dossier: TraderDossier) -> None:
        history, profile = dossier.history, dossier.profile
        assert history and profile
        interval = pick_interval(profile.median_hold_hours)
        dossier.interval = interval

        # Only markets they actually traded, most-used first.
        wanted: dict[str, list] = {}
        for trip in history.round_trips:
            wanted.setdefault(trip.coin, []).append(trip)
        coins = sorted(wanted, key=lambda c: -len(wanted[c]))[:3]

        span_ms = max(
            history.last_time - history.first_time, 7 * 86_400_000
        ) + 3 * 86_400_000
        start = history.first_time - 2 * 86_400_000

        async def load(coin: str):
            try:
                raw = await self.info.candles(coin, interval, start, start + span_ms)
                return coin, parse_candles(raw)
            except Exception as exc:  # noqa: BLE001 - a missing market is not fatal
                log.debug("candles failed for %s: %s", coin, exc)
                return coin, []

        series = dict(await asyncio.gather(*(load(c) for c in coins)))

        contexts: list[EntryContext] = []
        for coin in coins:
            candles = series.get(coin) or []
            if len(candles) < 30:
                continue
            for trip in wanted[coin]:
                context = build_context(
                    coin, trip.is_long, trip.entry_time, trip.entry_price, candles
                )
                if context:
                    contexts.append(context)

        dossier.contexts = contexts
        dossier.fingerprint = fingerprint(contexts)
        dossier.archetype = classify(dossier.fingerprint, profile.median_hold_hours)

        if dossier.archetype and dossier.fingerprint.usable:
            for coin in coins[:2]:
                candles = series.get(coin) or []
                if len(candles) >= 80:
                    dossier.backtests.append(
                        backtest(dossier.archetype.name, candles, coin, interval)
                    )

    async def study_many(
        self, addresses: list[str], *, concurrency: int = 4
    ) -> list[TraderDossier]:
        semaphore = asyncio.Semaphore(concurrency)

        async def one(address: str) -> TraderDossier | None:
            async with semaphore:
                try:
                    return await self.study(address)
                except Exception as exc:  # noqa: BLE001
                    log.warning("research failed for %s: %s", address, exc)
                    return None

        results = await asyncio.gather(*(one(a) for a in addresses))
        return [r for r in results if r is not None]


# --------------------------------------------------------------------------- #
# consensus across the tracked accounts
# --------------------------------------------------------------------------- #


async def gather_consensus(
    info: InfoClient,
    accounts: list[tuple[str, str, TraderProfile | None]],
    *,
    concurrency: int = 6,
) -> ConsensusReport:
    """Live positioning across `accounts`, plus flow over an adaptive window.

    `accounts` is (address, display name, profile-or-None). The profile supplies
    the per-side track record that weights each opinion; without one the account
    still counts, at a low default weight.
    """
    import time as _time

    semaphore = asyncio.Semaphore(concurrency)

    async def state_of(address: str):
        async with semaphore:
            try:
                return address, await info.account_state(address)
            except Exception as exc:  # noqa: BLE001 - one bad account is not fatal
                log.debug("state failed for %s: %s", address, exc)
                return address, None

    states = dict(await asyncio.gather(*(state_of(a) for a, _, _ in accounts)))

    # Widen the flow window until enough accounts have actually traded inside it.
    now_ms = int(_time.time() * 1000)
    fills_by_address: dict[str, list] = {}
    chosen_window = FLOW_WINDOWS_HOURS[-1]
    for window in FLOW_WINDOWS_HOURS:
        start = now_ms - window * 3_600_000

        async def fills_of(address: str):
            async with semaphore:
                try:
                    return address, await info.user_fills_by_time(address, start)
                except Exception as exc:  # noqa: BLE001
                    log.debug("fills failed for %s: %s", address, exc)
                    return address, []

        fills_by_address = dict(
            await asyncio.gather(*(fills_of(a) for a, _, _ in accounts))
        )
        active = sum(1 for f in fills_by_address.values() if f)
        chosen_window = window
        if active >= MIN_ACCOUNTS_FOR_FLOW:
            break
    active_accounts = sum(1 for f in fills_by_address.values() if f)
    log.info(
        "consensus: flow window %dh covers %d active accounts",
        chosen_window, active_accounts,
    )

    profiles = {address: profile for address, _, profile in accounts}
    names = {address: name for address, name, _ in accounts}

    stances_by_coin: dict[str, list[Stance]] = {}
    with_positions = 0
    for address, state in states.items():
        if state is None or not state.positions or state.account_value <= 0:
            continue
        with_positions += 1
        profile = profiles.get(address)

        # Net notional traded per coin inside the window, signed by direction.
        flow: dict[str, float] = {}
        for fill in fills_by_address.get(address, []):
            direction = fill.direction or ""
            signed = 1.0 if fill.is_buy else -1.0
            flow[fill.coin] = flow.get(fill.coin, 0.0) + signed * fill.notional

        for coin, position in state.positions.items():
            is_long = position.is_long
            stances_by_coin.setdefault(coin, []).append(
                Stance(
                    address=address,
                    name=names.get(address, address),
                    is_long=is_long,
                    position_fraction=position.signed_notional / state.account_value,
                    notional=position.signed_notional,
                    leverage=position.leverage,
                    unrealized=position.unrealized_pnl,
                    quality=quality_weight(profile, is_long),
                    side_win_rate=(
                        (profile.long if is_long else profile.short).win_rate
                        if profile else 0.0
                    ),
                    side_trades=(
                        (profile.long if is_long else profile.short).trades
                        if profile else 0
                    ),
                    flow_usd=flow.get(coin, 0.0),
                )
            )

    return build_consensus(
        stances_by_coin,
        accounts_considered=len(accounts),
        accounts_with_positions=with_positions,
        accounts_active=active_accounts,
        flow_window_hours=chosen_window,
    )
