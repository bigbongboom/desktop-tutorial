"""Is this trader actually copyable with a small account?

Ranking well on an equity curve is not the same as being worth mirroring. The
gap matters most for a small copier, and it is where the live data was most
misleading:

* One account showed a flawless realised record while sitting on unrealised
  losses. It closes winners and holds losers, so a copier inherits the losers.
* Another had a 100% win rate across six closing orders in a month. Six
  decisions is not a track record, however large the P&L beside it.
* Several had huge equity-curve ROI driven almost entirely by open positions
  they have never closed. Copying an unrealised gain means buying it at today's
  price, not at theirs.

So this gate asks a different question from discovery: has this trader REPEATEDLY
TAKEN MONEY OFF THE TABLE, in a way a $1,000 account can reproduce today?
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..util import clamp, safe_div

# Hyperliquid rejects orders below $10 of notional.
EXCHANGE_MIN_ORDER_USD = 10.0


@dataclass
class CopyAssessment:
    score: float = 0.0                # 0..100
    verdict: str = "unsuitable"       # copyable | marginal | unsuitable
    reasons: list[str] = field(default_factory=list)   # why NOT, in plain words
    strengths: list[str] = field(default_factory=list)
    expressible: float = 0.0          # share of their book a small account can mirror
    smallest_order_usd: float = 0.0
    positions: int = 0

    @property
    def copyable(self) -> bool:
        return self.verdict == "copyable"


def assess(
    dossier: dict,
    *,
    capital: float = 1000.0,
    exposure_multiplier: float = 0.25,
    min_order_usd: float = EXCHANGE_MIN_ORDER_USD,
    min_orders: int = 20,
    min_win_rate: float = 0.40,
) -> CopyAssessment:
    """Judge a researched account as a copy target for `capital`."""
    result = CopyAssessment()
    stats = dossier.get("stats") or {}
    sides = dossier.get("sides") or {}

    orders = stats.get("orders", 0)
    realized = stats.get("total_pnl", 0.0)
    unrealized = stats.get("unrealized_pnl", 0.0)
    win_rate = stats.get("win_rate", 0.0)
    factor = stats.get("profit_factor")
    leverage = max(stats.get("max_leverage", 0.0), stats.get("current_leverage", 0.0))

    # ---- disqualifiers ---------------------------------------------------- #
    if orders < min_orders:
        result.reasons.append(
            f"only {orders} closing orders visible - too few decisions to trust "
            f"(want {min_orders}+)"
        )
    if realized <= 0:
        result.reasons.append(
            f"realised P&L is {'negative' if realized < 0 else 'flat'} "
            f"(${realized:,.0f}) - the gains are on paper, not banked"
        )
    if stats.get("never_takes_losses"):
        result.reasons.append(
            "closes winners and holds losers - a copier inherits the losers"
        )
    if win_rate < min_win_rate and (factor is None or factor < 1.2):
        result.reasons.append(
            f"win rate {win_rate:.0%} without the payoff to justify it"
        )
    # An account whose P&L is mostly unrealised is a bet on positions already
    # entered at prices a copier cannot get.
    if realized > 0 and unrealized > 0 and safe_div(realized, realized + unrealized, 0.0) < 0.15:
        result.reasons.append(
            f"{safe_div(unrealized, realized + unrealized, 0.0):.0%} of their profit "
            "is unrealised - you would be buying it at today's price, not theirs"
        )

    # ---- can a small account express the book? ---------------------------- #
    budget = capital * exposure_multiplier
    smallest = 0.0
    expressible = 0
    total = 0
    for entry in dossier.get("positions_snapshot") or []:
        total += 1
        target = abs(entry.get("fraction", 0.0)) * budget
        smallest = target if smallest == 0 else min(smallest, target)
        if target >= min_order_usd:
            expressible += 1
    result.positions = total
    result.smallest_order_usd = smallest
    result.expressible = safe_div(expressible, total, 1.0) if total else 1.0
    if total and result.expressible < 0.6:
        result.reasons.append(
            f"only {result.expressible:.0%} of their positions clear the "
            f"${min_order_usd:.0f} minimum at ${capital:,.0f}"
        )

    # ---- strengths -------------------------------------------------------- #
    if orders >= 60:
        result.strengths.append(f"{orders} closing orders - a real sample")
    if realized > 0:
        result.strengths.append(f"${realized:,.0f} actually realised")
    if factor and factor >= 1.5:
        result.strengths.append(f"profit factor {factor:.2f}")
    for label in ("long", "short"):
        side = sides.get(label) or {}
        if side.get("trades", 0) >= 15 and side.get("pnl", 0) > 0:
            result.strengths.append(
                f"{label} side works: {side['win_rate']:.0%} on {side['trades']} exits"
            )
    if 3 <= leverage <= 20:
        result.strengths.append(f"uses {leverage:.0f}x leverage")

    # ---- score ------------------------------------------------------------ #
    sample = clamp(orders / 80.0, 0.0, 1.0)
    banked = clamp(safe_div(realized, max(abs(realized) + abs(unrealized), 1.0), 0.0), 0.0, 1.0)
    edge = clamp((win_rate - 0.35) / 0.45, 0.0, 1.0)
    pf_score = 0.5 if factor is None else clamp((factor - 1.0) / 2.0, 0.0, 1.0)
    # Leverage is wanted, but past ~20x it is fragility, not edge.
    lev_score = clamp(leverage / 10.0, 0.0, 1.0) if leverage <= 20 else clamp(30.0 / leverage, 0.0, 1.0)

    raw = (sample * 0.25 + banked * 0.25 + edge * 0.20 + pf_score * 0.20 + lev_score * 0.10)
    raw *= result.expressible
    if result.reasons:
        raw *= 0.35  # a disqualifier is not averaged away
    result.score = round(clamp(raw, 0.0, 1.0) * 100, 1)

    if not result.reasons and result.score >= 45:
        result.verdict = "copyable"
    elif not result.reasons or len(result.reasons) == 1:
        result.verdict = "marginal"
    else:
        result.verdict = "unsuitable"
    return result
