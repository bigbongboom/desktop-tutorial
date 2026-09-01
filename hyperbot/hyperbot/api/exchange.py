"""Signed order placement, with the dry-run gate in front of every write.

The Hyperliquid SDK is synchronous, so calls are pushed to a thread. The SDK is
imported lazily: scanning, ranking and watching all work with no key and no SDK
installed at all.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..log import get_logger
from ..util import round_price, round_size, to_float
from .info import AssetMeta

log = get_logger("api.exchange")


@dataclass
class OrderRequest:
    coin: str
    is_buy: bool
    size: float  # absolute, already rounded to szDecimals
    limit_price: float  # already rounded to tick rules
    reduce_only: bool = False
    reason: str = ""

    @property
    def side(self) -> str:
        return "BUY" if self.is_buy else "SELL"

    @property
    def notional(self) -> float:
        return self.size * self.limit_price

    def describe(self) -> str:
        flags = " reduce-only" if self.reduce_only else ""
        return (
            f"{self.side} {self.size:g} {self.coin} @ {self.limit_price:g} "
            f"(${self.notional:,.0f}){flags}"
        )


@dataclass
class OrderResult:
    ok: bool
    request: OrderRequest
    filled_size: float = 0.0
    avg_price: float = 0.0
    order_id: int = 0
    resting: bool = False
    error: str = ""
    dry_run: bool = False

    def describe(self) -> str:
        if self.dry_run:
            return f"[DRY-RUN] would {self.request.describe()}"
        if not self.ok:
            return f"REJECTED {self.request.describe()} - {self.error}"
        if self.filled_size:
            return (
                f"FILLED {self.request.side} {self.filled_size:g} {self.request.coin} "
                f"@ {self.avg_price:g}"
            )
        if self.resting:
            return f"RESTING {self.request.describe()} oid={self.order_id}"
        return f"NO FILL {self.request.describe()} (IOC expired inside slippage guard)"


class ExchangeClient:
    """Wraps the SDK's Exchange. In dry-run it is a faithful simulator that still
    does all the rounding and validation, so a live run has no new failure modes."""

    def __init__(self, config: Config):
        self.config = config
        self._exchange: Any = None
        self._leverage_set: dict[str, float] = {}
        self._init_error: str = ""

    @property
    def live(self) -> bool:
        return self.config.can_trade_live and self._exchange is not None

    def connect(self) -> bool:
        """Build the signer. Returns False (with a logged reason) when not armed."""
        blockers = self.config.live_blockers()
        if blockers:
            log.warning("LIVE TRADING DISABLED: %s", "; ".join(blockers))
            log.warning("running as a simulator - orders will be logged, not sent")
            return False
        try:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
        except ImportError as exc:
            self._init_error = f"hyperliquid-python-sdk not installed: {exc}"
            log.error(self._init_error)
            return False
        try:
            wallet = Account.from_key(self.config.private_key)
            self._exchange = Exchange(
                wallet,
                base_url=self.config.trade_api_url,
                account_address=self.config.account_address or None,
            )
        except Exception as exc:  # noqa: BLE001 - bad key / unreachable API
            self._init_error = f"exchange init failed: {exc}"
            log.error(self._init_error)
            return False
        log.info(
            "LIVE on %s as %s (signer %s)",
            self.config.network,
            self.config.account_address,
            wallet.address,
        )
        return True

    # ---- order construction ---------------------------------------------- #

    def build_order(
        self,
        coin: str,
        delta_notional: float,
        meta: AssetMeta,
        *,
        reduce_only: bool = False,
        reason: str = "",
    ) -> OrderRequest | None:
        """Turn a signed USD delta into a legally-rounded aggressive IOC order.

        Positive delta buys (open/increase long, or close short); negative sells.
        The limit price is set through the mark by `slippage_bps` so the order
        crosses immediately or expires — it never rests at a bad price.
        """
        mark = meta.mark_price
        if mark <= 0 or delta_notional == 0:
            return None
        is_buy = delta_notional > 0
        raw_size = abs(delta_notional) / mark
        size = round_size(raw_size, meta.sz_decimals)
        if size <= 0:
            return None

        guard = 1.0 + (self.config.risk.slippage_bps / 10_000.0) * (1 if is_buy else -1)
        price = round_price(mark * guard, meta.sz_decimals)
        if price <= 0:
            return None
        return OrderRequest(
            coin=coin,
            is_buy=is_buy,
            size=size,
            limit_price=price,
            reduce_only=reduce_only,
            reason=reason,
        )

    # ---- placement -------------------------------------------------------- #

    async def place(self, order: OrderRequest) -> OrderResult:
        if not self.live:
            return OrderResult(ok=True, request=order, dry_run=True)
        try:
            payload = await asyncio.to_thread(
                self._exchange.order,
                order.coin,
                order.is_buy,
                order.size,
                order.limit_price,
                {"limit": {"tif": "Ioc"}},
                order.reduce_only,
            )
        except Exception as exc:  # noqa: BLE001 - network/signing failure
            return OrderResult(ok=False, request=order, error=str(exc))
        return _parse_order_response(payload, order)

    async def set_leverage(self, coin: str, leverage: float) -> bool:
        """Idempotent: only sends when the desired leverage actually changed."""
        target = max(1, int(leverage))
        if self._leverage_set.get(coin) == target:
            return True
        if not self.live:
            self._leverage_set[coin] = target
            return True
        try:
            await asyncio.to_thread(
                self._exchange.update_leverage,
                target,
                coin,
                not self.config.risk.use_isolated_margin,
            )
            self._leverage_set[coin] = target
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("could not set %sx leverage on %s: %s", target, coin, exc)
            return False


def _parse_order_response(payload: Any, order: OrderRequest) -> OrderResult:
    """Hyperliquid nests the real outcome several levels down and reports
    per-order errors inside a 200 response, so 'status: ok' is not success."""
    if not isinstance(payload, dict):
        return OrderResult(ok=False, request=order, error=f"unexpected response: {payload!r}")
    if payload.get("status") != "ok":
        return OrderResult(ok=False, request=order, error=str(payload.get("response", payload)))

    response = payload.get("response", {})
    data = response.get("data", {}) if isinstance(response, dict) else {}
    statuses = data.get("statuses", []) if isinstance(data, dict) else []
    if not statuses:
        return OrderResult(ok=True, request=order)

    status = statuses[0]
    if isinstance(status, dict):
        if "error" in status:
            return OrderResult(ok=False, request=order, error=str(status["error"]))
        if "filled" in status:
            filled = status["filled"]
            return OrderResult(
                ok=True,
                request=order,
                filled_size=to_float(filled.get("totalSz")),
                avg_price=to_float(filled.get("avgPx")),
                order_id=int(filled.get("oid", 0) or 0),
            )
        if "resting" in status:
            return OrderResult(
                ok=True,
                request=order,
                resting=True,
                order_id=int(status["resting"].get("oid", 0) or 0),
            )
    return OrderResult(ok=True, request=order)
