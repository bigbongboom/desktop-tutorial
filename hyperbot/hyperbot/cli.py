"""Command line: scan, leaders, watch, run, status, notify-test, close-all."""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys

from .api.exchange import ExchangeClient
from .api.info import InfoClient
from .config import Config, load_config
from .copy.engine import CopyEngine
from .copy.reconciler import reconcile
from .copy.sizing import LeaderView, build_target_book
from .discovery.scanner import TraderScanner, allocations, select_roster
from .log import get_logger, setup_logging
from .notify.dispatcher import build_dispatcher
from .store.db import Store
from .util import pct, short_addr, usd

log = get_logger("cli")

BANNER = r"""
  hyperbot - Hyperliquid copy-trading desk
"""


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def _trader_table(rows: list, kind: str) -> str:
    header = (
        f"{'score':>6} {'trader':<24} {'perp cap':>13} {'roi/mo':>9} {'maxDD':>7} "
        f"{'R2':>5} {'cons':>6} {'conc':>6} {'pace':>6} {'days':>5}"
    )
    lines = [header, "-" * len(header)]
    for entry in rows:
        metrics = entry.metrics
        month = metrics.window("month")
        score = entry.rising.total if kind == "rising" else entry.elite.total
        lines.append(
            f"{score:>6.1f} {metrics.label[:24]:<24} {usd(metrics.perp_capital):>13} "
            f"{month.roi:>8.1%} {month.max_drawdown:>7.1%} {month.r_squared:>5.2f} "
            f"{month.consistency:>6.0%} {month.concentration:>6.0%} "
            f"{metrics.pace_ratio:>5.2f}x {metrics.days_active:>5.0f}"
        )
    return "\n".join(lines)


def _explain(entry, kind: str) -> str:
    breakdown = entry.rising if kind == "rising" else entry.elite
    return f"    {breakdown.explain()}"


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


async def cmd_scan(config: Config, args: argparse.Namespace) -> int:
    store = Store(config.db_path)
    async with InfoClient(config.public_api_url, concurrency=config.discovery.concurrency) as info:
        scanner = TraderScanner(info, config.discovery)
        result = await scanner.scan(force_refresh=args.refresh)

    print(f"\n{result.summary()}\n")
    print("=" * 100)
    print("TOP TRADERS - proven, durable, survivable")
    print("=" * 100)
    print(_trader_table(result.elite[: args.limit], "elite"))
    if args.explain:
        for entry in result.elite[: args.limit]:
            print(_explain(entry, "elite"))

    print()
    print("=" * 100)
    print("ON THE COME UP - small, accelerating, consistent")
    print("=" * 100)
    if result.rising:
        print(_trader_table(result.rising[: args.limit], "rising"))
        if args.explain:
            for entry in result.rising[: args.limit]:
                print(_explain(entry, "rising"))
    else:
        print("  no accounts cleared the climber filters this scan")

    for entry in result.elite + result.rising:
        store.save_trader(entry)

    roster = select_roster(
        result,
        mode=config.copy.roster,
        max_leaders=config.copy.max_leaders,
        rising_slots=config.copy.rising_slots,
    )
    weights = allocations(roster, mode=config.copy.allocation)
    print(f"\nROSTER ({config.copy.roster}, {config.copy.allocation}-weighted)")
    for entry in roster:
        print(
            f"  {weights.get(entry.address, 0):>6.1%}  {entry.label:<24} "
            f"elite={entry.elite.total:>5.1f}  rising={entry.rising.total:>5.1f}"
        )
    store.replace_roster(
        [
            (
                entry.address,
                entry.label,
                weights.get(entry.address, 0.0),
                "rising" if entry.rising.total >= entry.elite.total else "elite",
            )
            for entry in roster
        ]
    )
    store.close()
    return 0


async def cmd_leaders(config: Config, _: argparse.Namespace) -> int:
    store = Store(config.db_path)
    rows = store.load_roster()
    if not rows:
        print("no roster stored yet - run `hyperbot scan` first")
        return 1
    print(f"{'alloc':>7}  {'source':<7}  {'trader':<24}  address")
    for row in rows:
        print(
            f"{row['allocation']:>6.1%}  {row['source']:<7}  {row['label'][:24]:<24}  "
            f"{row['address']}"
        )
    store.close()
    return 0


