"""Async client for Hyperliquid's public /info endpoint.

Every read here is public: any address's positions, equity curve and fills are
visible to anyone. That is what makes copy trading possible on this venue at all.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..log import get_logger
from ..util import retry_async, to_float

log = get_logger("api.info")


@dataclass
class AssetMeta:
    """Everything needed to place a legal order on one perp."""

    name: str
    asset_id: int
    sz_decimals: int
    max_leverage: float
    mark_price: float = 0.0
    funding: float = 0.0
    open_interest: float = 0.0
    is_delisted: bool = False


@dataclass
class Position:
    coin: str
    size: float  # signed: positive long, negative short
    entry_price: float
    position_value: float  # absolute notional
    unrealized_pnl: float
    leverage: float
    liquidation_price: float = 0.0
    margin_used: float = 0.0

    @property
    def is_long(self) -> bool:
        return self.size > 0

    @property
    def signed_notional(self) -> float:
        return self.position_value if self.size > 0 else -self.position_value


@dataclass
class AccountState:
    address: str
    account_value: float = 0.0
    total_notional: float = 0.0
    total_margin_used: float = 0.0
    withdrawable: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    timestamp: int = 0

    @property
    def leverage(self) -> float:
        return self.total_notional / self.account_value if self.account_value else 0.0

    def signed_notional(self, coin: str) -> float:
        position = self.positions.get(coin)
        return position.signed_notional if position else 0.0


@dataclass
class Fill:
    coin: str
    price: float
    size: float
    side: str  # "B" buy / "A" sell
    time: int
    direction: str  # "Open Long", "Close Short", ...
    closed_pnl: float
    fee: float
    hash: str = ""
    oid: int = 0
    # Signed position size BEFORE this fill. Present on every fill, and the only
    # way to know where a position started when the opening fills fall outside
    # the 2000-fill window.
    start_position: float = 0.0

    @property
    def is_buy(self) -> bool:
        return self.side == "B"

    @property
    def notional(self) -> float:
        return self.price * self.size


class InfoClient:
    """Thin async wrapper. One shared httpx client, bounded concurrency, retries."""

    def __init__(self, base_url: str, *, concurrency: int = 8, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(concurrency)
        self._timeout = timeout
        self._meta_cache: dict[str, AssetMeta] = {}

    async def __aenter__(self) -> "InfoClient":
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers={"Content-Type": "application/json"},
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _post(self, payload: dict[str, Any]) -> Any:
        if self._client is None:
            raise RuntimeError("InfoClient used outside its async context manager")

        async def attempt() -> Any:
            async with self._semaphore:
                response = await self._client.post(f"{self.base_url}/info", json=payload)
            response.raise_for_status()
            return response.json()

        return await retry_async(
            attempt,
            attempts=4,
            on_error=lambda n, e: log.warning(
                "info %s attempt %d failed: %s", payload.get("type"), n, e
            ),
        )

    # ---- market metadata -------------------------------------------------- #

    async def asset_meta(self, refresh: bool = False) -> dict[str, AssetMeta]:
        """Perp universe with live mark prices. Cached; call with refresh=True to update marks."""
        if self._meta_cache and not refresh:
            return self._meta_cache
        payload = await self._post({"type": "metaAndAssetCtxs"})
        universe = payload[0]["universe"]
        contexts = payload[1]
        out: dict[str, AssetMeta] = {}
        for index, entry in enumerate(universe):
            context = contexts[index] if index < len(contexts) else {}
            mark = to_float(context.get("markPx")) or to_float(context.get("midPx"))
            out[entry["name"]] = AssetMeta(
                name=entry["name"],
                asset_id=index,
                sz_decimals=int(entry.get("szDecimals", 2)),
                max_leverage=to_float(entry.get("maxLeverage"), 1.0),
                mark_price=mark,
                funding=to_float(context.get("funding")),
                open_interest=to_float(context.get("openInterest")),
                is_delisted=bool(entry.get("isDelisted", False)),
            )
        self._meta_cache = out
        return out

    async def all_mids(self) -> dict[str, float]:
        payload = await self._post({"type": "allMids"})
        return {coin: to_float(price) for coin, price in payload.items()}

    # ---- account state ---------------------------------------------------- #

    async def account_state(self, address: str) -> AccountState:
        payload = await self._post({"type": "clearinghouseState", "user": address})
        summary = payload.get("marginSummary", {})
        state = AccountState(
            address=address,
            account_value=to_float(summary.get("accountValue")),
            total_notional=to_float(summary.get("totalNtlPos")),
            total_margin_used=to_float(summary.get("totalMarginUsed")),
            withdrawable=to_float(payload.get("withdrawable")),
            timestamp=int(payload.get("time", 0)),
        )
        for entry in payload.get("assetPositions", []):
            position = entry.get("position", {})
            size = to_float(position.get("szi"))
            if size == 0:
                continue
            leverage_block = position.get("leverage") or {}
            state.positions[position["coin"]] = Position(
                coin=position["coin"],
                size=size,
                entry_price=to_float(position.get("entryPx")),
                position_value=to_float(position.get("positionValue")),
                unrealized_pnl=to_float(position.get("unrealizedPnl")),
                leverage=to_float(leverage_block.get("value"), 1.0),
                liquidation_price=to_float(position.get("liquidationPx")),
                margin_used=to_float(position.get("marginUsed")),
            )
        return state

    async def account_states(self, addresses: list[str]) -> dict[str, AccountState]:
        """Fetch many accounts concurrently; failures are dropped, not fatal."""
        results = await asyncio.gather(
            *(self.account_state(address) for address in addresses),
            return_exceptions=True,
        )
        out: dict[str, AccountState] = {}
        for address, result in zip(addresses, results):
            if isinstance(result, Exception):
                log.warning("state fetch failed for %s: %s", address, result)
            else:
                out[address] = result
        return out

    async def portfolio(self, address: str) -> dict[str, dict[str, Any]]:
        """Equity-curve history keyed by window: day/week/month/allTime (+perp variants)."""
        payload = await self._post({"type": "portfolio", "user": address})
        return {window: data for window, data in payload}

    async def user_fills(self, address: str) -> list[Fill]:
        payload = await self._post({"type": "userFills", "user": address})
        return [_parse_fill(entry) for entry in payload]

    async def user_fills_by_time(
        self, address: str, start_ms: int, end_ms: int | None = None
    ) -> list[Fill]:
        request: dict[str, Any] = {
            "type": "userFillsByTime",
            "user": address,
            "startTime": start_ms,
        }
        if end_ms:
            request["endTime"] = end_ms
        return [_parse_fill(entry) for entry in await self._post(request)]

    async def candles(
        self, coin: str, interval: str, start_ms: int, end_ms: int
    ) -> list[dict[str, Any]]:
        """OHLCV candles. Intervals: 1m 5m 15m 1h 4h 1d (and others)."""
        return await self._post(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": interval,
                    "startTime": start_ms,
                    "endTime": end_ms,
                },
            }
        )

    async def open_orders(self, address: str) -> list[dict[str, Any]]:
        return await self._post({"type": "openOrders", "user": address})


def _parse_fill(entry: dict[str, Any]) -> Fill:
    return Fill(
        coin=entry.get("coin", ""),
        price=to_float(entry.get("px")),
        size=to_float(entry.get("sz")),
        side=entry.get("side", ""),
        time=int(entry.get("time", 0)),
        direction=entry.get("dir", ""),
        closed_pnl=to_float(entry.get("closedPnl")),
        fee=to_float(entry.get("fee")),
        hash=entry.get("hash", ""),
        oid=int(entry.get("oid", 0) or 0),
        start_position=to_float(entry.get("startPosition")),
    )
