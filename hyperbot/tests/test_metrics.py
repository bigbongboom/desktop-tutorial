"""Tests for the equity-curve maths, anchored on the real data that broke it."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hyperbot.discovery.metrics import (  # noqa: E402
    _capital_base,
    _max_drawdown,
    _r_squared,
    build_trader_metrics,
    compute_curve_metrics,
    period_returns,
)


def curve(points):
    """[(ts, equity, cum_pnl)] -> the portfolio window shape the API returns."""
    return {
        "accountValueHistory": [[ts, str(eq)] for ts, eq, _ in points],
        "pnlHistory": [[ts, str(pnl)] for ts, _, pnl in points],
        "vlm": "0",
    }


HOUR = 3_600_000


class TestCapitalBase:
    def test_uses_average_of_opening_and_deposit_adjusted_close(self):
        # Opened at 1000, closed at 3000 after depositing 1500 and earning 500.
        assert _capital_base(1000, 3000, 500) == 1750

    def test_survives_the_deposit_case_that_broke_scoring(self):
        """Real account: $495 -> $5,000,000 by deposit in one bucket, -$5,219 PnL.

        Dividing by the opening balance reported -1053% for a week in which the
        trader lost 0.1%, and the compounded curve never recovered.
        """
        base = _capital_base(495.53, 4_995_348.36, -5_218.94)
        assert base > 2_000_000
        assert -0.01 < -5_218.94 / base < 0

    def test_reports_zero_when_the_readings_disagree(self):
        """Withdrawal larger than the closing balance: capital at risk is
        unknowable, so the period must be dropped rather than guessed at."""
        assert _capital_base(0.0, 100.0, 500.0) == 0.0


class TestPeriodReturns:
    def test_skips_ramp_up_periods_below_the_windows_own_scale(self):
        points = [
            (0, 1.0, 0.0),        # dust
            (HOUR, 2.0, 1.0),     # dust
            (2 * HOUR, 10_000.0, 1.0),
            (3 * HOUR, 11_000.0, 1_001.0),
            (4 * HOUR, 12_000.0, 2_001.0),
            (5 * HOUR, 13_000.0, 3_001.0),
        ]
        equity = [[ts, eq] for ts, eq, _ in points]
        pnl = [[ts, p] for ts, _, p in points]
        returns, deltas, stamps, bases = period_returns(equity, pnl)
        assert len(returns) == len(deltas) == len(stamps) == len(bases)
        assert all(base >= 1000 for base in bases)

    def test_returns_are_deposit_neutral(self):
        """A pure deposit with no trading must produce a zero return."""
        equity = [[0, 1000.0], [HOUR, 50_000.0]]
        pnl = [[0, 0.0], [HOUR, 0.0]]
        returns, _, _, _ = period_returns(equity, pnl)
        assert returns == [0.0]


class TestReturnOnCapital:
    def test_roi_is_bounded_by_the_dollars_actually_earned(self):
        """The 281,531% bug: a trader who withdraws profits keeps a small base,
        so compounding invented wealth that never existed. ROI must stay tied to
        PnL over capital deployed."""
        points = [(i * HOUR, 5000.0, i * 200.0) for i in range(21)]
        metrics = compute_curve_metrics("month", curve(points))
        assert metrics.n_periods == 20
        # $4,000 earned on ~$5,000 of capital is ~80%, not thousands of percent.
        assert 0.7 < metrics.roi < 0.9

    def test_roi_is_exactly_pnl_over_average_capital(self):
        """The defining invariant: ROI can never exceed what the dollars support."""
        points = [(i * HOUR, 10_000.0 + i * 500.0, i * 500.0) for i in range(11)]
        metrics = compute_curve_metrics("month", curve(points))
        total_pnl = 5_000.0
        assert abs(metrics.roi - total_pnl / metrics.avg_capital) < 1e-9
        assert 0.3 < metrics.roi < 0.5  # $5k earned on ~$12k average capital

    def test_losing_account_reports_a_negative_roi(self):
        points = [(i * HOUR, 10_000.0, -i * 100.0) for i in range(11)]
        metrics = compute_curve_metrics("month", curve(points))
        assert metrics.roi < 0
        assert metrics.consistency == 0.0


class TestShapeStatistics:
    def test_max_drawdown(self):
        assert _max_drawdown([1.0, 1.5, 0.75, 1.2]) == 0.5
        assert _max_drawdown([1.0, 1.1, 1.2]) == 0.0

    def test_r_squared_rewards_straight_lines(self):
        straight = [1.0 + 0.1 * i for i in range(20)]
        assert _r_squared(straight) > 0.99

    def test_r_squared_punishes_a_single_jump(self):
        """The one-hit wonder: flat, one vertical move, flat again."""
        jumpy = [1.0] * 10 + [3.0] * 10
        assert _r_squared(jumpy) < _r_squared([1.0 + 0.1 * i for i in range(20)])

    def test_concentration_detects_a_single_dominant_period(self):
        points = [(0, 10_000.0, 0.0), (HOUR, 10_000.0, 50.0), (2 * HOUR, 10_000.0, 5_000.0)]
        metrics = compute_curve_metrics("month", curve(points))
        assert metrics.concentration > 0.9

    def test_consistency_counts_positive_periods(self):
        points = [(0, 10_000.0, 0.0)]
        for i in range(1, 11):
            points.append((i * HOUR, 10_000.0, points[-1][2] + (100 if i % 2 else -50)))
        metrics = compute_curve_metrics("month", curve(points))
        assert abs(metrics.consistency - 0.5) < 0.01


class TestReliability:
    def test_short_windows_are_not_evidence(self):
        points = [(i * HOUR, 10_000.0, i * 100.0) for i in range(3)]
        assert not compute_curve_metrics("month", curve(points)).reliable

    def test_dense_clean_windows_are_reliable(self):
        points = [(i * HOUR, 10_000.0, i * 100.0) for i in range(20)]
        assert compute_curve_metrics("month", curve(points)).reliable


class TestTraderMetrics:
    def _trader(self, **kwargs):
        points = [(i * HOUR, 10_000.0, i * 100.0) for i in range(20)]
        portfolio = {"perpMonth": curve(points), "perpWeek": curve(points),
                     "perpAllTime": curve(points)}
        return build_trader_metrics("0xabc", portfolio, **kwargs)

    def test_perp_capital_prefers_deployed_capital_over_account_value(self):
        """An account can show $390k of value while trading $5k of perps."""
        trader = self._trader(account_value=390_000.0)
        assert trader.account_value == 390_000.0
        assert 5_000 < trader.perp_capital < 20_000

    def test_pace_ratio_is_scale_free(self):
        trader = self._trader(account_value=10_000.0)
        # Same curve for week and month: week annualised to 30d beats the month.
        assert trader.pace_ratio > 1.0

    def test_pace_ratio_is_zero_when_the_month_lost_money(self):
        losing = [(i * HOUR, 10_000.0, -i * 100.0) for i in range(20)]
        trader = build_trader_metrics(
            "0xabc", {"perpMonth": curve(losing), "perpWeek": curve(losing)}
        )
        assert trader.pace_ratio == 0.0

    def test_track_record_uses_dollars_not_ratios(self):
        trader = self._trader(leaderboard_pnl={"allTime": 250_000.0})
        assert trader.track_record == 250_000.0