async def cmd_status(config: Config, _: argparse.Namespace) -> int:
    if not config.account_address:
        print("no account address configured (set HYPERLIQUID_ACCOUNT_ADDRESS)")
        return 1
    async with InfoClient(config.public_api_url) as info:
        state = await info.account_state(config.account_address)
        meta = await info.asset_meta()

    print(f"\naccount   {config.account_address}")
    print(f"network   {config.network} (public reads from mainnet)")
    print(f"equity    {usd(state.account_value)}")
    print(f"gross     {usd(state.total_notional)}  ({state.leverage:.2f}x)")
    print(f"margin    {usd(state.total_margin_used)}   withdrawable {usd(state.withdrawable)}")
    blockers = config.live_blockers()
    print(f"trading   {'DRY RUN - ' + '; '.join(blockers) if blockers else 'LIVE'}")

    if not state.positions:
        print("\nno open positions")
        return 0
    print(f"\n{'coin':<8} {'side':<6} {'size':>14} {'entry':>12} {'mark':>12} "
          f"{'notional':>13} {'uPnL':>13} {'lev':>5}")
    for coin, position in sorted(state.positions.items()):
        mark = meta[coin].mark_price if coin in meta else 0.0
        print(
            f"{coin:<8} {'LONG' if position.is_long else 'SHORT':<6} {position.size:>14g} "
            f"{position.entry_price:>12g} {mark:>12g} {usd(position.position_value):>13} "
            f"{usd(position.unrealized_pnl):>13} {position.leverage:>4g}x"
        )
    return 0


async def cmd_watch(config: Config, args: argparse.Namespace) -> int:
    """Notifications only: track the roster and report, never trade."""
    store = Store(config.db_path)
    dispatcher = build_dispatcher(config.notify)
    rows = store.load_roster()
    if not rows:
        print("no roster stored - run `hyperbot scan` first")
        return 1

    from .api.ws import WSManager

    labels = {row["address"].lower(): row["label"] for row in rows}
    ws = WSManager(config.ws_url)

    async def on_fill(address: str, fill) -> None:
        label = labels.get(address.lower(), short_addr(address))
        await dispatcher.trade(
            f"{label} {fill.direction or ('BUY' if fill.is_buy else 'SELL')} {fill.coin}",
            f"{fill.size:g} {fill.coin} @ {fill.price:g} ({usd(fill.notional)})",
            fields={"closed pnl": usd(fill.closed_pnl) if fill.closed_pnl else "-"},
        )

    ws.on_fill(on_fill)
    ws.follow(list(labels))
    await ws.start()
    await dispatcher.info(
        "Watching leaders", f"{len(labels)} traders, no orders will be placed",
        fields={"leaders": ", ".join(labels.values())},
    )
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await ws.stop()
        await dispatcher.close()
        store.close()
    return 0


async def cmd_run(config: Config, args: argparse.Namespace) -> int:
    if not config.account_address:
        print("no account address configured (set HYPERLIQUID_ACCOUNT_ADDRESS)")
        return 1

    store = Store(config.db_path)
    dispatcher = build_dispatcher(config.notify)
    exchange = ExchangeClient(config)
    exchange.connect()

    async with InfoClient(config.public_api_url, concurrency=config.discovery.concurrency) as info:
        engine = CopyEngine(config, info, exchange, dispatcher, store)
        stop = asyncio.Event()

        def request_stop(*_: object) -> None:
            log.info("shutdown requested")
            stop.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, request_stop)
            except NotImplementedError:  # Windows
                signal.signal(sig, request_stop)

        task = asyncio.create_task(engine.start())
        await stop.wait()
        await engine.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    await dispatcher.close()
    store.close()
    return 0


async def cmd_serve(config: Config, args: argparse.Namespace) -> int:
    """Run the local web dashboard (and the engine, unless --no-engine)."""
    try:
        from .web.server import serve
    except ImportError as exc:
        print(f"the dashboard needs aiohttp: pip install aiohttp  ({exc})")
        return 1

    stop = asyncio.Event()

    def request_stop(*_: object) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:  # Windows
            signal.signal(sig, request_stop)

    task = asyncio.create_task(
        serve(
            config,
            args.host,
            args.port,
            run_engine=not args.no_engine,
            open_browser=not args.no_browser,
        )
    )
    done, _ = await asyncio.wait(
        {task, asyncio.create_task(stop.wait())}, return_when=asyncio.FIRST_COMPLETED
    )
    if task in done:
        return 0 if not task.exception() else 1
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    log.info("dashboard stopped")
    return 0


