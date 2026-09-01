"""The risk envelope: evaluated before any order, and sticky once tripped."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..api.info import AccountState
from ..config import RiskConfig
from ..log import get_logger
from ..util import safe_div, utc_day

log = get_logger("copy.risk")


@dataclass
class RiskState:
    day: str = ""
    day_start_equity: float = 0.0
    high_water_mark: float = 0.0
    kill_switch: bool = False
    kill_reason: str = ""
    halted: bool = False
    halt_reason: str = ""
    realised_day_pnl: float = 0.0
    breaches: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.kill_switch or self.halted


class RiskManager:
    """Owns the kill switch.

    Sticky by design: once tripped it stays tripped until the UTC day rolls over
    or an operator clears it. A bot that has just lost its daily limit is a bot in
    an unknown state, and 'try again next cycle' is how a bad day becomes a
    catastrophic one.
    """

    def __init__(self, config: RiskConfig):
        self.config = config
        self.state = RiskState()

    def observe(self, account: AccountState) -> RiskState:
        """Update daily anchors and evaluate the kill conditions."""
        today = utc_day()
        equity = account.account_value

        if self.state.day != today:
            if self.state.day:
                log.info("UTC day rollover %s -> %s: kill switch reset", self.state.day, today)
            self.state.day = today
            self.state.day_start_equity = equity
            self.state.kill_switch = False
            self.state.kill_reason = ""
            self.state.breaches = []

        if equity > self.state.high_water_mark:
            self.state.high_water_mark = equity

        self.state.halted = False
        self.state.halt_reason = ""
        if equity < self.config.min_account_value:
            self.state.halted = True
            self.state.halt_reason = (
                f"account value ${equity:,.2f} below floor ${self.config.min_account_value:,.2f}"
            )

        if not self.state.kill_switch:
            reason = self._kill_reason(equity)
            if reason:
                self.state.kill_switch = True
                self.state.kill_reason = reason
                self.state.breaches.append(reason)
                log.critical("KILL SWITCH: %s", reason)
        return self.state

    def _kill_reason(self, equity: float) -> str:
        day_loss = safe_div(
            self.state.day_start_equity - equity, self.state.day_start_equity, 0.0
        )
        if self.config.daily_loss_limit > 0 and day_loss >= self.config.daily_loss_limit:
            return (
                f"daily loss {day_loss:.1%} hit the {self.config.daily_loss_limit:.0%} limit "
                f"(${self.state.day_start_equity:,.0f} -> ${equity:,.0f})"
            )
        drawdown = safe_div(
            self.state.high_water_mark - equity, self.state.high_water_mark, 0.0
        )
        if self.config.max_drawdown_limit > 0 and drawdown >= self.config.max_drawdown_limit:
            return (
                f"drawdown {drawdown:.1%} from high-water mark "
                f"${self.state.high_water_mark:,.0f} hit the "
                f"{self.config.max_drawdown_limit:.0%} limit"
            )
        return ""

    def clear_kill_switch(self, note: str = "manual") -> None:
        if self.state.kill_switch:
            log.warning("kill switch cleared (%s)", note)
        self.state.kill_switch = False
        self.state.kill_reason = ""

    def allows_opening(self) -> bool:
        """Reductions are always allowed; only new risk is blocked."""
        return not self.state.blocked

    def day_pnl(self, equity: float) -> tuple[float, float]:
        absolute = equity - self.state.day_start_equity
        return absolute, safe_div(absolute, self.state.day_start_equity, 0.0)
