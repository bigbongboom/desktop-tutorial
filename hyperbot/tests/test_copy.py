"""Tests for sizing, reconciliation, risk and Hyperliquid order rounding."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from hyperbot.api.info import AccountState, AssetMeta, Position  # noqa: E402
from hyperbot.config import CopyConfig, RiskConfig  # noqa: E402
from hyperbot.copy.reconciler import classify, reconcile  # noqa: E402
from hyperbot.copy.risk import RiskManager  # noqa: E402
from hyperbot.copy.sizing import LeaderView, build_target_book  # noqa: E402
from hyperbot.util import round_price, round_size  # noqa: E402


def account(equity=10_000.0, positions=None):
    state = AccountState(address="0xme", account_value=equity)
    for coin, notional in (positions or {}).items():
        state.positions[coin] = Position(
            coin=coin,
            size=abs(notional) / 100.0 * (1 if notional > 0 else -1),
            entry_price=100.0,
            position_value=abs(notional),
            unrealized_pnl=0.0,
            leverage=1.0,
        )
    return state


def leader(address, equity, weights):
    return LeaderView(address=address, label=address, equity=equity, weights=dict(weights))


META = {
    "BTC": AssetMeta("BTC", 0, 5, 40, mark_price=60_000.0),
    "ETH": AssetMeta("ETH", 1, 4, 25, mark_price=3_000.0),
    "SOL": AssetMeta("SOL", 2, 2, 20, mark_price=150.0),
}


class TestRounding:
    """Getting these wrong is the most common cause of silent order rejection."""

    def test_price_respects_five_significant_figures(self):
        # szDecimals=1 -> 5 decimals allowed, so the sig-fig rule is what binds.
        assert round_price(1.23456789, 1) == 1.2346
        assert round_price(12.3456789, 1) == 12.346

    def test_price_respects_max_decimals_for_the_asset(self):
        # Perps allow (6 - szDecimals) decimals; that rule binds first here.
        assert round_price(1234.567, 5) == 1234.6   # szDecimals=5 -> 1 decimal
        assert round_price(1.23456789, 4) == 1.23   # szDecimals=4 -> 2 decimals
        assert round_price(1.23456789, 6) == 1.0    # szDecimals=6 -> 0 decimals

    def test_large_prices_round_to_integers(self):
        assert round_price(123_456.789, 5) == 123_457.0

    def test_size_rounds_to_asset_precision(self):
        assert round_size(0.123456789, 5) == 0.12346
        assert round_size(1.999, 0) == 2.0


class TestTargetBook:
    def test_blends_leader_weights_by_allocation(self):
        leaders = {
            "a": leader("a", 100_000, {"BTC": 0.40}),
            "b": leader("b", 1_000_000, {"BTC": 0.20}),
        }
        copy_config = CopyConfig(exposure_multiplier=1.0)
        book = build_target_book(leaders, {"a": 0.5, "b": 0.5}, 10_000, copy_config, RiskConfig())
        # 0.5*0.40 + 0.5*0.20 = 0.30 of our equity
        assert book.positions["BTC"].weight == pytest.approx(0.30)
        assert book.positions["BTC"].notional == pytest.approx(3_000)

    def test_scales_a_whale_position_to_our_account_size(self):
        """A $10M whale's $4M position becomes 40% of OUR equity, not $4M."""
        leaders = {"whale": leader("whale", 10_000_000, {"ETH": 0.40})}
        book = build_target_book(
            leaders, {"whale": 1.0}, 5_000, CopyConfig(exposure_multiplier=1.0), RiskConfig()
        )
        assert book.positions["ETH"].notional == pytest.approx(2_000)

    def test_shorts_are_negative_and_survive_the_pipeline(self):
        leaders = {"a": leader("a", 100_000, {"ETH": -0.30})}
        book = build_target_book(
            leaders, {"a": 1.0}, 10_000, CopyConfig(exposure_multiplier=1.0), RiskConfig()
        )
        assert book.positions["ETH"].notional < 0
        assert book.positions["ETH"].direction == "SHORT"

    def test_opposing_leaders_net_off(self):
        """Two leaders on opposite sides must net, not open both sides at once."""
        leaders = {
            "a": leader("a", 100_000, {"BTC": 0.40}),
            "b": leader("b", 100_000, {"BTC": -0.40}),
        }
        book = build_target_book(
            leaders, {"a": 0.5, "b": 0.5}, 10_000, CopyConfig(exposure_multiplier=1.0),
            RiskConfig(),
        )
        assert "BTC" not in book.positions

    def test_exposure_multiplier_scales_everything_down(self):
        leaders = {"a": leader("a", 100_000, {"BTC": 0.40})}
        book = build_target_book(
            leaders, {"a": 1.0}, 10_000, CopyConfig(exposure_multiplier=0.25), RiskConfig()
        )
        assert book.positions["BTC"].weight == pytest.approx(0.10)

    def test_per_coin_cap_clamps_an_oversized_leader_position(self):
        leaders = {"a": leader("a", 100_000, {"BTC": 3.0})}
        risk = RiskConfig(max_position_pct=0.50)
        book = build_target_book(
            leaders, {"a": 1.0}, 10_000, CopyConfig(exposure_multiplier=1.0), risk
        )
        assert book.positions["BTC"].weight == pytest.approx(0.50)
        assert "BTC" in book.clamped

    def test_gross_cap_scales_the_book_proportionally(self):
        leaders = {"a": leader("a", 100_000, {"BTC": 3.0, "ETH": 3.0, "SOL": 3.0})}
        risk = RiskConfig(max_gross_exposure=1.0, max_position_pct=1.0)
        book = build_target_book(
            leaders, {"a": 1.0}, 10_000, CopyConfig(exposure_multiplier=1.0), risk
        )
        assert book.gross == pytest.approx(10_000)
        # Proportional: all three keep equal share rather than some being dropped.
        weights = {p.weight for p in book.positions.values()}
        assert len(weights) == 1

    def test_denylist_and_allowlist(self):
        leaders = {"a": leader("a", 100_000, {"BTC": 0.4, "ETH": 0.4})}
        copy_config = CopyConfig(exposure_multiplier=1.0)
        denied = build_target_book(
            leaders, {"a": 1.0}, 10_000, copy_config, RiskConfig(coin_denylist=["BTC"])
        )
        assert "BTC" not in denied.positions and "ETH" in denied.positions
        allowed = build_target_book(
            leaders, {"a": 1.0}, 10_000, copy_config, RiskConfig(coin_allowlist=["BTC"])
        )
        assert "BTC" in allowed.positions and "ETH" not in allowed.positions

    def test_position_limit_keeps_the_largest(self):
        leaders = {"a": leader("a", 100_000, {"BTC": 0.30, "ETH": 0.20, "SOL": 0.10})}
        risk = RiskConfig(max_concurrent_positions=2, max_gross_exposure=10.0)
        book = build_target_book(
            leaders, {"a": 1.0}, 10_000, CopyConfig(exposure_multiplier=1.0), risk
        )
        assert set(book.positions) == {"BTC", "ETH"}


