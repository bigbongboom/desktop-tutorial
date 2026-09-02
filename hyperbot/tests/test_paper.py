"""Tests for the simulated $1,000 account."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hyperbot.paper.broker import DUST_USD, PaperBroker  # noqa: E402


def broker(equity=1000.0, fee=4.5, slip=5.0):
    return PaperBroker(equity, taker_fee_bps=fee, slippage_bps=slip)


class TestExecution:
    def test_opening_charges_a_fee_and_slippage(self):
        b = broker()
        fill = b.execute("ETH", 100.0, 2000.0)
        assert fill is not None
        assert b.state.fees_paid > 0
        assert fill.price > 2000.0          # a buyer pays up
        assert b.state.cash < 1000.0        # the fee came out

    def test_a_sell_fills_below_the_mark(self):
        b = broker()
        fill = b.execute("ETH", -100.0, 2000.0)
        assert fill.price < 2000.0

    def test_equity_tracks_the_mark(self):
        b = broker()
        b.execute("ETH", 100.0, 2000.0)
        assert b.state.equity({"ETH": 2200.0}) > b.state.equity({"ETH": 2000.0})

    def test_closing_realises_the_gain(self):
        """Flattening means closing the position's CURRENT value, not the
        notional that opened it - $100 bought at 2000 is worth $110 at 2200,
        and closing only $100 of it leaves a real residual."""
        b = broker()
        b.execute("ETH", 100.0, 2000.0)
        before = b.state.realized_pnl
        held = b.state.positions["ETH"]
        b.execute("ETH", -held.size * 2200.0, 2200.0)
        assert b.state.realized_pnl > before
        assert "ETH" not in b.state.positions

    def test_closing_the_opening_notional_leaves_a_real_residual(self):
        b = broker()
        b.execute("ETH", 100.0, 2000.0)
        b.execute("ETH", -100.0, 2200.0)
        assert "ETH" in b.state.positions          # ~$10 still held, not dust
        assert b.state.positions["ETH"].size > 0

    def test_dust_is_swept_not_left_as_a_phantom_short(self):
        """Over-closing by a rounding error must not leave a tiny opposite leg."""
        b = broker()
        b.execute("ETH", 100.0, 2000.0)
        b.execute("ETH", -100.5, 2000.0)     # slight overshoot
        assert "ETH" not in b.state.positions

    def test_a_real_position_is_not_swept(self):
        b = broker()
        b.execute("ETH", DUST_USD * 20, 2000.0)
        assert "ETH" in b.state.positions

    def test_flip_resets_the_entry(self):
        b = broker()
        b.execute("ETH", 100.0, 2000.0)
        b.execute("ETH", -200.0, 2100.0)
        assert b.state.positions["ETH"].size < 0
        assert b.state.positions["ETH"].entry_price < 2100.0 * 1.01


class TestCosts:
    def test_longs_pay_positive_funding(self):
        b = broker()
        b.execute("ETH", 500.0, 2000.0)
        b.state.last_funding_ms -= 3_600_000       # pretend an hour passed
        paid = b.apply_funding({"ETH": 2000.0}, {"ETH": 0.0000125})
        assert paid > 0
        assert b.state.funding_paid > 0

    def test_shorts_receive_positive_funding(self):
        b = broker()
        b.execute("ETH", -500.0, 2000.0)
        b.state.last_funding_ms -= 3_600_000
        assert b.apply_funding({"ETH": 2000.0}, {"ETH": 0.0000125}) < 0

    def test_cost_drag_is_not_annualised_from_seconds(self):
        """A 40-second run once reported 64,578%/yr."""
        b = broker()
        b.execute("ETH", 500.0, 2000.0)
        summary = b.summary({"ETH": 2000.0})
        assert summary["cost_drag_annual_pct"] is None
        assert summary["cost_pct_of_capital"] > 0

    def test_cost_drag_appears_once_the_run_is_long_enough(self):
        b = broker()
        b.execute("ETH", 500.0, 2000.0)
        b.state.started_ms -= 24 * 3_600_000
        assert b.summary({"ETH": 2000.0})["cost_drag_annual_pct"] is not None


class TestRisk:
    def test_liquidation_when_equity_falls_below_maintenance(self):
        b = broker(100.0)
        b.execute("ETH", 1000.0, 2000.0, leverage=10.0)
        assert b.check_liquidation({"ETH": 1200.0})
        assert b.state.liquidated
        assert not b.state.positions

    def test_a_healthy_account_is_not_liquidated(self):
        b = broker(1000.0)
        b.execute("ETH", 500.0, 2000.0, leverage=2.0)
        assert not b.check_liquidation({"ETH": 2010.0})

    def test_a_liquidated_account_stops_trading(self):
        b = broker(100.0)
        b.execute("ETH", 1000.0, 2000.0, leverage=10.0)
        b.check_liquidation({"ETH": 1200.0})
        assert b.execute("ETH", 100.0, 1200.0) is None


class TestPersistence:
    def test_round_trip_preserves_the_account(self):
        b = broker()
        b.execute("ETH", 200.0, 2000.0)
        b.execute("BTC", -150.0, 60000.0)
        restored = PaperBroker.from_dict(b.to_dict())
        assert restored.state.cash == b.state.cash
        assert set(restored.state.positions) == set(b.state.positions)
        assert restored.state.trades == b.state.trades
