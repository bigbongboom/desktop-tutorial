"""Tests for the positioning consensus.

The fixtures encode what live data actually looked like: a cohort that is ~95%
long, coins held by a crowd with a poor record, and windows in which almost
nobody traded.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hyperbot.research.consensus import (  # noqa: E402
    Stance,
    build_consensus,
    quality_weight,
    report_to_dict,
)
from hyperbot.research.profile import SideStats, TraderProfile  # noqa: E402


def stance(name="t", is_long=True, fraction=0.5, quality=0.8, flow=0.0, address=None):
    return Stance(
        address=address or f"0x{name}", name=name, is_long=is_long,
        position_fraction=fraction if is_long else -fraction,
        notional=fraction * 10_000 * (1 if is_long else -1),
        leverage=3.0, unrealized=0.0, quality=quality,
        side_win_rate=0.7, side_trades=30, flow_usd=flow,
    )


def report(stances_by_coin, **kwargs):
    kwargs.setdefault("accounts_considered", 10)
    kwargs.setdefault("accounts_with_positions", 8)
    kwargs.setdefault("accounts_active", 4)
    kwargs.setdefault("flow_window_hours", 24)
    return build_consensus(stances_by_coin, **kwargs)


def profile(long_trades=40, long_wr=0.9, long_pnl=1000.0,
            short_trades=0, short_wr=0.0, short_pnl=0.0, unrealized=0.0):
    p = TraderProfile(address="0x1")
    p.trades = long_trades + short_trades
    p.wins = round(long_trades * long_wr + short_trades * short_wr)
    p.losses = p.trades - p.wins
    p.unrealized_pnl = unrealized
    for label, n, wr, pnl in (("long", long_trades, long_wr, long_pnl),
                              ("short", short_trades, short_wr, short_pnl)):
        side = SideStats(label)
        side.trades = n
        side.wins = round(n * wr)
        side.pnl = pnl
        side.gross_win = max(pnl, 0.0) * 2
        side.gross_loss = -abs(pnl)
        setattr(p, label, side)
    return p


class TestQualityWeight:
    def test_a_strong_record_on_that_side_scores_high(self):
        assert quality_weight(profile(long_trades=60, long_wr=0.9), True) > 0.6

    def test_a_side_they_are_bad_at_scores_low(self):
        """A 90%-on-longs trader must not lend that record to a short."""
        p = profile(long_trades=60, long_wr=0.9, short_trades=20,
                    short_wr=0.2, short_pnl=-500.0)
        assert quality_weight(p, True) > quality_weight(p, False) * 2

    def test_unknown_trader_gets_a_low_default(self):
        assert quality_weight(None, True) == 0.25

    def test_holding_losers_discounts_the_record(self):
        clean = profile(long_trades=40, long_wr=1.0)
        holder = profile(long_trades=40, long_wr=1.0, unrealized=-5000.0)
        assert holder.never_takes_losses
        assert quality_weight(holder, True) < quality_weight(clean, True)

    def test_thin_sample_is_discounted(self):
        thin = profile(long_trades=6, long_wr=1.0)
        thick = profile(long_trades=60, long_wr=1.0)
        assert quality_weight(thin, True) < quality_weight(thick, True)

    def test_weight_is_bounded(self):
        assert 0.0 <= quality_weight(profile(long_trades=500, long_wr=1.0), True) <= 1.0


class TestConsensus:
    def test_unanimous_beats_contested(self):
        r = report({
            "AAA": [stance("a"), stance("b"), stance("c")],
            "BBB": [stance("d"), stance("e", is_long=False), stance("f")],
        })
        assert r.coins[0].coin == "AAA"
        assert r.coins[0].agreement == 1.0
        assert r.coins[1].contested

    def test_a_single_holder_scores_zero(self):
        """One account with a big position is not a consensus."""
        r = report({"AAA": [stance("a", fraction=5.0)]})
        assert r.coins[0].score == 0.0

    def test_side_reflects_the_majority(self):
        r = report({"AAA": [stance("a", is_long=False), stance("b", is_long=False),
                            stance("c")]})
        assert r.coins[0].side == "SHORT"

    def test_even_split_is_reported_as_split(self):
        r = report({"AAA": [stance("a"), stance("b", is_long=False)]})
        assert r.coins[0].side == "SPLIT"

    def test_crowded_but_weak_is_called_out(self):
        r = report({"AAA": [stance(f"t{i}", quality=0.1) for i in range(6)]})
        assert "breadth here is not evidence" in r.coins[0].rationale()

    def test_quality_outranks_raw_breadth(self):
        r = report({
            "CROWD": [stance(f"c{i}", quality=0.08, fraction=0.5) for i in range(6)],
            "GOOD": [stance(f"g{i}", quality=0.95, fraction=0.5) for i in range(4)],
        })
        assert r.coins[0].coin == "GOOD"

    def test_one_sided_cohort_is_flagged(self):
        """The live cohort was 95% long - a selection artefact, not a signal."""
        r = report({"AAA": [stance(f"t{i}") for i in range(10)],
                    "BBB": [stance("s", is_long=False)]})
        assert r.long_share > 0.85
        assert r.one_sided_cohort
        assert "selected for realised profit" in r.cohort_warning

    def test_balanced_cohort_is_not_flagged(self):
        r = report({"AAA": [stance(f"l{i}") for i in range(5)],
                    "BBB": [stance(f"s{i}", is_long=False) for i in range(5)]})
        assert not r.one_sided_cohort
        assert r.cohort_warning == ""

    def test_flow_is_summed_and_counted(self):
        r = report({"AAA": [stance("a", flow=1000.0), stance("b", flow=-250.0),
                            stance("c")]})
        assert r.coins[0].flow_usd == 750.0
        assert r.coins[0].flow_accounts == 2

    def test_has_flow_requires_enough_active_accounts(self):
        """At 04:00 UTC almost nobody had traded; that must not read as signal."""
        assert not report({"AAA": [stance("a")]}, accounts_active=1).has_flow
        assert report({"AAA": [stance("a")]}, accounts_active=5).has_flow

    def test_empty_input(self):
        r = report({})
        assert r.coins == [] and r.long_share == 0.0

    def test_serialises_for_the_dashboard(self):
        r = report({"AAA": [stance("a"), stance("b")]})
        payload = report_to_dict(r)
        assert payload["coins"][0]["coin"] == "AAA"
        assert payload["coins"][0]["participants"][0]["name"]
        assert "flow_window_hours" in payload