class TestReconcile:
    def test_classify_covers_every_transition(self):
        assert classify(0, 100) == "open"
        assert classify(100, 0) == "close"
        assert classify(100, -100) == "flip"
        assert classify(100, 200) == "increase"
        assert classify(200, 100) == "reduce"

    def test_deadband_suppresses_churn(self):
        state = account(10_000, {"BTC": 1_000})
        copy_config = CopyConfig(deadband_pct=0.02, min_order_usd=12)
        # A $50 drift is under 2% of $10,000 - not worth the fees.
        assert reconcile(state, {"BTC": 1_050}, META, copy_config, RiskConfig()) == []

    def test_emits_an_order_once_past_the_deadband(self):
        state = account(10_000, {"BTC": 1_000})
        adjustments = reconcile(
            state, {"BTC": 1_500}, META, CopyConfig(deadband_pct=0.02), RiskConfig()
        )
        assert len(adjustments) == 1
        assert adjustments[0].kind == "increase"
        assert adjustments[0].delta_notional == pytest.approx(500)

    def test_reduce_and_close_are_reduce_only(self):
        state = account(10_000, {"BTC": 2_000})
        adjustments = reconcile(state, {"BTC": 500}, META, CopyConfig(), RiskConfig())
        assert adjustments[0].reduce_only is True

    def test_a_flip_is_never_reduce_only(self):
        """reduce_only on a flip would cancel the half that opens the new side."""
        state = account(10_000, {"BTC": 2_000})
        adjustments = reconcile(state, {"BTC": -2_000}, META, CopyConfig(), RiskConfig())
        assert adjustments[0].kind == "flip"
        assert adjustments[0].reduce_only is False

    def test_closes_orphans_no_leader_holds(self):
        state = account(10_000, {"SOL": 900})
        adjustments = reconcile(state, {}, META, CopyConfig(close_orphans=True), RiskConfig())
        assert adjustments[0].kind == "close"

    def test_leaves_orphans_alone_when_configured(self):
        state = account(10_000, {"SOL": 900})
        assert reconcile(state, {}, META, CopyConfig(close_orphans=False), RiskConfig()) == []

    def test_blocked_opening_still_permits_risk_reduction(self):
        state = account(10_000, {"BTC": 2_000})
        adjustments = reconcile(
            state, {"BTC": 500, "ETH": 3_000}, META, CopyConfig(), RiskConfig(),
            allow_opening=False,
        )
        kinds = {a.coin: a.kind for a in adjustments}
        assert kinds == {"BTC": "reduce"}  # the new ETH position is blocked

    def test_blocked_opening_downgrades_a_flip_to_a_close(self):
        """A flip crosses zero into NEW risk, which the kill switch forbids."""
        state = account(10_000, {"BTC": 2_000})
        adjustments = reconcile(
            state, {"BTC": -2_000}, META, CopyConfig(), RiskConfig(), allow_opening=False
        )
        assert adjustments[0].kind == "close"
        assert adjustments[0].target_notional == 0.0

    def test_risk_reducing_orders_are_sequenced_first(self):
        state = account(10_000, {"BTC": 2_000, "ETH": 2_000})
        adjustments = reconcile(
            state, {"BTC": 0, "ETH": 2_000, "SOL": 3_000}, META, CopyConfig(), RiskConfig()
        )
        assert adjustments[0].kind == "close"  # free margin before spending it

    def test_skips_coins_with_no_market_data(self):
        state = account(10_000)
        assert reconcile(state, {"NOTACOIN": 5_000}, META, CopyConfig(), RiskConfig()) == []