async def cmd_research(config: Config, args: argparse.Namespace) -> int:
    """Deep-dive one account, or every account on the stored roster."""
    from .research.analyst import Analyst

    store = Store(config.db_path)
    if args.address:
        targets = [args.address]
    else:
        targets = [row["address"] for row in store.load_roster()]
        if not targets:
            print("no roster stored - run `hyperbot scan` first, or pass an address")
            return 1

    async with InfoClient(config.public_api_url, concurrency=8) as info:
        dossiers = await Analyst(info).study_many(targets, concurrency=4)

    for dossier in dossiers:
        store.save_dossier(dossier.as_dict())
        profile = dossier.profile
        assert profile
        print("\n" + "=" * 78)
        print(f"{dossier.name}   {dossier.address}")
        print(f"  {dossier.description}")
        pf = "n/a" if profile.profit_factor is None else f"{profile.profit_factor:.2f}"
        print(f"\n  exits {profile.orders} | win {profile.win_rate:.0%} "
              f"[{profile.sample_quality}] | PF {pf} | realised {usd(profile.total_pnl)}")
        print(f"  long  {profile.long.trades:>4} exits  win {profile.long.win_rate:>5.0%}  "
              f"{usd(profile.long.pnl)}")
        print(f"  short {profile.short.trades:>4} exits  win {profile.short.win_rate:>5.0%}  "
              f"{usd(profile.short.pnl)}")
        if dossier.archetype:
            print(f"\n  style: {dossier.archetype.name} "
                  f"[{dossier.archetype.confidence} confidence]")
            print(f"    {dossier.archetype.summary}")
            for line in dossier.archetype.evidence:
                print(f"      - {line}")
        for test in dossier.backtests:
            factor = "n/a" if test.profit_factor is None else f"{test.profit_factor:.2f}"
            print(f"\n  backtest of OUR rule ({test.coin} {test.interval}): {test.rule}")
            print(f"    {test.trades} trades | win {test.win_rate:.0%} | PF {factor} | "
                  f"return {test.total_return:+.1%} | max DD {test.max_drawdown:.1%}")
        for note in dossier.notes:
            print(f"  note: {note}")
    store.close()
    return 0


async def cmd_signals(config: Config, args: argparse.Namespace) -> int:
    """Where the researched accounts are positioned, and what they changed."""
    import json as _json

    from .research.analyst import gather_consensus
    from .research.consensus import report_to_dict
    from .research.profile import SideStats, TraderProfile

    store = Store(config.db_path)
    rows = store.load_dossiers(args.accounts)
    if not rows:
        print("nothing researched yet - run `hyperbot research` first")
        return 1

    def rebuild(payload: dict) -> TraderProfile:
        """Enough of a profile to weight this account's opinion per side."""
        stats, sides = payload.get("stats", {}), payload.get("sides", {})
        profile = TraderProfile(address=payload["address"])
        profile.trades = stats.get("orders", 0)
        profile.wins = round(stats.get("win_rate", 0.0) * profile.trades)
        profile.losses = profile.trades - profile.wins
        profile.total_pnl = stats.get("total_pnl", 0.0)
        profile.unrealized_pnl = stats.get("unrealized_pnl", 0.0)
        for label in ("long", "short"):
            raw = sides.get(label, {})
            side = SideStats(label)
            side.trades = raw.get("trades", 0)
            side.wins = round(raw.get("win_rate", 0.0) * side.trades)
            side.pnl = raw.get("pnl", 0.0)
            side.volume = raw.get("volume", 0.0)
            factor = raw.get("profit_factor")
            if factor:
                side.gross_win = abs(side.pnl) * 2
                side.gross_loss = -abs(side.pnl) * 2 / max(factor, 0.01)
            elif side.pnl > 0:
                side.gross_win = side.pnl
            setattr(profile, label, side)
        return profile

    accounts = [(r["address"], r.get("name", r["address"]), rebuild(r)) for r in rows]
    async with InfoClient(config.public_api_url, concurrency=8) as info:
        report = await gather_consensus(info, accounts)
    store.save_consensus(report_to_dict(report))

    print(f"\n{report.accounts_with_positions} of {report.accounts_considered} researched "
          f"accounts hold positions.")
    if report.has_flow:
        print(f"Flow measured over the last {report.flow_window_hours}h "
              f"({report.accounts_active_in_window} accounts traded in it).")
    else:
        print("Too few accounts traded recently to read flow - positions only.")
    if report.cohort_warning:
        print(f"\n  WARNING: {report.cohort_warning}")

    print(f"\n{'score':>6} {'coin':<12} {'side':<6} {'held':>5} {'agree':>6} "
          f"{'record':>7} {'avg size':>9} {'flow':>14}")
    print("-" * 74)
    for entry in report.top(args.limit):
        print(f"{entry.score:>6.0f} {entry.coin.replace('xyz:', '')[:12]:<12} "
              f"{entry.side:<6} {entry.holders:>5} {entry.agreement:>6.0%} "
              f"{entry.quality:>7.2f} {entry.conviction:>8.0%} "
              f"{usd(entry.flow_usd) if entry.flow_usd else '-':>14}")

    for entry in report.top(min(args.limit, 5)):
        print(f"\n{entry.coin.replace('xyz:', '')}: {entry.rationale()}")
        for stance in entry.participants[:4]:
            print(f"   {'LONG ' if stance.is_long else 'SHORT'} "
                  f"{stance.position_fraction:>7.0%} of equity  "
                  f"record {stance.quality:.2f} "
                  f"({stance.side_win_rate:.0%} on {stance.side_trades} exits)  "
                  f"{stance.name[:44]}")

    print("\nThis is where measured traders have their money - not a forecast, not advice.")
    if args.json:
        print(_json.dumps(report_to_dict(report), indent=2))
    store.close()
    return 0


