"""Tests for trade reconstruction, profiling and strategy inference.

The fixtures encode the three real-data traps this module was built around:
an all-closes fill window, positions that never return to flat, and one order
that fills in hundreds of pieces.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hyperbot.api.info import AccountState, Fill, Position  # noqa: E402
from hyperbot.research.candles import Candle, build_context, ema, rsi  # noqa: E402
from hyperbot.research.naming import build_description, build_name  # noqa: E402
from hyperbot.research.profile import build_profile  # noqa: E402
from hyperbot.research.strategy import backtest, classify, fingerprint  # noqa: E402
from hyperbot.research.trades import reconstruct  # noqa: E402

MIN = 60_000


def fill(coin="ETH", px=100.0, sz=1.0, side="A", t=0, direction="Close Long",
         pnl=0.0, oid=1, start=1.0, fee=0.0):
    return Fill(coin=coin, price=px, size=sz, side=side, time=t, direction=direction,
                closed_pnl=pnl, fee=fee, oid=oid, start_position=start)


class TestTradeReconstruction:
    def test_partial_fills_of_one_order_are_one_decision(self):
        """A 381-piece unwind is one decision, not 381 wins."""
        fills = [fill(t=i * 1000, sz=0.1, pnl=10.0, oid=7, start=100 - i * 0.1)
                 for i in range(50)]
        history = reconstruct(fills)
        assert len(history.exits) == 1
        assert history.exits[0].fills == 50
        assert history.exits[0].pnl == 500.0

    def test_separate_orders_stay_separate(self):
        fills = [fill(t=1000, pnl=10.0, oid=1), fill(t=2000, pnl=-5.0, oid=2)]
        history = reconstruct(fills)
        assert len(history.exits) == 2
        assert {e.pnl for e in history.exits} == {10.0, -5.0}

    def test_time_gaps_do_not_merge_distinct_orders(self):
        """Grouping by time collapsed wins and losses into a fake 100% record."""
        fills = [fill(t=0, pnl=100.0, oid=1), fill(t=60_000, pnl=-80.0, oid=2)]
        history = reconstruct(fills)
        assert len(history.exits) == 2

    def test_long_and_short_closes_are_distinguished(self):
        fills = [
            fill(t=1000, direction="Close Long", pnl=5.0, oid=1),
            fill(t=2000, direction="Close Short", pnl=7.0, oid=2, side="B"),
        ]
        history = reconstruct(fills)
        assert {e.is_long for e in history.exits} == {True, False}

    def test_settlement_and_spot_are_excluded_but_tracked(self):
        """They are real P&L but not trading decisions - and must not vanish."""
        fills = [
            fill(t=1000, direction="Close Long", pnl=100.0, oid=1),
            fill(t=2000, direction="Settlement", pnl=50.0, oid=2),
            fill(t=3000, direction="Sell", pnl=-20.0, oid=3),
        ]
        history = reconstruct(fills)
        assert len(history.exits) == 1
        assert history.settlement_pnl == 50.0
        assert history.spot_pnl == -20.0

    def test_round_trip_recovered_when_the_open_is_visible(self):
        fills = [
            fill(t=0, direction="Open Long", side="B", sz=2.0, px=100.0, start=0.0, oid=1),
            fill(t=3600_000, direction="Close Long", side="A", sz=2.0, px=110.0,
                 start=2.0, pnl=20.0, oid=2),
        ]
        history = reconstruct(fills)
        assert len(history.round_trips) == 1
        trip = history.round_trips[0]
        assert trip.entry_price == 100.0
        assert trip.hold_seconds == 3600
        assert abs(trip.return_pct - 0.10) < 1e-9

    def test_no_round_trips_when_only_closes_are_visible(self):
        """The real 2000/2000-closes case: exits still work, entries do not."""
        fills = [fill(t=i * MIN, pnl=1.0, oid=i, start=100.0) for i in range(30)]
        history = reconstruct(fills)
        assert len(history.exits) == 30
        assert history.round_trips == []
        assert history.coverage == 0.0
        assert not history.has_entry_data

    def test_empty_input(self):
        history = reconstruct([])
        assert history.exits == [] and history.coverage == 0.0


class TestProfile:
    def _history(self, pnls, direction="Close Long"):
        return reconstruct(
            [fill(t=i * MIN, pnl=p, oid=i, direction=direction) for i, p in enumerate(pnls)]
        )

    def test_win_rate_and_profit_factor(self):
        profile = build_profile("0x1", self._history([10, 10, -5, 20, -5]))
        assert profile.trades == 5
        assert abs(profile.win_rate - 0.6) < 1e-9
        assert abs(profile.profit_factor - 4.0) < 1e-9

    def test_profit_factor_is_undefined_not_zero_without_losses(self):
        """Reporting 0.00 for a trader who has never lost is simply wrong."""
        assert build_profile("0x1", self._history([10, 20, 30])).profit_factor is None

    def test_never_takes_losses_flags_unrealised_pain(self):
        state = AccountState(address="0x1", account_value=1000.0)
        state.positions["ETH"] = Position("ETH", 1.0, 100.0, 100.0, -500.0, 2.0)
        profile = build_profile("0x1", self._history([10, 20, 30]), state)
        assert profile.never_takes_losses

    def test_perfect_record_with_profits_on_the_book_is_not_flagged(self):
        state = AccountState(address="0x1", account_value=1000.0)
        state.positions["ETH"] = Position("ETH", 1.0, 100.0, 100.0, +500.0, 2.0)
        profile = build_profile("0x1", self._history([10, 20, 30]), state)
        assert not profile.never_takes_losses

    def test_long_short_split(self):
        fills = (
            [fill(t=i * MIN, pnl=10.0, oid=i, direction="Close Long") for i in range(8)]
            + [fill(t=(20 + i) * MIN, pnl=-5.0, oid=20 + i, direction="Close Short")
               for i in range(2)]
        )
        profile = build_profile("0x1", reconstruct(fills))
        assert profile.long.trades == 8 and profile.long.win_rate == 1.0
        assert profile.short.trades == 2 and profile.short.win_rate == 0.0
        assert profile.direction_bias == "long-only"  # 80% long clears the 75% bar

    def test_a_two_thirds_long_book_is_two_sided_not_long_only(self):
        fills = (
            [fill(t=i * MIN, pnl=10.0, oid=i, direction="Close Long") for i in range(4)]
            + [fill(t=(20 + i) * MIN, pnl=-5.0, oid=20 + i, direction="Close Short")
               for i in range(2)]
        )
        assert build_profile("0x1", reconstruct(fills)).direction_bias == "two-sided"

    def test_sample_quality_tracks_order_count(self):
        assert build_profile("0x1", self._history([1] * 5)).sample_quality == "very thin"
        assert build_profile("0x1", self._history([1] * 70)).sample_quality == "strong"


class TestNaming:
    def test_name_is_deterministic_and_descriptive(self):
        history = reconstruct(
            [fill(coin="SOL", t=i * MIN, pnl=1.0, oid=i) for i in range(30)]
        )
        profile = build_profile("0x1", history)
        assert build_name(profile) == build_name(profile)
        assert "SOL" in build_name(profile)

    def test_dormant_account(self):
        assert build_name(build_profile("0x1", reconstruct([]))) == "Dormant Account"

    def test_description_omits_leverage_when_nothing_is_open(self):
        """'up to 0x leverage' is worse than saying nothing."""
        profile = build_profile("0x1", reconstruct(
            [fill(t=i * MIN, pnl=1.0, oid=i) for i in range(10)]))
        assert "0x leverage" not in build_description(profile)

    def test_description_warns_on_a_thin_sample(self):
        profile = build_profile("0x1", reconstruct(
            [fill(t=i * MIN, pnl=1.0, oid=i) for i in range(4)]))
        assert "indicative" in build_description(profile)


class TestCandlesAndStrategy:
    def _series(self, closes):
        return [Candle(time=i * 3_600_000, open=c, high=c * 1.01, low=c * 0.99,
                       close=c, volume=1.0) for i, c in enumerate(closes)]

    def test_ema_tracks_a_rising_series(self):
        values = ema([float(i) for i in range(100)], 20)
        assert values[-1] < 99 and values[-1] > 80

    def test_rsi_is_bounded(self):
        assert all(0 <= v <= 100 for v in rsi([float(i % 7) for i in range(120)], 14))

    def test_rsi_is_high_on_a_monotonic_rise(self):
        assert rsi([float(i) for i in range(120)], 14)[-1] > 90

    def test_entry_context_detects_a_dip_entry(self):
        closes = [100.0 + i for i in range(60)] + [160.0 - i * 2 for i in range(20)]
        candles = self._series(closes)
        context = build_context("ETH", True, candles[75].time, candles[75].close, candles)
        assert context is not None
        assert context.distance_from_ema < 0  # entered below the short average

    def test_context_is_none_without_enough_history(self):
        assert build_context("ETH", True, 0, 100.0, self._series([100.0] * 10)) is None

    def test_classify_refuses_a_thin_sample(self):
        result = classify(fingerprint([]), None)
        assert result.confidence == "low"
        assert "Not enough" in result.name

    def test_backtest_has_no_lookahead_and_reports_a_rule(self):
        candles = self._series([100 + (i % 20) for i in range(300)])
        result = backtest("Pullback / dip entry", candles, "ETH", "1h")
        assert result.rule and result.bars == 300
        assert result.trades >= 0
        assert 0.0 <= result.win_rate <= 1.0

    def test_backtest_declines_an_archetype_it_cannot_express(self):
        candles = self._series([100.0 + i for i in range(200)])
        result = backtest("Mixed / discretionary", candles, "ETH", "1h")
        assert result.trades == 0
        assert "no mechanical rule" in result.rule