class TestRiskManager:
    def test_daily_loss_limit_trips_the_kill_switch(self):
        manager = RiskManager(RiskConfig(daily_loss_limit=0.10))
        manager.observe(account(10_000))
        state = manager.observe(account(8_900))
        assert state.kill_switch
        assert "daily loss" in state.kill_reason

    def test_kill_switch_is_sticky_within_the_day(self):
        """Recovering must NOT re-arm trading: the bot is in an unknown state."""
        manager = RiskManager(RiskConfig(daily_loss_limit=0.10))
        manager.observe(account(10_000))
        manager.observe(account(8_900))
        state = manager.observe(account(10_500))
        assert state.kill_switch
        assert not manager.allows_opening()

    def test_drawdown_limit_measures_from_the_high_water_mark(self):
        manager = RiskManager(RiskConfig(daily_loss_limit=0.99, max_drawdown_limit=0.25))
        manager.observe(account(10_000))
        manager.observe(account(20_000))
        state = manager.observe(account(14_000))
        assert state.kill_switch
        assert "drawdown" in state.kill_reason

    def test_halts_below_the_minimum_account_value(self):
        manager = RiskManager(RiskConfig(min_account_value=50))
        state = manager.observe(account(10))
        assert state.halted and not manager.allows_opening()

    def test_clearing_the_kill_switch_re_arms_trading(self):
        manager = RiskManager(RiskConfig(daily_loss_limit=0.10))
        manager.observe(account(10_000))
        manager.observe(account(8_000))
        manager.clear_kill_switch("operator")
        assert manager.allows_opening()
