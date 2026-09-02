"""Local web dashboard: a browser view of the desk on http://localhost:8730.

Binds to 127.0.0.1 by default and deliberately. The UI exposes actions that can
move money (rescan, flatten), so it must not be reachable from the network unless
the operator explicitly asks with --host.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from ..api.exchange import ExchangeClient
from ..api.info import InfoClient
from ..config import Config
from ..copy.engine import CopyEngine
from ..copy.reconciler import reconcile
from ..copy.sizing import LeaderView, build_target_book
from ..discovery.scanner import TraderScanner, allocations, select_roster
from ..research.analyst import Analyst, gather_consensus
from ..research.consensus import report_to_dict
from ..log import get_logger
from ..notify.dispatcher import Channel, Dispatcher, Event
from ..store.db import Store
from ..util import now_ms, safe_div, utc_now

log = get_logger("web")

DASHBOARD = Path(__file__).parent / "dashboard.html"


class StoreChannel(Channel):
    """Persists every notification, so the feed survives a restart.

    Without this the event history lives only in the running process: restarting
    the server emptied the feed, and an exported snapshot carried no events at all.
    """

    name = "store"

    def __init__(self, store: Store):
        self.store = store

    async def send(self, event: Event) -> None:
        self.store.record_event(event.severity.name, event.title, event.body)


class WebChannel(Channel):
    """Notification transport that pushes into every connected browser."""

    name = "web"

    def __init__(self, hub: "EventHub"):
        self.hub = hub

    async def send(self, event: Event) -> None:
        await self.hub.publish(
            {
                "type": "event",
                "ts": now_ms(),
                "severity": event.severity.name,
                "title": event.title,
                "body": event.body,
                "fields": event.fields,
            }
        )


@dataclass
class EventHub:
    """Fan-out to browser WebSockets, with a replay buffer for late joiners."""

    sockets: set[web.WebSocketResponse] = field(default_factory=set)
    history: list[dict[str, Any]] = field(default_factory=list)
    limit: int = 200

    async def publish(self, message: dict[str, Any]) -> None:
        if message.get("type") == "event":
            self.history.append(message)
            del self.history[: max(0, len(self.history) - self.limit)]
        dead = []
        for socket in self.sockets:
            try:
                await socket.send_json(message)
            except Exception:  # noqa: BLE001 - a closed tab is not an error
                dead.append(socket)
        for socket in dead:
            self.sockets.discard(socket)


class DashboardServer:
    def __init__(self, config: Config, *, run_engine: bool = True):
        self.config = config
        self.run_engine = run_engine
        self.hub = EventHub()
        self.store = Store(config.db_path)
        self.info: InfoClient | None = None
        self.engine: CopyEngine | None = None
        self._engine_task: asyncio.Task | None = None
        self._scan_task: asyncio.Task | None = None
        self._research_task: asyncio.Task | None = None
        self._scan_status = "idle"
        self._research_status = "idle"
        self._started = utc_now()
        self._seed_history()

    def _seed_history(self) -> None:
        """Show what happened before this process started."""
        try:
            self.hub.history = [
                {
                    "type": "event",
                    "ts": row["ts_ms"],
                    "severity": row["severity"],
                    "title": row["title"],
                    "body": row["body"] or "",
                    "fields": {},
                }
                for row in self.store.recent_events(self.hub.limit)
            ]
        except Exception as exc:  # noqa: BLE001 - an empty feed is not fatal
            log.debug("could not seed event history: %s", exc)

    # ---- snapshot --------------------------------------------------------- #

    async def snapshot(self) -> dict[str, Any]:
        config = self.config
        blockers = config.live_blockers()
        data: dict[str, Any] = {
            "config": {
                "network": config.network,
                "live": config.can_trade_live,
                "blockers": blockers,
                "account": config.account_address,
                "exposure_multiplier": config.copy.exposure_multiplier,
                "max_gross": config.risk.max_gross_exposure,
                "daily_loss_limit": config.risk.daily_loss_limit,
                "roster_mode": config.copy.roster,
                "engine_running": self._engine_task is not None
                and not self._engine_task.done(),
                "scan_status": self._scan_status,
                "research_status": self._research_status,
                "researched": self.store.dossier_count(),
                "started": self._started.isoformat(timespec="seconds"),
            },
            "account": None,
            "positions": [],
            "targets": [],
            "adjustments": [],
            "roster": [],
            "elite": self._traders("elite_score"),
            "rising": self._traders("rising_score"),
            "orders": self._orders(),
            "equity_series": self._equity_series(),
            "events": self.hub.history[-60:],
            "risk": self._risk(),
            "research": self.store.load_dossiers(60),
            "consensus": self.store.load_consensus(),
            "paper": await self._paper_summary(),
            "paper_equity": self.store.paper_equity_series(400),
        }

        if not (self.info and config.account_address):
            return data

        try:
            account = await self.info.account_state(config.account_address)
            meta = await self.info.asset_meta(refresh=True)
        except Exception as exc:  # noqa: BLE001 - the page must still render
            data["error"] = f"could not read account: {exc}"
            return data

        day_start = self._day_start_equity(account.account_value)
        data["account"] = {
            "equity": account.account_value,
            "gross": account.total_notional,
            "margin_used": account.total_margin_used,
            "withdrawable": account.withdrawable,
            "leverage": account.leverage,
            "day_pnl": account.account_value - day_start,
            "day_pnl_pct": safe_div(account.account_value - day_start, day_start, 0.0),
            "unrealized": sum(p.unrealized_pnl for p in account.positions.values()),
        }
        data["positions"] = [
            {
                "coin": coin,
                "side": "LONG" if position.is_long else "SHORT",
                "size": position.size,
                "entry": position.entry_price,
                "mark": meta[coin].mark_price if coin in meta else 0.0,
                "notional": position.signed_notional,
                "upnl": position.unrealized_pnl,
                "leverage": position.leverage,
                "liquidation": position.liquidation_price,
            }
            for coin, position in sorted(
                account.positions.items(), key=lambda kv: -kv[1].position_value
            )
        ]

        roster_rows = self.store.load_roster()
        data["roster"] = [
            {
                "address": row["address"],
                "label": row["label"],
                "allocation": row["allocation"],
                "source": row["source"],
            }
            for row in roster_rows
        ]
        if not roster_rows:
            return data

        try:
            states = await self.info.account_states([row["address"] for row in roster_rows])
        except Exception as exc:  # noqa: BLE001
            data["error"] = f"could not read leaders: {exc}"
            return data

        labels = {row["address"].lower(): row["label"] for row in roster_rows}
        weights = {row["address"].lower(): row["allocation"] for row in roster_rows}
        views = {
            address.lower(): LeaderView.from_state(state, labels.get(address.lower(), address))
            for address, state in states.items()
        }
        book = build_target_book(
            views, weights, account.account_value, self.config.copy, self.config.risk
        )
        data["targets"] = [
            {
                "coin": coin,
                "side": position.direction,
                "weight": position.weight,
                "notional": position.notional,
                "current": account.signed_notional(coin),
                "contributors": position.contributors,
            }
            for coin, position in sorted(
                book.positions.items(), key=lambda kv: -abs(kv[1].notional)
            )
        ]
        data["book"] = {
            "gross": book.gross,
            "net": book.net,
            "gross_scale": book.gross_scale,
            "clamped": book.clamped,
        }
        adjustments = reconcile(
            account,
            {coin: position.notional for coin, position in book.positions.items()},
            meta,
            self.config.copy,
            self.config.risk,
        )
        data["adjustments"] = [
            {
                "coin": adjustment.coin,
                "kind": adjustment.kind,
                "current": adjustment.current_notional,
                "target": adjustment.target_notional,
                "delta": adjustment.delta_notional,
            }
            for adjustment in adjustments
        ]
        return data

    async def _paper_summary(self) -> dict[str, Any] | None:
        """Summarise the paper account.

        Reads it from the store when no engine is running, so an exported
        snapshot still carries the test result - the whole point of the run.
        """
        if self.info is None:
            return None
        broker = self.engine.paper if self.engine else None
        if broker is None:
            if not self.config.paper.enabled:
                return None
            saved = self.store.load_paper()
            if not saved:
                return None
            from ..paper.broker import PaperBroker

            broker = PaperBroker.from_dict(
                saved,
                taker_fee_bps=self.config.paper.taker_fee_bps,
                slippage_bps=self.config.paper.slippage_bps,
            )
        try:
            meta = await self.info.asset_meta()
            marks = {coin: asset.mark_price for coin, asset in meta.items()}
        except Exception:  # noqa: BLE001 - fall back to entry prices
            marks = {}
        return broker.summary(marks)

    def _risk(self) -> dict[str, Any]:
        if not self.engine:
            return {"kill_switch": False, "halted": False, "reason": ""}
        state = self.engine.risk.state
        return {
            "kill_switch": state.kill_switch,
            "halted": state.halted,
            "reason": state.kill_reason or state.halt_reason,
            "day": state.day,
            "day_start_equity": state.day_start_equity,
            "high_water_mark": state.high_water_mark,
        }

    def _traders(self, column: str, limit: int = 12) -> list[dict[str, Any]]:
        return [
            {
                "address": row["address"],
                "label": row["label"],
                "elite": row["elite_score"],
                "rising": row["rising_score"],
                "perp_capital": row["perp_capital"],
                "roi_month": row["roi_month"],
                "max_drawdown": row["max_drawdown"],
                "consistency": row["consistency"],
                "r_squared": row["r_squared"],
                "pace_ratio": row["pace_ratio"],
                "days_active": row["days_active"],
            }
            for row in self.store.top_traders(column, limit)
        ]

    def _orders(self, limit: int = 25) -> list[dict[str, Any]]:
        return [
            {
                "ts": row["ts_ms"],
                "coin": row["coin"],
                "side": row["side"],
                "size": row["size"],
                "price": row["limit_price"],
                "notional": row["notional"],
                "kind": row["kind"],
                "status": row["status"],
                "reduce_only": bool(row["reduce_only"]),
                "error": row["error"] or "",
            }
            for row in self.store.recent_orders(limit)
        ]

    def _equity_series(self, limit: int = 400) -> list[dict[str, Any]]:
        rows = list(
            self.store.connection.execute(
                "SELECT ts_ms, account_value, day_pnl FROM equity ORDER BY ts_ms DESC LIMIT ?",
                (limit,),
            )
        )
        return [
            {"ts": row["ts_ms"], "equity": row["account_value"], "day_pnl": row["day_pnl"]}
            for row in reversed(rows)
        ]

    def _day_start_equity(self, fallback: float) -> float:
        if self.engine and self.engine.risk.state.day_start_equity:
            return self.engine.risk.state.day_start_equity
        row = self.store.connection.execute(
            "SELECT account_value FROM equity WHERE day = date('now') ORDER BY ts_ms LIMIT 1"
        ).fetchone()
        return row["account_value"] if row else fallback

    # ---- actions ---------------------------------------------------------- #

    async def trigger_scan(self) -> dict[str, Any]:
        if self._scan_task and not self._scan_task.done():
            return {"ok": False, "message": "a scan is already running"}

        async def work() -> None:
            self._scan_status = "running"
            await self.hub.publish({"type": "scan", "status": "running"})
            try:
                assert self.info is not None
                scanner = TraderScanner(self.info, self.config.discovery)
                result = await scanner.scan(force_refresh=True)
                for entry in result.elite + result.rising:
                    self.store.save_trader(entry)
                chosen = select_roster(
                    result,
                    mode=self.config.copy.roster,
                    max_leaders=self.config.copy.max_leaders,
                    rising_slots=self.config.copy.rising_slots,
                )
                weights = allocations(chosen, mode=self.config.copy.allocation)
                self.store.replace_roster(
                    [
                        (
                            entry.address,
                            entry.label,
                            weights.get(entry.address, 0.0),
                            "rising" if entry.rising.total >= entry.elite.total else "elite",
                        )
                        for entry in chosen
                    ]
                )
                self._scan_status = "idle"
                await self.hub.publish(
                    {"type": "scan", "status": "done", "summary": result.summary()}
                )
            except Exception as exc:  # noqa: BLE001
                self._scan_status = "failed"
                log.error("scan failed: %s", exc, exc_info=True)
                await self.hub.publish({"type": "scan", "status": "failed", "error": str(exc)})

        self._scan_task = asyncio.create_task(work())
        return {"ok": True, "message": "scan started"}

    async def run_research(self, addresses: list[str] | None = None) -> dict[str, Any]:
        """Study accounts in depth: trades, win rate, entry behaviour, strategy."""
        if self._research_task and not self._research_task.done():
            return {"ok": False, "message": "research already running"}

        async def work() -> None:
            self._research_status = "running"
            await self.hub.publish({"type": "research", "status": "running"})
            try:
                assert self.info is not None
                targets = addresses or self._research_targets()
                if not targets:
                    self._research_status = "idle"
                    await self.hub.publish(
                        {"type": "research", "status": "done", "count": 0}
                    )
                    return
                log.info("researching %d accounts in depth", len(targets))
                dossiers = await Analyst(
                    self.info,
                    capital=self.config.paper.starting_equity,
                    exposure_multiplier=self.config.copy.exposure_multiplier,
                ).study_many(targets, concurrency=4)
                for dossier in dossiers:
                    self.store.save_dossier(dossier.as_dict())

                # Positioning consensus across everyone we just studied, using
                # their measured per-side record to weight each opinion.
                report = await gather_consensus(
                    self.info,
                    [(d.address, d.name, d.profile) for d in dossiers],
                )
                self.store.save_consensus(report_to_dict(report))
                self._reselect_roster(dossiers)
                self._research_status = "idle"
                log.info("research complete: %d dossiers", len(dossiers))
                await self.hub.publish(
                    {"type": "research", "status": "done", "count": len(dossiers)}
                )
            except Exception as exc:  # noqa: BLE001
                self._research_status = "failed"
                log.error("research failed: %s", exc, exc_info=True)
                await self.hub.publish(
                    {"type": "research", "status": "failed", "error": str(exc)}
                )

        self._research_task = asyncio.create_task(work())
        return {"ok": True, "message": "research started"}

    def _reselect_roster(self, dossiers: list) -> None:
        """Rebuild the roster from what research learned.

        Discovery ranks on the equity curve, which counts unrealised gains. For
        copying that is the wrong test: the account with the largest realised
        P&L in one live run had made it across just six closing orders. Once the
        dossiers exist we know who has repeatedly banked money, so the roster is
        rebuilt from copyability rather than from curve shape.
        """
        ranked = sorted(
            (d for d in dossiers if d.copyability and d.copyability.copyable),
            key=lambda d: -d.copyability.score,
        )
        if not ranked:
            log.warning(
                "no account passed the copyability gate - keeping the existing roster"
            )
            return

        chosen = ranked[: self.config.copy.max_leaders]
        total = sum(d.copyability.score for d in chosen) or 1.0
        self.store.replace_roster([
            (
                d.address,
                d.name,
                d.copyability.score / total,
                "copyable",
            )
            for d in chosen
        ])
        log.info(
            "roster rebuilt from copyability: %s",
            ", ".join(f"{d.name} ({d.copyability.score:.0f})" for d in chosen),
        )
        if self.engine is not None:
            self.engine.adopt_roster([
                (d.address, d.name, d.copyability.score / total) for d in chosen
            ])

    def _research_targets(self, limit: int = 24) -> list[str]:
        """The best-scoring accounts we know about, roster first."""
        seen: list[str] = []
        for row in self.store.load_roster():
            if row["address"] not in seen:
                seen.append(row["address"])
        for column in ("elite_score", "rising_score"):
            for row in self.store.top_traders(column, limit):
                if row["address"] not in seen:
                    seen.append(row["address"])
        return seen[:limit]

    async def flatten(self) -> dict[str, Any]:
        if not (self.engine and self.info and self.config.account_address):
            return {"ok": False, "message": "engine not running"}
        account = await self.info.account_state(self.config.account_address)
        meta = await self.info.asset_meta(refresh=True)
        if not account.positions:
            return {"ok": False, "message": "no open positions"}
        await self.engine.flatten(account, meta, reason="operator pressed Flatten in the UI")
        return {"ok": True, "message": f"flattened {len(account.positions)} positions"}


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #


def build_app(server: DashboardServer, dispatcher: Dispatcher) -> web.Application:
    app = web.Application()

    async def index(_: web.Request) -> web.StreamResponse:
        return web.FileResponse(DASHBOARD)

    async def favicon(_: web.Request) -> web.StreamResponse:
        mark = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
            '<rect width="32" height="32" rx="7" fill="#2a78d6"/>'
            '<path d="M8 21l5-7 4 4 7-9" stroke="#fff" stroke-width="3" '
            'fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>'
        )
        return web.Response(text=mark, content_type="image/svg+xml")

    async def api_snapshot(_: web.Request) -> web.StreamResponse:
        return web.json_response(await server.snapshot())

    async def api_scan(_: web.Request) -> web.StreamResponse:
        return web.json_response(await server.trigger_scan())

    async def api_research(_: web.Request) -> web.StreamResponse:
        return web.json_response(await server.run_research())

    async def api_trader(request: web.Request) -> web.StreamResponse:
        address = request.match_info["address"]
        dossier = server.store.load_dossier(address)
        if dossier is None:
            return web.json_response({"error": "not researched yet"}, status=404)
        return web.json_response(dossier)

    async def api_flatten(request: web.Request) -> web.StreamResponse:
        body = await request.json() if request.can_read_body else {}
        if body.get("confirm") != "FLATTEN":
            return web.json_response(
                {"ok": False, "message": "confirmation required"}, status=400
            )
        return web.json_response(await server.flatten())

    async def websocket(request: web.Request) -> web.StreamResponse:
        socket = web.WebSocketResponse(heartbeat=30)
        await socket.prepare(request)
        server.hub.sockets.add(socket)
        try:
            await socket.send_json({"type": "hello", "events": server.hub.history[-60:]})
            async for message in socket:
                if message.type == WSMsgType.ERROR:
                    break
        finally:
            server.hub.sockets.discard(socket)
        return socket

    app.add_routes(
        [
            web.get("/", index),
            web.get("/favicon.ico", favicon),
            web.get("/api/snapshot", api_snapshot),
            web.post("/api/scan", api_scan),
            web.post("/api/research", api_research),
            web.get("/api/trader/{address}", api_trader),
            web.post("/api/flatten", api_flatten),
            web.get("/ws", websocket),
        ]
    )
    app["dispatcher"] = dispatcher
    return app


async def _auto_research(server: "DashboardServer") -> None:
    """Keep the research and the positioning consensus current.

    Runs once as soon as the first scan produces a roster, then repeats on a
    timer. Positions move and accounts trade, so a consensus computed at startup
    goes stale; without this the panel silently ages while the page looks live.
    """
    for _ in range(60):  # wait for the first roster
        await asyncio.sleep(5)
        if server._research_targets():
            break
    else:
        return

    interval = max(300, server.config.discovery.research_refresh_minutes * 60)
    while True:
        try:
            await server.run_research()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed pass must not end the loop
            log.error("scheduled research failed: %s", exc)
        # Wait for the pass to finish before timing the next one.
        while server._research_status == "running":
            await asyncio.sleep(5)
        await asyncio.sleep(interval)


async def serve(
    config: Config,
    host: str,
    port: int,
    *,
    run_engine: bool = True,
    open_browser: bool = True,
) -> None:
    from ..notify.dispatcher import build_dispatcher

    server = DashboardServer(config, run_engine=run_engine)
    dispatcher = build_dispatcher(config.notify)
    dispatcher.add(WebChannel(server.hub))
    dispatcher.add(StoreChannel(server.store))

    async with InfoClient(
        config.public_api_url, concurrency=config.discovery.concurrency
    ) as info:
        server.info = info
        exchange = ExchangeClient(config)
        exchange.connect()

        app = build_app(server, dispatcher)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

        url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"
        log.info("=" * 62)
        log.info("dashboard ready at %s", url)
        if host not in ("127.0.0.1", "localhost"):
            log.warning("bound to %s - this UI can place orders. Do not expose it.", host)
        log.info("=" * 62)

        if open_browser:
            # Best effort: headless boxes and WSL often have no browser to open.
            try:
                import webbrowser

                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass

        if run_engine and config.account_address:
            server.engine = CopyEngine(config, info, exchange, dispatcher, server.store)
            server._engine_task = asyncio.create_task(server.engine.start())
            server._auto_research_task = asyncio.create_task(_auto_research(server))
        elif run_engine:
            log.warning("no account address set - dashboard runs in discovery-only mode")
            await dispatcher.info(
                "Dashboard started (discovery only)",
                "Set HYPERLIQUID_ACCOUNT_ADDRESS to track an account and mirror leaders.",
            )

        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            if server.engine:
                await server.engine.stop()
            if server._engine_task:
                server._engine_task.cancel()
            await runner.cleanup()
            await dispatcher.close()
            server.store.close()
