"""Tests for the two rankings, especially what they must REFUSE to rank."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hyperbot.config import DiscoveryConfig  # noqa: E402
from hyperbot.discovery.metrics import build_trader_metrics  # noqa: E402
from hyperbot.discovery.scoring import EliteScorer, RisingScorer  # noqa: E402

DAY = 86_400_000  # fixtures must span real history to clear the longevity floors


def window(points):
    return {
        "accountValueHistory": [[ts, str(eq)] for ts, eq, _ in points],
        "pnlHistory": [[ts, str(pnl)] for ts, _, pnl in points],
        "vlm": "0",
    }


def steady(n=30, equity=100_000.0, step=1_000.0):
    """A clean compounding curve: the profile both rosters should reward."""
    return [(i * DAY, equity + i * step, i * step) for i in range(n)]


def one_hit(n=30, equity=100_000.0):
    """Flat, one enormous period, flat again - a lottery ticket."""
    points = []
    pnl = 0.0
    for i in range(n):
        if i == 15:
            pnl += 30_000.0
        points.append((i * DAY, equity + pnl, pnl))
    return points


def trader(month_points, week_points=None, **kwargs):
    portfolio = {
        "perpMonth": window(month_points),
        "perpWeek": window(week_points or month_points),
        "perpAllTime": window(month_points),
    }
    kwargs.setdefault("account_value", 100_000.0)
    kwargs.setdefault("leaderboard_pnl", {"allTime": 250_000.0})
    return build_trader_metrics("0xtest", portfolio, **kwargs)


class TestEliteScorer:
    def setup_method(self):
        self.scorer = EliteScorer(DiscoveryConfig())

    def test_a_steady_compounder_qualifies_and_scores_well(self):
        result = self.scorer.score(trader(steady()))
        assert result.qualified, result.rejections
        assert result.total > 40

    def test_a_one_hit_wonder_is_rejected_not_merely_downweighted(self):
        """The core design rule: a disqualifying trait cannot be averaged away."""
        result = self.scorer.score(trader(one_hit()))
        assert not result.qualified
        assert any("one-hit wonder" in reason for reason in result.rejections)

    def test_a_one_hit_wonder_scores_below_a_steady_trader(self):
        assert self.scorer.score(trader(one_hit())).total < self.scorer.score(
            trader(steady())
        ).total

    def test_a_dormant_account_is_rejected_however_good_its_history(self):
        """No recent perp activity means nothing to copy, whatever the record."""
        flat = [(i * DAY, 0.0, 0.0) for i in range(30)]
        result = self.scorer.score(trader(flat, leaderboard_pnl={"allTime": 5_000_000.0}))
        assert not result.qualified
        assert any("dormant" in r or "perp capital" in r for r in result.rejections)

    def test_a_losing_trader_is_rejected(self):
        losing = [(i * DAY, 100_000.0 - i * 500, -i * 500.0) for i in range(30)]
        result = self.scorer.score(trader(losing, leaderboard_pnl={"allTime": -10_000.0}))
        assert not result.qualified

    def test_a_deep_drawdown_is_rejected(self):
        points, pnl = [], 0.0
        for i in range(30):
            pnl += 2_000.0 if i < 15 else -4_000.0   # ~46% peak-to-trough
            points.append((i * DAY, 100_000.0 + pnl, pnl))
        result = self.scorer.score(trader(points))
        assert not result.qualified
        assert any("drawdown" in reason for reason in result.rejections)

    def test_short_history_is_rejected(self):
        brief = [(i * DAY, 100_000.0 + i * 500, i * 500.0) for i in range(8)]
        result = self.scorer.score(trader(brief))
        assert not result.qualified

    def test_scores_are_bounded_to_0_100(self):
        for points in (steady(), one_hit(), steady(step=50_000.0)):
            assert 0.0 <= self.scorer.score(trader(points)).total <= 100.0

    def test_breakdown_is_explainable(self):
        result = self.scorer.score(trader(steady()))
        assert "consistency" in result.components
        assert "track_record" in result.components
        assert result.explain()


class TestRisingScorer:
    def setup_method(self):
        self.scorer = RisingScorer(DiscoveryConfig())

    def test_a_small_accelerating_compounder_qualifies(self):
        # Week pace ahead of the month's pace.
        result = self.scorer.score(
            trader(steady(equity=50_000.0, step=500.0),
                   week_points=steady(equity=50_000.0, step=900.0),
                   account_value=50_000.0)
        )
        assert result.qualified, result.rejections
        assert result.total > 0

    def test_an_account_above_the_climber_band_is_rejected(self):
        result = self.scorer.score(
            trader(steady(equity=50_000_000.0, step=500_000.0), account_value=50_000_000.0)
        )
        assert not result.qualified
        assert any("climber band" in reason for reason in result.rejections)

    def test_a_fading_account_is_rejected(self):
        """Big month, dead week: not on the come up any more."""
        flat_week = [(i * DAY, 100_000.0, 0.0) for i in range(30)]
        result = self.scorer.score(trader(steady(), week_points=flat_week))
        assert not result.qualified
        assert any("fading" in r or "week not profitable" in r for r in result.rejections)

    def test_smaller_accounts_outrank_larger_ones_on_identical_performance(self):
        small = self.scorer.score(
            trader(steady(equity=25_000.0, step=250.0),
                   week_points=steady(equity=25_000.0, step=450.0), account_value=25_000.0)
        )
        large = self.scorer.score(
            trader(steady(equity=1_000_000.0, step=10_000.0),
                   week_points=steady(equity=1_000_000.0, step=18_000.0),
                   account_value=1_000_000.0)
        )
        assert small.qualified and large.qualified
        assert small.total > large.total

    def test_a_one_hit_wonder_is_rejected_here_too(self):
        result = self.scorer.score(trader(one_hit(equity=50_000.0), account_value=50_000.0))
        assert not result.qualified
