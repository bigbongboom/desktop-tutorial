"""WebSocket manager: leader fills and mark prices, with auto-reconnect.

The stream is a LATENCY optimisation, never a source of truth. If it drops, the
engine still reconciles from clearinghouseState on its timer; it just reacts a bit
later. Nothing in the copy path assumes a fill was seen.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

import websockets

from ..log import get_logger
from .info import Fill, _parse_fill

log = get_logger("api.ws")

FillHandler = Callable[[str, Fill], Awaitable[None]]
MidHandler = Callable[[dict[str, float]], Awaitable[None]]
StatusHandler = Callable[[str, str], Awaitable[None]]


class WSManager:
    def __init__(self, url: str):
        self.url = url
        self._addresses: set[str] = set()
        self._fill_handlers: list[FillHandler] = []
        self._mid_handlers: list[MidHandler] = []
        self._status_handlers: list[StatusHandler] = []
        self._task: asyncio.Task | None = None
        self._connection: Any = None
        self._running = False
        self._subscribe_mids = True
        # Hyperliquid replays recent fills on (re)subscribe; without this we would
        # re-trigger on history every reconnect.
        self._seen_fills: set[str] = set()
        self._seen_order: list[str] = []

    # ---- registration ----------------------------------------------------- #

    def on_fill(self, handler: FillHandler) -> None:
        self._fill_handlers.append(handler)

    def on_mids(self, handler: MidHandler) -> None:
        self._mid_handlers.append(handler)

    def on_status(self, handler: StatusHandler) -> None:
        self._status_handlers.append(handler)

    def follow(self, addresses: list[str]) -> None:
        self._addresses = {a.lower() for a in addresses}

    # ---- lifecycle -------------------------------------------------------- #

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="hyperbot-ws")

    async def stop(self) -> None:
        self._running = False
        if self._connection:
            try:
                await self._connection.close()
            except Exception:  # noqa: BLE001 - closing a dead socket is fine
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    self.url, ping_interval=30, ping_timeout=20, max_size=8 << 20
                ) as connection:
                    self._connection = connection
                    await self._subscribe(connection)
                    log.info("ws connected, following %d leaders", len(self._addresses))
                    await self._emit_status("connected", f"{len(self._addresses)} leaders")
                    backoff = 1.0
                    async for raw in connection:
                        await self._dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any transport failure reconnects
                if not self._running:
                    return
                log.warning("ws disconnected (%s), retrying in %.0fs", exc, backoff)
                await self._emit_status("disconnected", str(exc))
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)
            finally:
                self._connection = None

    async def _subscribe(self, connection: Any) -> None:
        subscriptions: list[dict[str, Any]] = []
        if self._subscribe_mids:
            subscriptions.append({"type": "allMids"})
        for address in self._addresses:
            subscriptions.append({"type": "userFills", "user": address})
        for subscription in subscriptions:
            await connection.send(
                json.dumps({"method": "subscribe", "subscription": subscription})
            )

    async def resubscribe(self, addresses: list[str]) -> None:
        """Roster changed: reconnect so the new leader set is live immediately."""
        self.follow(addresses)
        if self._connection:
            try:
                await self._connection.close()  # the run loop reconnects and resubscribes
            except Exception:  # noqa: BLE001
                pass

    # ---- dispatch --------------------------------------------------------- #

    async def _dispatch(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        channel = message.get("channel")
        data = message.get("data")

        if channel == "allMids" and isinstance(data, dict):
            mids = {c: float(p) for c, p in (data.get("mids") or {}).items()}
            if mids:
                for handler in self._mid_handlers:
                    await _safely(handler(mids))

        elif channel == "userFills" and isinstance(data, dict):
            if data.get("isSnapshot"):
                # Snapshot on connect is history; record it as seen, don't act on it.
                for entry in data.get("fills", []):
                    self._mark_seen(_fill_key(entry))
                return
            address = (data.get("user") or "").lower()
            for entry in data.get("fills", []):
                key = _fill_key(entry)
                if self._mark_seen(key):
                    continue
                fill = _parse_fill(entry)
                for handler in self._fill_handlers:
                    await _safely(handler(address, fill))

    def _mark_seen(self, key: str) -> bool:
        """Returns True if the key was already seen. Bounded memory."""
        if key in self._seen_fills:
            return True
        self._seen_fills.add(key)
        self._seen_order.append(key)
        if len(self._seen_order) > 20_000:
            for old in self._seen_order[:10_000]:
                self._seen_fills.discard(old)
            del self._seen_order[:10_000]
        return False

    async def _emit_status(self, state: str, detail: str) -> None:
        for handler in self._status_handlers:
            await _safely(handler(state, detail))


def _fill_key(entry: dict[str, Any]) -> str:
    return f"{entry.get('hash', '')}:{entry.get('tid', '')}:{entry.get('oid', '')}"


async def _safely(coro: Awaitable[None]) -> None:
    """A handler blowing up must never kill the socket."""
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.error("ws handler failed: %s", exc, exc_info=True)