async def cmd_snapshot(config: Config, args: argparse.Namespace) -> int:
    """Export the dashboard as one self-contained HTML file (no server needed)."""
    from .web.server import DashboardServer
    from .web.snapshot import write

    server = DashboardServer(config, run_engine=False)
    async with InfoClient(
        config.public_api_url, concurrency=config.discovery.concurrency
    ) as info:
        server.info = info
        snapshot = await server.snapshot()
    server.store.close()

    out = write(snapshot, args.output)
    print(f"\nwrote {out}  ({out.stat().st_size / 1024:.0f} KB)")
    print("open it directly in a browser - it needs no server and places no orders")
    return 0


async def cmd_notify_test(config: Config, _: argparse.Namespace) -> int:
    dispatcher = build_dispatcher(config.notify)
    if not dispatcher.channels:
        print("no notification channels are enabled")
        return 1
    await dispatcher.info(
        "hyperbot test notification",
        "If you can read this, notifications are wired up correctly.",
        fields={"channels": ", ".join(c.name for c in dispatcher.channels)},
    )
    await dispatcher.close()
    print(f"sent via: {', '.join(c.name for c in dispatcher.channels)}")
    return 0


async def cmd_close_all(config: Config, args: argparse.Namespace) -> int:
    if not config.account_address:
        print("no account address configured")
        return 1
    store = Store(config.db_path)
    dispatcher = build_dispatcher(config.notify)
    exchange = ExchangeClient(config)
    exchange.connect()

    async with InfoClient(config.public_api_url) as info:
        state = await info.account_state(config.account_address)
        meta = await info.asset_meta(refresh=True)
        if not state.positions:
            print("no open positions")
            return 0
        print(f"about to close {len(state.positions)} positions: {', '.join(state.positions)}")
        if not args.yes:
            if input("type 'close' to confirm: ").strip().lower() != "close":
                print("aborted")
                return 1
        engine = CopyEngine(config, info, exchange, dispatcher, store)
        await engine.flatten(state, meta, reason="operator requested close-all")

    await dispatcher.close()
    store.close()
    return 0


