# ETH Sweep Desk

A standalone, single-file dashboard for one setup: the **1-minute liquidity-sweep
reversal** on Ethereum. Zero dependencies, no build step, no shared code or settings
with anything else in this repo.

```sh
../serve.sh          # http://localhost:8765/sweep/
```

Or open `index.html` through any static server. Avoid `file://` — the page then has a
null origin and some browsers block the exchange API calls the live feed depends on.

## The pattern

A **range level** is swept, price **reclaims** it, **TSI** is at its extreme and turning
— then long or short back into the range. Longs come off a swept support, shorts off a
swept resistance.

## How it finds them

- **Levels drawn for you** — pivots clustered into horizontal levels with two or more touches,
  plus fitted sloped channel lines. Near-duplicates are merged and the slope is capped, so what
  you get is the two or three lines a chartist would actually draw, not thirty micro-levels.
- **Sweep detection** — the wick must pierce the level and the close must return inside within
  three bars, with a volume thrust. Rejection is measured across the **whole sweep**, not one
  bar: a three-bar sweep puts its low on a bar that closes at its low and therefore has no wick
  at all.
- **TSI band that adapts** — scaled −1…+1 to match a TradingView pane. The extreme band is
  calibrated to where TSI actually reaches (expanding window, no lookahead) rather than pinned
  to a number, because the scale depends entirely on the lengths you choose. Pin it yourself by
  setting a non-zero band.
- **Trade plan** — entry on the reclaim, stop beyond the sweep wick by a configurable ATR
  buffer, target the opposite side of the range with a 1:1 floor.
- **Leverage panel, told straight** — position size, liquidation price, how far the stop sits
  toward liquidation, loss-if-stopped and round-trip fees as a share of margin, net P&L after
  fees both ways, and the leverage at which that exact stop would clear liquidation with a 30%
  buffer. Sizing is never auto-adjusted; the numbers are shown and the call is yours.
- **Two gates that exist because of leverage** — a stop tighter than one-minute noise is
  rejected, and so is a target that cannot clear its own round-trip fee. At 500× the round trip
  costs 0.04% of the position, which on ETH near $1,880 is about 0.75 of a point.
- **Honest replay** — every sweep in the loaded window, levels rebuilt from the data available
  at each bar, entries filling on the signal close, stops counted before targets. It reports
  hit rate, average R, how many setups would have been **liquidated** before resolving, and the
  net result of taking every one of them at your leverage.

Data comes from **Coinbase ETH-USD first** (Binance → OKX → Hyperliquid as fallbacks). This is
deliberate: ETH-USD on Coinbase ran about 1.7 points from ETH-USDT elsewhere while this was
built — roughly 0.09%, which is most of a whole stop at these sizes. Scan the book you trade.

**Read the replay numbers before trusting the pattern.** Over a recent ~2.8-day sample the
detector's own encoding of this setup did not make money at 500× in any configuration tested,
and roughly 30% of its setups saw an adverse excursion past liquidation. That is a small sample
and a rules-based approximation of a discretionary read — but it is what the data said, and the
dashboard reports it rather than hiding it.

## Settings

| Control | Default | Notes |
|---|---|---|
| Leverage × | 500 | Never auto-adjusted. Liquidation is reported, the call is yours. |
| Margin $ | 100 | Position size is margin × leverage. |
| Taker fee % | 0.02 | Per side. Round trip at 500× costs 20% of margin. |
| Maint. margin % | 0.04 | The number that decides whether a stop is even reachable. |
| TSI long / short | 25 / 13 | TradingView defaults, shown on the −1…+1 scale. |
| TSI band | 0 (auto) | Auto-calibrates to where TSI actually reaches. Set a value to pin it. |
| Stop buffer ×ATR | 0.6 | How far beyond the sweep wick the stop sits. |
| Strictness | Normal | Loose / Normal / Strict — thresholds from measured sweep distributions. |
| Chart window | 180 bars | Display only; the replay always uses everything loaded. |

Settings persist to `localStorage` under keys of their own, so this dashboard never
touches the signal desk's configuration.

## Not financial advice

A pattern detector on live public price data. Detected setups are tendencies, not
predictions, and the replay statistics cover a small sample of recent bars. At 500×
your margin is 0.2% of the position — a 0.15% move against you is most of it. Never
risk money you cannot afford to lose.
