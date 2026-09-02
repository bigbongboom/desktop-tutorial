# Hyperliquid Copy-Trading Desk — Engineering Spec

This file is the hardened version of the original one-line brief:

> *"connect the hyperliquid platform to a crypto trading bot that follows all the top
> traders and has another section that looks at the consistent high ROI accounts that
> are on the come up, build a full notification and trade long or short positions."*

That brief names a product but leaves every decision that determines whether the thing
makes or loses money undefined. This spec pins those decisions down. It is the contract
the code in `hyperbot/` implements.

---

## 1. What this is

A daemon that watches public Hyperliquid trader performance, selects a roster of leaders
under explicit statistical criteria, mirrors their **perpetual** positions — long *and*
short — into your own account at a scaled size, enforces a hard risk envelope, and
narrates everything it does to Telegram / Discord / console.

Hyperliquid is unusual in that **every account's positions, fills and equity curve are
public**. There is no copy-trading API to sign up for and no leader permission to
request: the entire edge is in *who* you choose to follow and *how* you translate their
book into yours. This system is therefore two products stapled together:

1. **A selection engine** (the hard part — §3, §4)
2. **A mirroring engine** (the mechanical part — §5, §6, §7)

## 2. Data sources — all public, all verified live

| Source | Endpoint | Used for |
|---|---|---|
| Leaderboard | `GET https://stats-data.hyperliquid.xyz/Mainnet/leaderboard` | Discovery universe: ~44.6k accounts with day / week / month / allTime ROI, PnL, volume |
| Account state | `POST /info {"type":"clearinghouseState","user":addr}` | Any address's live positions, entry price, leverage, margin, account value |
| Equity curve | `POST /info {"type":"portfolio","user":addr}` | Per-address account-value and PnL history across 8 windows (`day`,`week`,`month`,`allTime` × spot/perp) — the raw material for drawdown, Sharpe and consistency |
| Fills | `POST /info {"type":"userFills","user":addr}` | Realised PnL per trade, hold times, win rate |
| Asset metadata | `POST /info {"type":"metaAndAssetCtxs"}` | `szDecimals`, `maxLeverage`, mark price, funding — needed to round orders legally |
| Live stream | `wss://api.hyperliquid.xyz/ws` — `userFills`, `allMids`, `activeAssetCtx` | Sub-second leader trade detection and mark prices |

**Read/write split:** public reads always go to **mainnet** (testnet has no meaningful
traders to copy). Orders go to whichever network `network:` selects. This is deliberate
and is the same split the repo's existing Kraken bot uses in dry-run.

## 3. Selection — "top traders"

Raw leaderboard ROI is a trap. A 900% monthly ROI on a $400 account that traded once is
noise; so is a whale whose allTime PnL is large but whose returns are flat. Every
candidate is scored on its **equity curve**, not its headline number.

**Measurement first.** Two decisions here dominate everything downstream, and both were
forced by what the live data does (see README "What the numbers mean" for the full
evidence):

- **Returns are measured on capital deployed, not compounded.** `roi = cumulative PnL /
  average capital deployed`, where a period's capital base is the average of its opening
  balance and its deposit-adjusted closing balance. Compounding per-period returns across
  Hyperliquid's coarse buckets breaks whenever capital moves — it produced both −100% ROIs
  for ~46% of accounts and a 281,531% ROI for an account that ended the month smaller than
  it started.
- **The leaderboard's own ROI is not a ranking input.** It disagrees with any reconstruction
  by a median of ~900 percentage points over the month window. It is used only as a coarse
  sort key in stage 1 and for display. Lifetime **PnL in dollars** carries track record
  instead — a sum, not a ratio.
- **Size is measured on perp capital**, not account value, because perp capital is what the
  mirroring math divides by.

Metrics computed per account (`discovery/metrics.py`):

- `roi` (on deployed capital), `pnl`, `volume` per window
- **Max drawdown** — peak-to-trough on the account-value series
- **Sharpe** and **Sortino** — annualised from per-period returns, downside deviation for Sortino
- **Calmar** — window ROI ÷ max drawdown
- **Consistency** — fraction of periods with a positive return
- **Curve linearity (R²)** — least-squares fit of the equity curve vs time (linear, since the
  curve is additive). A steady edge scores near 1.0; a curve that is one vertical jump scores
  low. This is the single best discriminator between skill and luck available from public data.
