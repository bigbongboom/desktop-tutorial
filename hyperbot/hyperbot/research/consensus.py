"""Where the tracked traders actually stand, and what they changed recently.

WHY THIS IS NOT "TODAY'S TRADES"
--------------------------------
The obvious build is "what did the good traders buy today". Measured against
live accounts, that question usually has no answer: sampling 16 well-scored
accounts, only 3 had traded in the last 24 hours and the median account had not
traded for 4.5 days. At 04:00 UTC a calendar-day window returned zero fills for
every one of them. A signal that silently rests on three accounts - or on none -
is worse than no signal.

So the primary reading is CURRENT POSITIONING, which is live for every account
all of the time: what capital is committed to right now, weighted by how well
each trader has actually done on that side. Recent flow is the secondary reading,
over a window that widens (24h -> 72h -> 7d) until enough accounts are inside it,
and every result carries the window it actually used and how many accounts spoke.

Nothing here forecasts a price. It reports where measured traders have their
money and what they changed, which is a fact about them, not about the future.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..util import clamp, safe_div
from .profile import TraderProfile

# Widen until at least this many accounts have traded inside the window.
FLOW_WINDOWS_HOURS = (24, 72, 168)
MIN_ACCOUNTS_FOR_FLOW = 3


@dataclass
class Stance:
    """One trader's live position in one coin."""

    address: str
    name: str
    is_long: bool
    position_fraction: float  # signed, as a share of THEIR account value
    notional: float           # signed USD
    leverage: float
    unrealized: float
    quality: float            # 0..1, how much their opinion has earned
    side_win_rate: float
    side_trades: int
    flow_usd: float = 0.0     # signed notional traded in the flow window


@dataclass
class CoinConsensus:
    coin: str
    longs: int = 0
    shorts: int = 0
    net_weight: float = 0.0    # quality-weighted signed position fraction
    gross_weight: float = 0.0
    agreement: float = 0.0     # dominant side's share of participants
    conviction: float = 0.0    # mean |position fraction| among holders
    quality: float = 0.0       # mean quality of holders
    flow_usd: float = 0.0      # net notional opened(+) / closed(-) in window
    flow_accounts: int = 0
    score: float = 0.0
    participants: list[Stance] = field(default_factory=list)

    @property
    def side(self) -> str:
        if self.longs and self.shorts:
            if self.longs == self.shorts:
                return "SPLIT"
            return "LONG" if self.longs > self.shorts else "SHORT"
        return "LONG" if self.longs else "SHORT"

    @property
    def holders(self) -> int:
        return self.longs + self.shorts

    @property
    def contested(self) -> bool:
        return self.longs > 0 and self.shorts > 0

    def rationale(self) -> str:
        """Plain English, built only from what was measured."""
        side = self.side
        who = f"{self.holders} of the tracked accounts"
        if self.contested:
            lead = (
                f"{who} hold {self.coin}, but they disagree - "
                f"{self.longs} long against {self.shorts} short"
            )
        else:
            lead = f"{who} are {side.lower()} {self.coin}, none on the other side"
        size = f"averaging {self.conviction:.0%} of their own equity"
        flow = ""
        if self.flow_usd:
            direction = "added to" if self.flow_usd > 0 else "trimmed"
            flow = (
                f"; {self.flow_accounts} {direction} the position "
                f"(${abs(self.flow_usd):,.0f} notional) in the window"
            )
        tail = ""
        if self.holders >= 5 and self.quality < 0.35:
            tail = (
                " Crowded, but the accounts in it have a weak measured record on "
                "this side - breadth here is not evidence."
            )
        return f"{lead}, {size}{flow}.{tail}"


@dataclass
class ConsensusReport:
    coins: list[CoinConsensus] = field(default_factory=list)
    accounts_considered: int = 0
    accounts_with_positions: int = 0
    accounts_active_in_window: int = 0
    flow_window_hours: int = 0
    generated_ms: int = 0
    long_share: float = 0.0     # share of all live positions that are long
    total_stances: int = 0

    @property
    def has_flow(self) -> bool:
        return self.accounts_active_in_window >= MIN_ACCOUNTS_FOR_FLOW

    @property
    def one_sided_cohort(self) -> bool:
        """True when the tracked accounts are nearly all on one side.

        This is a property of WHO IS BEING TRACKED, not of the market. Discovery
        ranks accounts on realised profit, so in a rising market it selects
        long-biased traders, and their 'consensus' is then long almost by
        construction. Without this flag a reader mistakes a selection artefact
        for a signal.
        """
        return self.total_stances >= 6 and (
            self.long_share >= 0.85 or self.long_share <= 0.15
        )

    @property
    def cohort_warning(self) -> str:
        if not self.one_sided_cohort:
            return ""
        side = "long" if self.long_share >= 0.5 else "short"
        return (
            f"{self.long_share:.0%} of every position these accounts hold is {side}. "
            "They were selected for realised profit, so in a trending market the "
            "selection itself leans that way - read agreement here as 'this cohort "
            f"is {side}', not as evidence the market must go that way."
        )

    def top(self, limit: int = 8) -> list[CoinConsensus]:
        return self.coins[:limit]


