"""Small shared helpers: number coercion, Hyperliquid rounding rules, retries, time."""
from __future__ import annotations

import asyncio
import math
import random
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")

# --------------------------------------------------------------------------- #
# numbers
# --------------------------------------------------------------------------- #


def to_float(value: Any, default: float = 0.0) -> float:
    """Hyperliquid returns every number as a string. Coerce, never raise."""
    if value is None:
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(out) or math.isinf(out) else out


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def sign(value: float) -> int:
    return (value > 0) - (value < 0)


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    return numerator / denominator if denominator else default


def pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def usd(value: float) -> str:
    magnitude = abs(value)
    if magnitude >= 1_000_000:
        return f"${value / 1_000_000:,.2f}M"
    if magnitude >= 10_000:
        return f"${value / 1_000:,.1f}k"
    return f"${value:,.2f}"


def short_addr(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}" if len(address) > 12 else address


# --------------------------------------------------------------------------- #
# Hyperliquid tick / lot rules
# --------------------------------------------------------------------------- #
# Perp prices: at most 5 significant figures AND at most (6 - szDecimals) decimal
# places. Integer prices are always legal regardless of significant figures.
# Sizes: exactly szDecimals decimal places. Breaking either rule gets the order
# silently rejected, so this is not a place to improvise.

MAX_PX_SIG_FIGS = 5


def round_price(price: float, sz_decimals: int, is_perp: bool = True) -> float:
    if price <= 0:
        return 0.0
    max_decimals = (6 if is_perp else 8) - sz_decimals
    if price >= 10 ** MAX_PX_SIG_FIGS:
        # Already beyond 5 sig figs before the decimal point: integers are legal.
        return float(round(price))
    with_sig_figs = float(f"{price:.{MAX_PX_SIG_FIGS}g}")
    return float(round(with_sig_figs, max(0, max_decimals)))


def round_size(size: float, sz_decimals: int) -> float:
    return float(round(size, sz_decimals))


def size_is_dust(size: float, sz_decimals: int) -> bool:
    """True when a size rounds away to nothing at the asset's precision."""
    return abs(round_size(size, sz_decimals)) < 10 ** -sz_decimals / 2


# --------------------------------------------------------------------------- #
# time
# --------------------------------------------------------------------------- #


def now_ms() -> int:
    return int(time.time() * 1000)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_day(timestamp_ms: int | None = None) -> str:
    moment = (
        datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        if timestamp_ms
        else utc_now()
    )
    return moment.strftime("%Y-%m-%d")


def ms_to_iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


# --------------------------------------------------------------------------- #
# retry
# --------------------------------------------------------------------------- #


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 20.0,
    on_error: Callable[[int, Exception], None] | None = None,
) -> T:
    """Exponential backoff with jitter. Re-raises the final exception."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - deliberate: retry any transport error
            last = exc
            if on_error:
                on_error(attempt + 1, exc)
            if attempt == attempts - 1:
                break
            delay = min(max_delay, base_delay * 2**attempt)
            await asyncio.sleep(delay * (0.5 + random.random()))
    assert last is not None
    raise last