async def cmd_preview(config: Config, args: argparse.Namespace) -> int:
    """Show the target book and the orders that would be sent. Never trades."""
    store = Store(config.db_path)
    rows = store.load_roster()
    if not rows:
        print("no roster stored - run `hyperbot scan` first")
        return 1
    if not config.account_address:
        print("no account address configured (set HYPERLIQUID_ACCOUNT_ADDRESS)")
        return 1

    async with InfoClient(config.public_api_url) as info:
        account = await info.account_state(config.account_address)
        meta = await info.asset_meta(refresh=True)
        states = await info.account_states([row["address"] for row in rows])

    labels = {row["address"].lower(): row["label"] for row in rows}
    weights = {row["address"].lower(): row["allocation"] for row in rows}
    views = {
        address.lower(): LeaderView.from_state(state, labels.get(address.lower(), address))
        for address, state in states.items()
    }
    book = build_target_book(views, weights, account.account_value, config.copy, config.risk)

    print(f"\nour equity {usd(account.account_value)}   "
          f"target gross {usd(book.gross)} ({book.gross / max(account.account_value, 1):.2f}x)"
          f"   net {usd(book.net)}")
    if book.gross_scale < 1:
        print(f"  (book scaled to {book.gross_scale:.0%} by the gross exposure cap)")
    print(f"\n{'coin':<8} {'side':<6} {'weight':>8} {'target':>13} {'current':>13}  mirroring")
    for coin, position in sorted(book.positions.items(), key=lambda kv: -abs(kv[1].notional)):
        print(
            f"{coin:<8} {position.direction:<6} {position.weight:>7.2%} "
            f"{usd(position.notional):>13} {usd(account.signed_notional(coin)):>13}  "
            + ", ".join(f"{k} {v:+.1%}" for k, v in position.contributors.items())
        )

    adjustments = reconcile(
        account, {c: p.notional for c, p in book.positions.items()},
        meta, config.copy, config.risk,
    )
    print(f"\n{len(adjustments)} orders would be placed:")
    for adjustment in adjustments:
        print(f"  {adjustment.describe()}")
    if not adjustments:
        print("  (none - already inside the deadband)")
    store.close()
    return 0


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hyperbot", description="Hyperliquid copy-trading desk"
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="discover and rank traders")
    scan.add_argument("--limit", type=int, default=15, help="rows to display per roster")
    scan.add_argument("--refresh", action="store_true", help="force leaderboard re-download")
    scan.add_argument("--explain", action="store_true", help="show score components")
    scan.set_defaults(handler=cmd_scan)

    leaders = subparsers.add_parser("leaders", help="show the stored roster")
    leaders.set_defaults(handler=cmd_leaders)

    status = subparsers.add_parser("status", help="account, positions and P&L")
    status.set_defaults(handler=cmd_status)

    preview = subparsers.add_parser("preview", help="show the target book and pending orders")
    preview.set_defaults(handler=cmd_preview)

    watch = subparsers.add_parser("watch", help="stream leader trades, place no orders")
    watch.set_defaults(handler=cmd_watch)

    run = subparsers.add_parser("run", help="run the copy engine")
    run.set_defaults(handler=cmd_run)

    serve = subparsers.add_parser("serve", help="run the web dashboard on localhost")
    serve.add_argument("--port", type=int, default=8730, help="port (default 8730)")
    serve.add_argument(
        "--host", default="127.0.0.1",
        help="bind address. Defaults to localhost on purpose: the UI can place orders.",
    )
    serve.add_argument(
        "--no-engine", action="store_true",
        help="dashboard only - do not run the copy engine",
    )
    serve.add_argument(
        "--no-browser", action="store_true", help="do not open a browser window",
    )
    serve.set_defaults(handler=cmd_serve)

    research = subparsers.add_parser(
        "research", help="deep-dive traders: win rate, sides, entry style, backtest"
    )
    research.add_argument(
        "address", nargs="?", help="one address (default: the stored roster)"
    )
    research.set_defaults(handler=cmd_research)

    signals = subparsers.add_parser(
        "signals", help="where the researched accounts are positioned right now"
    )
    signals.add_argument("--limit", type=int, default=10, help="coins to show")
    signals.add_argument("--accounts", type=int, default=40, help="accounts to include")
    signals.add_argument("--json", action="store_true", help="also dump raw JSON")
    signals.set_defaults(handler=cmd_signals)

    snapshot = subparsers.add_parser(
        "snapshot", help="export the dashboard as a standalone HTML file"
    )
    snapshot.add_argument(
        "-o", "--output", default="desk-snapshot.html", help="output path"
    )
    snapshot.set_defaults(handler=cmd_snapshot)

    notify = subparsers.add_parser("notify-test", help="send a test notification")
    notify.set_defaults(handler=cmd_notify_test)

    close = subparsers.add_parser("close-all", help="flatten every position (reduce-only)")
    close.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    close.set_defaults(handler=cmd_close_all)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config, warnings = load_config(args.config)
    setup_logging("DEBUG" if args.verbose else config.log_level, config.log_file)
    for warning in warnings:
        log.warning(warning)

    if args.command in ("run", "close-all", "serve") and config.can_trade_live:
        log.warning("=" * 68)
        log.warning("LIVE TRADING IS ARMED on %s - real money is at risk", config.network)
        log.warning("=" * 68)

    try:
        return asyncio.run(args.handler(config, args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