def quality_weight(profile: TraderProfile | None, is_long: bool) -> float:
    """How much this trader's opinion has earned, on THIS side, 0..1.

    Built from the side they are actually taking: a trader who is 92% on longs
    and 30% on shorts should not lend a short position their long record.
    """
    if profile is None or profile.trades == 0:
        return 0.25
    side = profile.long if is_long else profile.short

    if side.trades >= 5:
        win = clamp(side.win_rate, 0.0, 1.0)
        factor = side.profit_factor
        # An undefined profit factor means no losses yet - promising, not proven.
        pf_score = 0.6 if factor is None else clamp(factor / 3.0, 0.0, 1.0)
        profitable = 1.0 if side.pnl > 0 else 0.25
        sample = clamp(side.trades / 40.0, 0.25, 1.0)
        base = (win * 0.35 + pf_score * 0.30 + profitable * 0.35) * sample
    else:
        # Too few exits on this side: fall back to the overall record, discounted.
        base = clamp(profile.win_rate, 0.0, 1.0) * 0.5

    # An account that closes winners and holds losers has an inflated record.
    if profile.never_takes_losses:
        base *= 0.4
    if profile.sample_quality in ("thin", "very thin"):
        base *= 0.7
    return clamp(base, 0.0, 1.0)


def build_consensus(
    stances_by_coin: dict[str, list[Stance]],
    *,
    accounts_considered: int,
    accounts_with_positions: int,
    accounts_active: int,
    flow_window_hours: int,
) -> ConsensusReport:
    report = ConsensusReport(
        accounts_considered=accounts_considered,
        accounts_with_positions=accounts_with_positions,
        accounts_active_in_window=accounts_active,
        flow_window_hours=flow_window_hours,
        generated_ms=int(time.time() * 1000),
    )

    all_stances = [s for group in stances_by_coin.values() for s in group]
    report.total_stances = len(all_stances)
    report.long_share = safe_div(
        sum(1 for s in all_stances if s.is_long), len(all_stances), 0.0
    )

    for coin, stances in stances_by_coin.items():
        if not stances:
            continue
        entry = CoinConsensus(coin=coin, participants=sorted(
            stances, key=lambda s: -abs(s.notional)
        ))
        for stance in stances:
            if stance.is_long:
                entry.longs += 1
            else:
                entry.shorts += 1
            entry.net_weight += stance.position_fraction * stance.quality
            entry.gross_weight += abs(stance.position_fraction) * stance.quality
            entry.flow_usd += stance.flow_usd
        entry.flow_accounts = sum(1 for s in stances if s.flow_usd)
        entry.conviction = sum(abs(s.position_fraction) for s in stances) / len(stances)
        entry.quality = sum(s.quality for s in stances) / len(stances)
        entry.agreement = safe_div(max(entry.longs, entry.shorts), entry.holders, 0.0)
        entry.score = _score(entry)
        report.coins.append(entry)

    report.coins.sort(key=lambda c: -c.score)
    return report


def _score(entry: CoinConsensus) -> float:
    """Rank by agreement, breadth, track record and size of commitment.

    Deliberately multiplicative: one account with a huge position and no
    corroboration should not outrank four good traders on the same side.
    """
    breadth = clamp((entry.holders - 1) / 4.0, 0.0, 1.0)  # 1 holder scores 0
    agreement = clamp((entry.agreement - 0.5) * 2.0, 0.0, 1.0)  # 50/50 scores 0
    conviction = clamp(entry.conviction / 0.30, 0.0, 1.0)
    return round(breadth * agreement * entry.quality * (0.4 + 0.6 * conviction) * 100, 1)


def report_to_dict(report: ConsensusReport, limit: int = 12) -> dict:
    """Flat JSON for the dashboard and the exported snapshot."""
    return {
        "generated_ms": report.generated_ms,
        "accounts_considered": report.accounts_considered,
        "accounts_with_positions": report.accounts_with_positions,
        "accounts_active_in_window": report.accounts_active_in_window,
        "flow_window_hours": report.flow_window_hours,
        "has_flow": report.has_flow,
        "long_share": report.long_share,
        "one_sided_cohort": report.one_sided_cohort,
        "cohort_warning": report.cohort_warning,
        "coins": [
            {
                "coin": entry.coin,
                "side": entry.side,
                "score": entry.score,
                "holders": entry.holders,
                "longs": entry.longs,
                "shorts": entry.shorts,
                "agreement": entry.agreement,
                "conviction": entry.conviction,
                "quality": entry.quality,
                "flow_usd": entry.flow_usd,
                "flow_accounts": entry.flow_accounts,
                "contested": entry.contested,
                "rationale": entry.rationale(),
                "participants": [
                    {
                        "address": stance.address,
                        "name": stance.name,
                        "is_long": stance.is_long,
                        "position_fraction": stance.position_fraction,
                        "notional": stance.notional,
                        "leverage": stance.leverage,
                        "unrealized": stance.unrealized,
                        "quality": stance.quality,
                        "side_win_rate": stance.side_win_rate,
                        "side_trades": stance.side_trades,
                        "flow_usd": stance.flow_usd,
                    }
                    for stance in entry.participants[:8]
                ],
            }
            for entry in report.coins[:limit]
        ],
    }
