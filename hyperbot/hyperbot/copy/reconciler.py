"""Diff the target book against what we actually hold, and emit the orders.

Reconciliation - not fill mirroring - is what keeps a copy bot correct. Mirroring
fills desyncs permanently the first time an order is rejected or a socket drops;
converging on a target self-heals on the next cycle.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..api.info import AccountState, AssetMeta
from ..config import CopyConfig, RiskConfig
from ..log import get_logger
from ..util import sign

log = get_logger("copy.reconciler")


@dataclass
class Adjustment:
    coin: str
    current_notional: float
    target_notional: float
    delta_notional: float
    kind: str  # open | increase | reduce | close | flip
    reduce_only: bool

    def describe(self) -> str:
        return (
            f"{self.kind.upper():<8} {self.coin:<8} "
            f"${self.current_notional:>12,.0f} -> ${self.target_notional:>12,.0f} "
            f"(delta ${self.delta_notional:>+12,.0f})"
        )


def classify(current: float, target: float) -> str:
    if current == 0:
        return "open" if target != 0 else "none"
    if target == 0:
        return "close"
    if sign(current) != sign(target):
        return "flip"
    return "increase" if abs(target) > abs(current) else "reduce"


def reconcile(
    account: AccountState,
    targets: dict[str, float],
    meta: dict[str, AssetMeta],
    copy_config: CopyConfig,
    risk_config: RiskConfig,
    *,
    allow_opening: bool = True,
) -> list[Adjustment]:
    """Produce the minimum set of adjustments that move us onto the target book."""
    equity = account.account_value
    deadband = max(copy_config.min_order_usd, copy_config.deadband_pct * equity)

    coins = set(targets) | set(account.positions)
    adjustments: list[Adjustment] = []

    for coin in sorted(coins):
        asset = meta.get(coin)
        if asset is None or asset.mark_price <= 0:
            if coin in targets:
                log.debug("skipping %s: no tradable market data", coin)
            continue

        current = account.signed_notional(coin)
        target = targets.get(coin, 0.0)

        # A coin no leader holds any more is closed only if we are allowed to.
        if coin not in targets and not copy_config.close_orphans:
            continue

        delta = target - current
        if abs(delta) < deadband:
            continue

        kind = classify(current, target)
        if kind == "none":
            continue

        # Blocked from opening (kill switch): still permit risk-reducing moves.
        if not allow_opening and kind in ("open", "increase", "flip"):
            if kind == "flip":
                # Reduce to flat instead of flipping through zero into new risk.
                delta = -current
                kind = "close"
                target = 0.0
            else:
                continue

        adjustments.append(
            Adjustment(
                coin=coin,
                current_notional=current,
                target_notional=target,
                delta_notional=delta,
                kind=kind,
                # reduce_only is only safe when the move cannot cross zero.
                reduce_only=kind in ("reduce", "close"),
            )
        )

    # Risk-reducing adjustments first: free margin before spending it.
    order = {"close": 0, "reduce": 1, "flip": 2, "increase": 3, "open": 4}
    adjustments.sort(key=lambda item: (order.get(item.kind, 9), -abs(item.delta_notional)))
    return adjustments
