"""Full research pass on one account: fills -> profile -> name -> strategy."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ..api.info import InfoClient
from ..log import get_logger
from ..util import safe_div
from .candles import EntryContext, build_context, parse_candles
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
    def __init__(self, info: InfoClient):
        self.info = info

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
