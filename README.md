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
- **Full transparency**: every signal's reading and point contribution is listed in the
  breakdown table, and every chart has an accessible data-table twin.
- Interactive SVG charts (price + EMAs + Bollinger wash, RSI, MACD) with crosshair
  tooltips, light/dark theme, auto-refresh every 60 s.

## Usage

Open `index.html` directly, serve it statically, or enable GitHub Pages on this repo.
Pick a timeframe, set your capital and risk per trade, and click an asset card.

## Disclaimer

This is an educational technical-analysis tool, **not financial advice**. Signals are
probabilistic tendencies derived from historical price and volume — they can be and
often are wrong. Never risk money you cannot afford to lose, and never take a position
without the stop the sizing math assumes.
