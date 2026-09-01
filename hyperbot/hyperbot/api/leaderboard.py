"""Hyperliquid's public leaderboard: the discovery universe (~44k accounts).

The payload is ~37 MB, so it is cached on disk and only re-fetched when stale.
Each row carries day / week / month / allTime ROI, PnL and volume, which is enough
to cheaply pre-filter before spending one HTTP request per survivor on deep metrics.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..config import LEADERBOARD_URL
from ..log import get_logger
from ..util import retry_async, to_float

log = get_logger("api.leaderboard")

WINDOWS = ("day", "week", "month", "allTime")


@dataclass
class LeaderboardRow:
    address: str
    account_value: float
    display_name: str = ""
    roi: dict[str, float] = field(default_factory=dict)
    pnl: dict[str, float] = field(default_factory=dict)
    volume: dict[str, float] = field(default_factory=dict)

    def get_roi(self, window: str) -> float:
        return self.roi.get(window, 0.0)

    def get_pnl(self, window: str) -> float:
        return self.pnl.get(window, 0.0)

    def get_volume(self, window: str) -> float:
        return self.volume.get(window, 0.0)

    @property
    def label(self) -> str:
        return self.display_name or self.address


class LeaderboardClient:
    def __init__(self, cache_path: str = ".cache/leaderboard.json", ttl_seconds: int = 3600):
        self.cache_path = Path(cache_path)
        self.ttl_seconds = ttl_seconds

    def _cache_is_fresh(self) -> bool:
        if not self.cache_path.exists():
            return False
        return (time.time() - self.cache_path.stat().st_mtime) < self.ttl_seconds

    async def fetch(self, force: bool = False) -> list[LeaderboardRow]:
        if not force and self._cache_is_fresh():
            log.info("using cached leaderboard (%s)", self.cache_path)
            try:
                raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
                return _parse(raw)
            except (json.JSONDecodeError, OSError, KeyError) as exc:
                log.warning("leaderboard cache unreadable (%s) - refetching", exc)

        log.info("downloading leaderboard (~37MB, this takes a moment)")

        async def attempt() -> bytes:
            # Stream: buffering 37MB through the default path is slower and spikier.
            chunks: list[bytes] = []
            async with httpx.AsyncClient(timeout=180.0) as client:
                async with client.stream("GET", LEADERBOARD_URL) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes(1 << 20):
                        chunks.append(chunk)
            return b"".join(chunks)

        body = await retry_async(
            attempt,
            attempts=3,
            base_delay=2.0,
            on_error=lambda n, e: log.warning("leaderboard attempt %d failed: %s", n, e),
        )
        raw = json.loads(body)
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_bytes(body)
        except OSError as exc:
            log.warning("could not cache leaderboard: %s", exc)

        rows = _parse(raw)
        log.info("leaderboard: %d accounts", len(rows))
        return rows


def _parse(raw: dict) -> list[LeaderboardRow]:
    rows: list[LeaderboardRow] = []
    for entry in raw.get("leaderboardRows", []):
        row = LeaderboardRow(
            address=entry.get("ethAddress", ""),
            account_value=to_float(entry.get("accountValue")),
            display_name=entry.get("displayName") or "",
        )
        for window, stats in entry.get("windowPerformances", []):
            row.roi[window] = to_float(stats.get("roi"))
            row.pnl[window] = to_float(stats.get("pnl"))
            row.volume[window] = to_float(stats.get("vlm"))
        if row.address:
            rows.append(row)
    return rows
