"""Discovery pipeline: 44k accounts -> a few hundred candidates -> two ranked rosters.

Stage 1 is free (leaderboard rows already in memory). Stage 2 costs two HTTP
requests per survivor, so the funnel matters: `deep_scan_limit` is the real knob
on how long a scan takes.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..api.info import InfoClient
from ..api.leaderboard import LeaderboardClient, LeaderboardRow
from ..config import DiscoveryConfig
from ..log import get_logger
from ..util import safe_div
from .metrics import TraderMetrics, build_trader_metrics
from .scoring import EliteScorer, RisingScorer, ScoreBreakdown

log = get_logger("discovery.scanner")


@dataclass
class ScoredTrader:
    metrics: TraderMetrics
    elite: ScoreBreakdown
    rising: ScoreBreakdown

    @property
    def address(self) -> str:
        return self.metrics.address

    @property
    def label(self) -> str:
        return self.metrics.label


@dataclass
class ScanResult:
    elite: list[ScoredTrader] = field(default_factory=list)
    rising: list[ScoredTrader] = field(default_factory=list)
    scanned: int = 0
    universe: int = 0
    deep_scanned: int = 0

    def summary(self) -> str:
        return (
            f"universe {self.universe:,} -> prefiltered {self.scanned:,} -> "
            f"deep-scanned {self.deep_scanned} -> {len(self.elite)} elite / "
            f"{len(self.rising)} rising"
        )


class TraderScanner:
    def __init__(self, info: InfoClient, config: DiscoveryConfig):
        self.info = info
        self.config = config
        self.leaderboard = LeaderboardClient()
        self.elite_scorer = EliteScorer(config)
        self.rising_scorer = RisingScorer(config)

    # ---- stage 1: cheap ---------------------------------------------------- #

    def prefilter(self, rows: list[LeaderboardRow]) -> list[LeaderboardRow]:
        config = self.config
        kept: list[LeaderboardRow] = []
        for row in rows:
            if not (config.min_account_value <= row.account_value <= config.max_account_value):
                continue
            if row.get_pnl("allTime") < config.min_all_time_pnl:
                continue
            if row.get_volume("month") < config.min_volume:
                continue
            # Must be profitable over the month or be a genuinely strong recent
            # mover; a flat-to-down month is not a candidate either way.
            if row.get_roi("month") <= 0 and row.get_roi("week") <= 0:
                continue
            kept.append(row)
        return kept

    def rank_candidates(self, rows: list[LeaderboardRow]) -> list[LeaderboardRow]:
        """Elite-oriented ordering: proven size and a strong month.

        Blends month ROI, week ROI and a capped PnL credibility term so the list
        is not entirely small accounts with lottery-ticket percentages.
        """

        def key(row: LeaderboardRow) -> float:
            month = row.get_roi("month")
            week = row.get_roi("week")
            credibility = min(1.0, safe_div(row.get_pnl("allTime"), 250_000.0))
            return month * 0.5 + week * 0.3 + credibility * 0.2

        return sorted(rows, key=key, reverse=True)

    def rank_rising_candidates(self, rows: list[LeaderboardRow]) -> list[LeaderboardRow]:
        """Climber-oriented ordering — a SEPARATE funnel, on purpose.

        Ranking every candidate by elite criteria starves the rising roster by
        construction: the deep-scan budget goes entirely to huge established
        accounts, and climber-band accounts are never even fetched. This ranks
        inside the climber band and weights *current* pace over lifetime size.
        """
        config = self.config
        in_band = [
            row
            for row in rows
            if config.rising_min_equity <= row.account_value <= config.rising_max_equity
        ]

        def key(row: LeaderboardRow) -> float:
            week = row.get_roi("week")
            month = row.get_roi("month")
            # Recent pace as a share of established pace - the same idea the
            # rising scorer gates on, computed cheaply for ordering.
            pace = safe_div(week * (30.0 / 7.0), month, 0.0) if month > 0 else 0.0
            return week * 0.45 + month * 0.25 + min(pace, 2.0) * 0.30

        return sorted(in_band, key=key, reverse=True)

    # ---- stage 2: expensive ------------------------------------------------ #

    async def deep_scan_one(self, row: LeaderboardRow) -> TraderMetrics | None:
        try:
            portfolio, state = await asyncio.gather(
                self.info.portfolio(row.address),
                self.info.account_state(row.address),
            )
        except Exception as exc:  # noqa: BLE001 - one bad account must not stop a scan
            log.debug("deep scan failed for %s: %s", row.address, exc)
            return None
        return build_trader_metrics(
            row.address,
            portfolio,
            display_name=row.display_name,
            # Prefer live account value; the leaderboard snapshot can be hours stale.
            account_value=state.account_value or row.account_value,
            leaderboard_roi=row.roi,
            leaderboard_pnl=row.pnl,
            leaderboard_volume=row.volume,
            open_positions=len(state.positions),
        )

    async def scan(self, *, force_refresh: bool = False) -> ScanResult:
        rows = await self.leaderboard.fetch(force=force_refresh)
        result = ScanResult(universe=len(rows))

        survivors = self.prefilter(rows)
        result.scanned = len(survivors)
        log.info("prefilter: %d of %d accounts pass basic floors", len(survivors), len(rows))

        # Split the budget across BOTH funnels, then deduplicate. Without the
        # second funnel the rising roster is starved no matter how its scorer is
        # tuned, because climber-band accounts never reach the deep scan.
        budget = self.config.deep_scan_limit
        rising_budget = int(budget * self.config.rising_scan_share)
        elite_budget = budget - rising_budget

        candidates: list[LeaderboardRow] = []
        seen: set[str] = set()
        for row in self.rank_candidates(survivors)[:elite_budget]:
            candidates.append(row)
            seen.add(row.address)
        for row in self.rank_rising_candidates(survivors):
            if len(candidates) >= budget:
                break
            if row.address not in seen:
                candidates.append(row)
                seen.add(row.address)
        log.info(
            "deep-scanning %d candidates (%d elite-ranked, %d climber-ranked)",
            len(candidates),
            elite_budget,
            len(candidates) - elite_budget,
        )

        semaphore = asyncio.Semaphore(self.config.concurrency)

        async def bounded(row: LeaderboardRow) -> TraderMetrics | None:
            async with semaphore:
                return await self.deep_scan_one(row)

        gathered = await asyncio.gather(*(bounded(row) for row in candidates))
        scanned = [item for item in gathered if item is not None]
        result.deep_scanned = len(scanned)

        elite: list[ScoredTrader] = []
        rising: list[ScoredTrader] = []
        for metrics in scanned:
            elite_score = self.elite_scorer.score(metrics)
            rising_score = self.rising_scorer.score(metrics)
            metrics.elite_score = elite_score.total
            metrics.rising_score = rising_score.total
            entry = ScoredTrader(metrics=metrics, elite=elite_score, rising=rising_score)
            if elite_score.qualified and elite_score.total > 0:
                elite.append(entry)
            if rising_score.qualified and rising_score.total > 0:
                rising.append(entry)

        result.elite = sorted(elite, key=lambda item: item.elite.total, reverse=True)
        result.rising = sorted(rising, key=lambda item: item.rising.total, reverse=True)
        log.info(result.summary())
        return result


def select_roster(
    result: ScanResult,
    *,
    mode: str = "blend",
    max_leaders: int = 5,
    rising_slots: int = 2,
) -> list[ScoredTrader]:
    """Pick the accounts to actually follow.

    'blend' reserves `rising_slots` for climbers and fills the rest from the elite
    list, deduplicating by address so one trader never occupies two slots.
    """
    if mode == "elite":
        return result.elite[:max_leaders]
    if mode == "rising":
        return result.rising[:max_leaders]

    chosen: list[ScoredTrader] = []
    seen: set[str] = set()
    for trader in result.rising[: max(0, min(rising_slots, max_leaders))]:
        chosen.append(trader)
        seen.add(trader.address)
    for trader in result.elite:
        if len(chosen) >= max_leaders:
            break
        if trader.address in seen:
            continue
        chosen.append(trader)
        seen.add(trader.address)
    # If one list came up short, top up from the other rather than under-filling.
    for trader in result.rising:
        if len(chosen) >= max_leaders:
            break
        if trader.address not in seen:
            chosen.append(trader)
            seen.add(trader.address)
    return chosen


def allocations(
    roster: list[ScoredTrader], *, mode: str = "score", rising_slots: int = 2
) -> dict[str, float]:
    """Normalised per-leader weights summing to 1.0."""
    if not roster:
        return {}
    if mode == "equal":
        share = 1.0 / len(roster)
        return {trader.address: share for trader in roster}

    # Use whichever roster each trader actually qualified best on.
    raw = {
        trader.address: max(trader.elite.total, trader.rising.total) or 1.0 for trader in roster
    }
    total = sum(raw.values())
    return {address: value / total for address, value in raw.items()} if total else {}