- **Profit factor** — Σ gains ÷ |Σ losses|
- **Concentration** — largest single-period gain ÷ total gain. **This is the one-hit-wonder
  detector.** An account whose entire month is one lucky period is fragile no matter how
  good the ROI looks, and is penalised hard rather than ranked highly.
- **Turnover** — volume ÷ account value. Flags both wash-trading and death-by-fees.
- **Days active** — longevity floor; nothing under `min_days_active` is copyable.

`EliteScorer` blends these into 0–100 with drawdown and concentration as *penalties*, not
inputs, so a single catastrophic property cannot be averaged away by good ones.

## 4. Selection — "on the come up"

A separate scorer, not a filter on the first one. It looks for accounts that are **small,
accelerating, and smooth** — traders compounding a real edge before size slows them down.

An account qualifies as a climber when all of these hold:

- Account value inside `[min_equity, max_equity]` — the *climber band* (default $10k–$2M).
  Above the band, returns are size-constrained; below it, the sample is noise.
- **Pace ratio ≥ `rising_min_pace_ratio`** (default 0.25): `(roi_week × 30/7) / roi_month`,
  the recent pace as a *multiple* of the established pace. A raw difference is meaningless
  across this population — an account up 1200% on the month shows an "acceleration" of −500
  points while still compounding beautifully — so the gate is scale-free. 1.0 means this
  week matched the month's pace; >1 is genuinely accelerating.
- Month max drawdown ≤ `max_drawdown` (default 25%).
- Consistency ≥ `min_consistency` and curve R² ≥ `min_r_squared`.
- `days_active ≥ min_days_active` (default 21) and volume ≥ `min_volume`.
- Concentration ≤ `max_concentration` — no single period may carry the run.

`RisingScorer` then ranks survivors on pace × smoothness × growth multiple, with a
**small-account bonus** that decays with size, so a clean $50k compounder outranks a $5M
account with the same percentage return.

**Two funnels, not one.** The deep-scan budget is split (default 60/40) between an
elite-ranked candidate list and a climber-ranked one, then deduplicated. Ranking every
candidate by elite criteria starves the rising roster by construction: the budget goes
entirely to large established accounts and climber-band accounts are never fetched at all,
so no amount of scorer tuning can recover them.

## 5. Mirroring — the translation

**Position-fraction mirroring, not fill mirroring.** Copying fills one-for-one desyncs
permanently the first time a WebSocket drops or an order is rejected; the bot then holds a
book nobody chose. Instead the bot continuously computes a *target* and converges on it:

```
For each leader L with account value E_L:
    w_L[coin] = signed_notional_L[coin] / E_L          # leader's book as % of their equity

target_w[coin] = Σ_L  alloc_L · w_L[coin]              # alloc_L = normalised leader weight
target_w[coin] = clamp(target_w[coin] · exposure_multiplier,
                       ±max_position_pct)
target_notional[coin] = target_w[coin] · my_equity     # negative = short
```

Gross exposure is then scaled down proportionally if `Σ|target_notional| > max_gross_exposure ·
my_equity`. Longs and shorts are symmetric throughout — a negative target is a short, and
crossing zero is executed as a single flip order.

WebSocket fills are used only as a **low-latency trigger** to run a reconcile early; the
authoritative input is always `clearinghouseState`. Losing the stream costs latency, never
correctness.

**Deadband:** an order is emitted only when `|target − actual|` exceeds
`max(min_order_usd, deadband_pct × my_equity)`. Without this the bot churns fees on every
tick of the leader's mark price.

## 6. Risk envelope — evaluated before every order

| Control | Default | Behaviour on breach |
|---|---|---|
| `dry_run` | `true` | Orders logged, never sent |
| Daily loss limit | 10% of day-start equity | **Kill switch**: flatten optionally, block all opens until UTC rollover |
| Max drawdown limit | 25% from equity high-water mark | Kill switch |
| Max gross exposure | 3.0× equity | Targets scaled down proportionally |
| Max position size | 50% of equity per coin | Target clamped |
| Max leverage | 5× | Leverage set per-asset, capped by the asset's own max |
| Max concurrent positions | 8 | Lowest-conviction targets dropped |
| Coin allow / deny list | empty / empty | Target forced to zero |
| Min account value | $50 | Engine halts |
| Slippage guard | 30 bps | Order priced at mark ± guard, IOC — rejects rather than fills badly |

The kill switch is **sticky**: once tripped it stays tripped until the UTC day rolls over
or an operator clears it, so a bot in a bad state cannot re-enter on the next cycle.

## 7. Execution

