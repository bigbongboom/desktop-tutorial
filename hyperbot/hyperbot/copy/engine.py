"""The copy engine: watch leaders, converge on their book, narrate everything."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..api.exchange import ExchangeClient
from ..api.info import AccountState, Fill, InfoClient
from ..api.ws import WSManager
from ..config import Config
from ..discovery.scanner import ScoredTrader, TraderScanner, allocations, select_roster
from ..log import get_logger
from ..notify.dispatcher import Dispatcher, Severity
from ..store.db import Store
from ..util import pct, short_addr, usd, utc_now
from .reconciler import Adjustment, reconcile
from .risk import RiskManager
from .sizing import LeaderView, build_target_book

log = get_logger("copy.engine")


@dataclass
class Leader:
    address: str
    label: str
    allocation: float
    source: str = "elite"
    view: LeaderView | None = None


@dataclass
class EngineStats:
    cycles: int = 0
    orders_sent: int = 0
    orders_filled: int = 0
    orders_rejected: int = 0
    last_equity: float = 0.0
    started: str = field(default_factory=lambda: utc_now().isoformat(timespec="seconds"))


class CopyEngine:
    def __init__(
        self,
        config: Config,
        info: InfoClient,
        exchange: ExchangeClient,
        dispatcher: Dispatcher,
        store: Store,
    ):
        self.config = config
        self.info = info
        self.exchange = exchange
        self.notify = dispatcher
        self.store = store
        self.risk = RiskManager(config.risk)
        self.stats = EngineStats()
        self.leaders: dict[str, Leader] = {}
        self.ws = WSManager(config.ws_url)
        self._reconcile_now = asyncio.Event()
        self._running = False
        self._last_positions: dict[str, float] = {}
        self._positions_seeded = False
        self._last_summary_day = ""

    # ---- roster ----------------------------------------------------------- #

    async def load_roster(self, *, force_refresh: bool = False) -> list[Leader]:
        copy_config = self.config.copy
        if copy_config.manual_leaders:
            self.leaders = {
                address.lower(): Leader(
                    address=address.lower(),
                    label=short_addr(address),
                    allocation=1.0 / len(copy_config.manual_leaders),
                    source="manual",
                )
                for address in copy_config.manual_leaders
            }
            log.info("following %d manually configured leaders", len(self.leaders))
            return list(self.leaders.values())

        scanner = TraderScanner(self.info, self.config.discovery)
        result = await scanner.scan(force_refresh=force_refresh)
        chosen: list[ScoredTrader] = select_roster(
            result,
            mode=copy_config.roster,
            max_leaders=copy_config.max_leaders,
            rising_slots=copy_config.rising_slots,
        )
        weights = allocations(chosen, mode=copy_config.allocation)

        previous = set(self.leaders)
        self.leaders = {}
        for trader in chosen:
            self.store.save_trader(trader)
            source = "rising" if trader.rising.total >= trader.elite.total else "elite"
            self.leaders[trader.address.lower()] = Leader(
                address=trader.address.lower(),
                label=trader.label,
                allocation=weights.get(trader.address, 0.0),
                source=source,
            )
        self.store.replace_roster(
            [(l.address, l.label, l.allocation, l.source) for l in self.leaders.values()]
        )

        added = set(self.leaders) - previous
        dropped = previous - set(self.leaders)
        if added or dropped:
            await self.notify.info(
                "Leader roster updated",
                result.summary(),
                fields={
                    "following": ", ".join(
                        f"{l.label} {l.allocation:.0%} ({l.source})"
                        for l in self.leaders.values()
                    )
                    or "none",
                    "added": ", ".join(short_addr(a) for a in added) or "-",
                    "dropped": ", ".join(short_addr(a) for a in dropped) or "-",
                },
                dedupe_key="roster",
            )
        return list(self.leaders.values())

    # ---- leader tracking --------------------------------------------------- #

    async def refresh_leader_views(self) -> None:
        addresses = list(self.leaders)
        if not addresses:
            return
        states = await self.info.account_states(addresses)
        for address, state in states.items():
            leader = self.leaders.get(address.lower())
            if leader:
                leader.view = LeaderView.from_state(state, leader.label)

    async def _on_leader_fill(self, address: str, fill: Fill) -> None:
        """A leader traded: report it and pull the next reconcile forward."""
        leader = self.leaders.get(address.lower())
        if leader is None:
            return
        await self.notify.trade(
            f"{leader.label} {fill.direction or ('BUY' if fill.is_buy else 'SELL')} {fill.coin}",
            f"{fill.size:g} {fill.coin} @ {fill.price:g} ({usd(fill.notional)})",
            fields={
                "closed pnl": usd(fill.closed_pnl) if fill.closed_pnl else "-",
                "leader": short_addr(leader.address),
            },
            dedupe_key=f"leaderfill:{address}:{fill.coin}:{fill.direction}",
        )
        if self.config.copy.fill_trigger:
            self._reconcile_now.set()

    async def _on_ws_status(self, state: str, detail: str) -> None:
        if state == "disconnected":
            await self.notify.warn(
                "Leader stream disconnected",
                f"{detail}. Reconnecting; the engine keeps reconciling on its timer.",
                dedupe_key="ws",
            )

    # ---- the cycle --------------------------------------------------------- #

    async def run_cycle(self) -> None:
        self.stats.cycles += 1
        account = await self.info.account_state(self.config.account_address)
        self.stats.last_equity = account.account_value
        meta = await self.info.asset_meta(refresh=True)

        state = self.risk.observe(account)
        day_pnl, day_pnl_pct = self.risk.day_pnl(account.account_value)
        self.store.record_equity(
            account.account_value, account.total_notional, day_pnl, len(account.positions)
        )

        if state.kill_switch and state.kill_reason:
            await self.notify.critical(
                "KILL SWITCH TRIPPED",
                state.kill_reason,
                fields={"equity": usd(account.account_value), "day P&L": usd(day_pnl)},
                dedupe_key=f"kill:{state.day}",
            )
            if self.config.risk.flatten_on_kill:
                await self.flatten(account, meta, reason="kill switch")
                return
        if state.halted:
            await self.notify.critical(
                "Engine halted", state.halt_reason, dedupe_key="halt"
            )
            return

        await self.refresh_leader_views()
        views = {a: l.view for a, l in self.leaders.items() if l.view is not None}
        if not views:
            await self.notify.warn(
                "No leader data this cycle", "Skipping - will retry.", dedupe_key="noleaders"
            )
            return

        book = build_target_book(
            views,
            {a: l.allocation for a, l in self.leaders.items()},
            account.account_value,
            self.config.copy,
            self.config.risk,
        )
        targets = {coin: position.notional for coin, position in book.positions.items()}

        adjustments = reconcile(
            account, targets, meta, self.config.copy, self.config.risk,
            allow_opening=self.risk.allows_opening(),
        )
        await self._report_position_changes(account)

        if not adjustments:
            log.info(
                "cycle %d: in sync | equity %s | gross %s | day %s",
                self.stats.cycles, usd(account.account_value),
                usd(account.total_notional), pct(day_pnl_pct),
            )
            return

        log.info("cycle %d: %d adjustments", self.stats.cycles, len(adjustments))
        for adjustment in adjustments:
            log.info("  %s", adjustment.describe())
            await self._execute(adjustment, meta, book)

    async def _execute(self, adjustment: Adjustment, meta: dict, book) -> None:
        asset = meta.get(adjustment.coin)
        if asset is None:
            return
        if adjustment.kind in ("open", "increase", "flip"):
            leverage = min(self.config.risk.max_leverage, asset.max_leverage)
            await self.exchange.set_leverage(adjustment.coin, leverage)

        contributors = ""
        target = book.positions.get(adjustment.coin)
        if target:
            contributors = ", ".join(
                f"{label} {weight:+.1%}" for label, weight in target.contributors.items()
            )

        order = self.exchange.build_order(
            adjustment.coin,
            adjustment.delta_notional,
            asset,
            reduce_only=adjustment.reduce_only,
            reason=f"{adjustment.kind}: {contributors}" if contributors else adjustment.kind,
        )
        if order is None:
            log.debug("%s: delta too small to express at asset precision", adjustment.coin)
            return

        row_id = self.store.record_order_intent(order, adjustment.kind, not self.exchange.live)
        result = await self.exchange.place(order)
        self.store.record_order_result(row_id, result)
        self.stats.orders_sent += 1

        if result.ok and result.filled_size:
            self.stats.orders_filled += 1
        elif not result.ok:
            self.stats.orders_rejected += 1

        severity = Severity.TRADE if result.ok else Severity.WARN
        await self.notify.send(
            _order_event(adjustment, order, result, contributors, severity)
        )

    async def _report_position_changes(self, account: AccountState) -> None:
        """Announce our own position transitions, not just the orders behind them."""
        current = {coin: p.signed_notional for coin, p in account.positions.items()}
        if not self._positions_seeded:
            # First sight of the account: adopt whatever is already open as the
            # baseline. Without this, starting the bot on an existing book
            # announces every held position as freshly "Opened".
            self._positions_seeded = True
            self._last_positions = current
            if current:
                log.info(
                    "adopting %d pre-existing positions as baseline: %s",
                    len(current), ", ".join(sorted(current)),
                )
            return
        for coin, notional in current.items():
            previous = self._last_positions.get(coin, 0.0)
            if previous == 0 and notional != 0:
                position = account.positions[coin]
                await self.notify.trade(
                    f"Opened {'LONG' if notional > 0 else 'SHORT'} {coin}",
                    f"{abs(position.size):g} {coin} @ {position.entry_price:g} "
                    f"({usd(abs(notional))}, {position.leverage:g}x)",
                    fields={"liquidation": f"{position.liquidation_price:g}"
                            if position.liquidation_price else "-"},
                )
        for coin, previous in self._last_positions.items():
            if previous != 0 and current.get(coin, 0.0) == 0:
                await self.notify.trade(f"Closed {coin}", f"was {usd(abs(previous))}")
        self._last_positions = current

    async def flatten(self, account: AccountState, meta: dict, *, reason: str) -> None:
        """Close everything with reduce-only orders."""
        if not account.positions:
            return
        await self.notify.critical(
            "Flattening all positions", reason,
            fields={"positions": ", ".join(account.positions)},
        )
        for coin, position in account.positions.items():
            asset = meta.get(coin)
            if asset is None:
                continue
            order = self.exchange.build_order(
                coin, -position.signed_notional, asset, reduce_only=True, reason=f"flatten: {reason}"
            )
            if order is None:
                continue
            row_id = self.store.record_order_intent(order, "close", not self.exchange.live)
            result = await self.exchange.place(order)
            self.store.record_order_result(row_id, result)
            log.info("  %s", result.describe())

    async def _daily_summary(self) -> None:
        today = utc_now().strftime("%Y-%m-%d")
        if self._last_summary_day == today or utc_now().hour != self.config.notify.daily_summary_hour_utc:
            return
        self._last_summary_day = today
        day_pnl, day_pnl_pct = self.risk.day_pnl(self.stats.last_equity)
        await self.notify.info(
            f"Daily summary {today}",
            fields={
                "equity": usd(self.stats.last_equity),
                "day P&L": f"{usd(day_pnl)} ({pct(day_pnl_pct)})",
                "cycles": str(self.stats.cycles),
                "orders": f"{self.stats.orders_filled} filled / "
                          f"{self.stats.orders_rejected} rejected",
                "leaders": ", ".join(l.label for l in self.leaders.values()),
            },
        )

    # ---- lifecycle --------------------------------------------------------- #

    async def start(self) -> None:
        self._running = True
        await self.load_roster()

        self.ws.on_fill(self._on_leader_fill)
        self.ws.on_status(self._on_ws_status)
        self.ws.follow(list(self.leaders))
        await self.ws.start()

        blockers = self.config.live_blockers()
        await self.notify.info(
            "Copy engine started",
            "DRY RUN - no orders will be sent." if blockers else
            f"LIVE on {self.config.network}.",
            fields={
                "account": short_addr(self.config.account_address) or "not set",
                "leaders": str(len(self.leaders)),
                "exposure": f"{self.config.copy.exposure_multiplier:.2f}x of leader weights",
                "max gross": f"{self.config.risk.max_gross_exposure:.1f}x equity",
                "daily loss limit": f"{self.config.risk.daily_loss_limit:.0%}",
                "blockers": "; ".join(blockers) if blockers else "none",
            },
        )

        last_roster_refresh = utc_now()
        while self._running:
            try:
                await self.run_cycle()
                await self._daily_summary()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad cycle must not kill the bot
                log.error("cycle failed: %s", exc, exc_info=True)
                await self.notify.warn("Cycle failed", str(exc), dedupe_key="cyclefail")

            if (utc_now() - last_roster_refresh).total_seconds() > (
                self.config.discovery.refresh_minutes * 60
            ):
                last_roster_refresh = utc_now()
                try:
                    await self.load_roster(force_refresh=True)
                    await self.ws.resubscribe(list(self.leaders))
                except Exception as exc:  # noqa: BLE001
                    log.error("roster refresh failed: %s", exc)

            # Wake early if a leader trades, otherwise sleep out the interval.
            try:
                await asyncio.wait_for(
                    self._reconcile_now.wait(), timeout=self.config.copy.reconcile_seconds
                )
            except asyncio.TimeoutError:
                pass
            self._reconcile_now.clear()

    async def stop(self) -> None:
        self._running = False
        await self.ws.stop()
        await self.notify.info(
            "Copy engine stopped",
            fields={
                "cycles": str(self.stats.cycles),
                "orders filled": str(self.stats.orders_filled),
                "equity": usd(self.stats.last_equity),
            },
        )


def _order_event(adjustment, order, result, contributors: str, severity):
    from ..notify.dispatcher import Event

    return Event(
        title=f"{result.describe()}",
        body=f"{adjustment.kind} {adjustment.coin}: "
             f"{usd(adjustment.current_notional)} -> {usd(adjustment.target_notional)}",
        severity=severity,
        fields={"mirroring": contributors} if contributors else {},
    )
