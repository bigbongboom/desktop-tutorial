# Crypto Signal Desk

A self-contained crypto analysis dashboard for **Bitcoin, Ethereum, Solana and Hyperliquid (HYPE)**.
One file, zero dependencies — open `index.html` in any browser.

## What it does

- **Live market data** with automatic failover: Binance → OKX → Hyperliquid's native API
  (HYPE uses Hyperliquid first). If every feed is unreachable it falls back to clearly
  badged simulated data so the desk still demonstrates itself offline.
- **Twelve-component signal engine** scored into one long/short number (−100…+100):
  trend structure (price vs EMA200, EMA20/50/200 alignment, EMA slope), momentum
  (RSI 14, MACD cross, MACD histogram momentum, 10-bar rate of change), mean reversion
  (Bollinger %B, Stochastic 14/3) and volume (OBV flow, volume thrust).
- **Clear verdicts** — STRONG LONG / LONG / NEUTRAL / SHORT / STRONG SHORT — per asset,
  per timeframe (15m / 1h / 4h / 1D), plus a multi-timeframe consensus view.
- **ATR-anchored trade plan**: entry, stop (1.5×ATR), target (2.5–3.5×ATR), reward:risk,
  an expected hold window, and fixed-fractional position sizing from your capital and
  risk budget (0.5–2% per trade, exposure capped at 5×).
- **Market pulse** — the data professional desks watch: perpetual funding rates and open
  interest (crowd positioning, via Hyperliquid) and the Fear & Greed index, kept out of
  the backtested score on purpose so the accuracy numbers stay honest.
- **Backtester** — replays the exact engine bar-by-bar with zero lookahead (entries on
  the next bar's open, stop counted before target): win rate, profit factor, expectancy,
  max drawdown, equity curve, and a per-timeframe comparison vs buy & hold.
- **Paper trading with an editable trade ticket** — customize size, leverage (1–10×,
  with risk warnings), stop and target before placing, with live notional/margin/risk
  math and validation. Stops/targets settle against candle highs/lows; no exchange keys
  ever touch the page; one-click links execute for real on Hyperliquid or Binance.
- **Portfolio portal** — a dedicated tab with paper equity, unrealized/realized P&L,
  every open and closed trade at live prices (auto-refreshed every 60 s), and in-row
  editing of an open trade's stop and target.
- **Signal alerts** — opt-in browser notifications when a LONG or SHORT fires, including
  entry price, stop and target (the tab must stay open — a static page cannot push to a
  closed browser). Entry/stop/target are also drawn on the price chart.
- **Crypto news feed** — latest headlines (CryptoCompare) tagged by asset, because a
  story can invalidate any technical setup.
- **Key levels** — swing support/resistance drawn on the price chart and folded into
  the trade plan's profit-taking notes.
- **Full transparency**: every signal's reading and point contribution is listed in the
  breakdown table, and every chart has an accessible data-table twin.
- **Live trading view** — TradingView-style candlestick chart (down to 1-minute bars)
  with a position overlay: green zone from entry to take-profit, red zone from entry to
  stop-loss, with SL/TP price and percentage labels; shows your open paper trade or the
  engine's planned trade. Switchable to a line + indicators view.
- Interactive SVG charts (RSI, MACD) with crosshair tooltips, light/dark theme,
  auto-refresh every 60 s.

## Usage

Open `index.html` directly, serve it statically, or enable GitHub Pages on this repo.
Pick a timeframe, set your capital and risk per trade, and click an asset card.

## Disclaimer

This is an educational technical-analysis tool, **not financial advice**. Signals are
probabilistic tendencies derived from historical price and volume — they can be and
often are wrong. Never risk money you cannot afford to lose, and never take a position
without the stop the sizing math assumes.