- Aggressive **IOC limit** orders priced at mark ± `slippage_bps`. Never market orders —
  an IOC that cannot fill inside the guard is a rejection, which is the correct outcome.
- Closes and reductions carry `reduce_only`, so a reconcile can never accidentally flip a
  position it meant to shrink.
- Prices are rounded to Hyperliquid's actual rule (≤5 significant figures **and** ≤
  `6 − szDecimals` decimals; integers always legal); sizes to the asset's `szDecimals`.
  Getting this wrong is the single most common cause of silent rejection.
- Every order is written to SQLite before and after the send.

## 8. Notifications

Severity-tagged, deduplicated (same key within `cooldown_seconds` fires once), fanned out
to console, Telegram, Discord and generic webhooks.

`INFO` engine start/stop · leader roster changes · scan summaries · daily PnL digest
`TRADE` leader opened / closed / flipped · our order placed / filled / rejected · position opened / closed
`WARN` slippage rejection · WS reconnect · risk clamp applied · leader dropped for drawdown
`CRITICAL` kill switch tripped · daily loss limit · exchange auth failure

## 8b. Local dashboard

`python run.py serve` runs the engine and a web UI together on
**http://localhost:8730**: equity chart, open positions, target book vs current, pending
adjustments, both trader rosters, recent orders, and a live event feed pushed over
WebSocket by a `WebChannel` plugged into the same dispatcher as Telegram and Discord.

It binds to `127.0.0.1` by default and deliberately — the UI exposes Rescan and Flatten,
so it must not be reachable from the network unless the operator passes `--host`, which
warns. Long/short use the blue/red diverging pair rather than green/red: same polarity,
and it stays readable with colour-vision deficiency (validated CVD dE 21.6 light /
19.2 dark against an >= 8 target).

## 8c. Trader research (`hyperbot/research/`)

| Module | Job |
|---|---|
| `trades.py` | Fills -> exit events (one per closing **order**) and, where the opens are visible, flat-to-flat round trips via `startPosition` |
| `profile.py` | Win rate, profit factor, expectancy, payoff, long/short split, activity, leverage band, sample quality |
| `naming.py` | Deterministic behavioural handle + factual description |
| `candles.py` | EMA/RSI/ATR and the entry context for each visible entry |
| `strategy.py` | Aggregate fingerprint -> archetype, plus a no-lookahead backtest of the inferred rule |
| `analyst.py` | Orchestrates one account end to end into a dossier |

Three measured constraints drive the design, all verified on live accounts:

- The fill feed caps at **2000 records**. Every statistic is window-bounded and says so.
- That window can be **entirely closes** - one account returned 2000/2000 "Close Long" with
  its opens outside the window, unrecoverable by paging. Hold time and entry analysis are
  therefore optional and gated on `coverage`, never faked.
- Positions **rarely return to flat** (3 crossings in 2000 fills on one account), so
  flat-to-flat cannot be the definition of a trade.

Hence the unit of realisation is the **closing order**. Grouping by fill would report 2000
wins for six decisions; grouping by a time window merged wins with losses and manufactured a
100% win rate. Settlement and spot legs carry real P&L but are not trading decisions, so
they are excluded from win rate and reported separately - the totals reconcile exactly
against the raw feed.

Backtests evaluate **our reconstruction** of an inferred pattern, never the trader's
strategy, and the UI states that where the numbers appear.

## 9. Safety locks on live trading

Live orders require **three independent switches**, all off by default:

1. `dry_run: false` in config
2. `i_understand_live_trading_risk: true` in config
3. `HYPERLIQUID_PRIVATE_KEY` present in the environment

Missing any one of them and the engine runs as a simulator. The private key is read from
the environment only — never from config, never logged, never committed. Use a Hyperliquid
**API wallet** (Settings → API), not your main wallet key: an API wallet can trade but
cannot withdraw.

## 10. Explicit non-goals

- No withdrawal, transfer or vault operations. The bot has no code path that moves funds off
  the account.
- No backtest of copy performance. Hyperliquid's public history is not deep or granular
  enough per-account to backtest honestly, and a fake backtest is worse than none.
- No reliance on any single window. A window whose reconstruction hit its backstop is marked
  unreliable and rejected rather than scored on artefacts.
- No promise of profit. Copying a profitable trader is not the same as being one: you inherit
  their drawdowns on a delay, you pay your own fees and funding, and past ROI on a 44k-account
  leaderboard is heavily survivorship-biased. Size accordingly.
