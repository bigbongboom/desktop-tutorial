# hyperbot — Hyperliquid copy-trading desk

Finds the traders worth following on Hyperliquid, mirrors their perpetual positions
(long **and** short) into your account at a scaled size, enforces a hard risk envelope,
and tells you everything it does.

Hyperliquid is unusual: **every account's positions, fills and equity curve are public**.
There is no copy-trading API to sign up for and no permission to request. The entire edge
is in *who* you follow and *how* you translate their book into yours — so that is where
most of this code lives.

```
                44,627 accounts (public leaderboard)
                          |
              cheap filters: size, PnL, volume
                          |
                  4,810 candidates
                          |
        +-----------------+-----------------+
        |                                   |
  elite funnel                        climber funnel
  (proven, durable)                   (small, accelerating)
        |                                   |
        +----------- deep scan -------------+
              (equity curve per account)
                          |
        TOP TRADERS                 ON THE COME UP
                          |
                  roster + allocations
                          |
              position-fraction mirroring
                          |
              risk envelope + kill switch
                          |
                  IOC orders + alerts
```

## Two rosters, not one filtered list

**TOP TRADERS** — proven, durable, survivable. Ranked on the *shape* of the equity curve:
Sharpe, Calmar, max drawdown, consistency, curve linearity, profit factor, and lifetime
PnL in dollars.

**ON THE COME UP** — small accounts compounding a real edge. Must sit inside a *climber
band* of perp capital (default $10k–$2M), still be climbing this week, and show low
drawdown with a smooth curve. Smaller accounts get a bonus that decays with size, so a
clean $50k compounder outranks a $5M account with the same percentage return.

These are **separate funnels with separate deep-scan budgets**, not one list filtered
twice. Ranking every candidate by elite criteria starves the climber roster by
construction — the scan budget goes entirely to large established accounts and
climber-band accounts are never even fetched.

## Quick start — the dashboard

**Windows:** double-click **`start.bat`**
**macOS / Linux:** run **`./start.sh`**

That is the whole setup. It finds Python, installs what is missing, creates your config on
first run, starts the server and opens the dashboard for you.

Prefer to do it by hand:

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env            # optional: notifications, and later a key

python run.py serve             # on Windows this is `python`, not `python3`
```

Then open **http://localhost:8730**.

> **`localhost` means *your own computer*.** The link only works while the server is
> running on the machine you are browsing from, and only for as long as that window stays
> open. If the page will not load, the server is not running — that is almost always all it
> is. Closing the terminal stops it.

That one command runs everything: it scans and ranks traders, picks a roster, starts the
copy engine in dry-run, and serves a live dashboard — equity chart, open positions,
target book, pending orders, both trader rosters, and a live event feed streaming over
WebSocket. It works with **no private key**; without one it simply narrates the orders it
would have placed.

```bash
python run.py serve --port 9000     # different port
python run.py serve --no-engine     # dashboard only, no trading loop
```

It binds to `127.0.0.1` **on purpose** — the UI has Rescan and Flatten buttons, so it must
not be reachable from your network. `--host` overrides that and warns you.

Set `HYPERLIQUID_ACCOUNT_ADDRESS` in `.env` to track an account; without it the dashboard
runs in discovery-only mode (rosters and rankings, no positions).

## The CLI

Everything the dashboard does is also a command, if you prefer the terminal:

```bash
python run.py scan --explain    # rank both rosters (no key needed)
python run.py leaders           # show the selected roster
python run.py preview           # target book + the orders it would place
python run.py watch             # live leader trades -> notifications, no trading
python run.py run               # the copy engine, headless (dry-run by default)
python run.py status            # your account, positions, P&L
python run.py close-all         # flatten everything, reduce-only
```

`scan`, `leaders`, `preview`, `watch` and `status` need **no private key at all** — they
read public data.

## Safety: three locks on live trading

Live orders require **all three**, and every one is off by default:

1. `dry_run: false` in `config.yaml`
2. `i_understand_live_trading_risk: true` in `config.yaml`
3. `HYPERLIQUID_PRIVATE_KEY` set in the environment

Miss any one and the engine runs as a faithful simulator: it does all the same rounding,
risk checks and reconciliation, and logs the orders it *would* have sent.

Use a Hyperliquid **API wallet** (Settings → API), not your main wallet key. An API wallet
can trade but cannot withdraw. This bot has **no withdrawal or transfer code path** of any
kind.

## The risk envelope

Evaluated before every order:

| Control | Default | On breach |
|---|---|---|
| Daily loss limit | 10% of day-start equity | Kill switch |
| Max drawdown | 25% from high-water mark | Kill switch |
| Max gross exposure | 3.0× equity | Book scaled down proportionally |
| Max position size | 50% of equity per coin | Target clamped |
| Max leverage | 5× | Capped per asset |
| Max concurrent positions | 8 | Lowest-conviction targets dropped |
| Slippage guard | 30 bps | IOC rejects rather than fills badly |
| Coin allow / deny list | empty | Target forced to zero |

The kill switch is **sticky**: once tripped it stays tripped until the UTC day rolls over
or an operator clears it. Recovering does not re-arm trading — a bot that just hit its
daily limit is a bot in an unknown state. Risk-*reducing* orders are still allowed while
it is tripped; a position flip is downgraded to a close rather than crossing zero into new
risk.

## How mirroring works

The unit of copying is a **position fraction**: what share of their own equity a leader has
committed to a coin, long or short.

```
w_leader[coin]  = signed_notional / their_equity
target_w[coin]  = Σ (allocation × w_leader[coin]) × exposure_multiplier
target_notional = target_w[coin] × your_equity        # negative = short
```

So a $10M whale's $4M ETH position becomes 40% of *your* equity, not $4M. Two leaders on
opposite sides of the same coin net off — you hold the roster's actual consensus, not both
sides at once.

The bot **reconciles toward a target** rather than mirroring fills. Copying fills desyncs
permanently the first time an order is rejected or a socket drops; converging on a target
self-heals on the next cycle. Leader fills arriving over WebSocket are only a latency
optimisation that wakes the loop early — losing the stream costs latency, never
correctness.

A deadband (default 2% of equity) suppresses churn, so the bot does not pay fees chasing
every tick of a leader's mark price.

## Notifications

Console, Telegram, Discord and generic webhooks. Severity-tagged and deduplicated.

```
INFO      engine start/stop, roster changes, scan summaries, daily digest
TRADE     leader opened/closed/flipped, our orders, our positions
WARN      slippage rejection, WS reconnect, risk clamp, leader dropped
CRITICAL  kill switch, daily loss limit, exchange auth failure
```

Set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` or `DISCORD_WEBHOOK_URL` in `.env` — channels
enable themselves when credentials are present. Verify with `python run.py notify-test`.

## What the numbers mean

Two measurement decisions matter more than anything else in the ranking, and both were
forced by what the live data actually does:

**Returns are measured on capital deployed, not compounded.** Hyperliquid reports
account value and cumulative PnL in coarse buckets (the allTime window runs ~167 hours per
point). Compounding per-period returns across those buckets breaks whenever capital moves:
one real account went from $495 to $5,000,000 by deposit *inside a single bucket* that lost
$5,219 — dividing by the opening balance reports −1053% for a week in which the trader lost
0.1%, and the compounded curve never recovers. Roughly 46% of scanned accounts scored an
allTime ROI of exactly −100% this way. Another account withdrew profits continuously, kept
~$5k of perp capital, earned $9,424, and *ended the month smaller than it started* — yet
compounding its buckets reported 281,531%. So ROI here is cumulative PnL over average
capital deployed, which is bounded by the dollars and cannot invent wealth.

**The leaderboard's own ROI is not used for ranking.** Measured against the reconstruction
it is not a return on deployed capital: over the month window the two disagree by a median
of ~900 percentage points, and the leaderboard routinely reports figures like 61,000% for
accounts that merely started the month with a tiny balance. It is a fine coarse sort key
over 44k rows, so that is all it is used for. Lifetime **PnL in dollars** is used instead
for track record — a sum, not a ratio, and immune to the whole problem.

Also worth knowing:

- **Size is measured on perp capital**, not account value. An account can show $390k of
  account value while trading $5k of perps; sizing off the $390k would copy a book the
  trader never had.
- **Curve linearity (R²)** is the best skill-vs-luck discriminator available from public
  data. A steady edge draws a near-straight line; a curve that is flat then jumps once fits
  a line badly.
- **Concentration** is the one-hit-wonder detector: the share of profit from a single
  period. An account whose whole month is one lucky bucket is *rejected*, not merely
  downweighted. Good properties are averaged; disqualifying ones are not allowed to be
  averaged away.
- **Pace ratio** replaces raw acceleration: this week's pace as a *multiple* of the
  month's. A raw difference is meaningless across this population — an account up 1200% on
  the month shows an "acceleration" of −500 points while still compounding beautifully.

## If the dashboard will not load

| What you see | What it means |
|---|---|
| "This site can't be reached" / connection refused | The server is not running. Start `start.bat` / `./start.sh` and leave that window open. |
| `'python' is not recognized` (Windows) | Python is not on your PATH. Reinstall from python.org and tick **"Add python.exe to PATH"**. |
| `python3: command not found` (Windows) | On Windows the command is `python` or `py -3`, not `python3`. |
| `ModuleNotFoundError: No module named 'aiohttp'` | Dependencies are missing: `pip install -r requirements.txt`. |
| `address already in use` | Something already holds the port: `python run.py serve --port 9000`. |
| Page loads but everything is empty | Normal on the very first run - the opening scan takes ~30s. Watch the terminal. |
| Page loads but no positions | No `HYPERLIQUID_ACCOUNT_ADDRESS` in `.env`, so it runs discovery-only. |

The terminal running the server prints the real error. If something is wrong, that is where
it says so.

## Tests

```bash
python -m pytest tests/ -q      # 62 tests
```

They cover the maths that the rankings and the orders depend on: the capital-base
denominator (with the real $495 → $5M deposit case), ROI staying bounded by dollars, the
Hyperliquid price/size rounding rules, position blending and every clamp, all five
reconciliation transitions, and kill-switch stickiness.

## Honest limits

- **Copying a profitable trader is not the same as being one.** You inherit their drawdowns
  on a delay, you pay your own fees and funding, and you enter after they do.
- Leaderboard performance is heavily **survivorship-biased**: 44k accounts, and the ones at
  the top this month are partly the ones that got lucky this month.
- There is **no backtest of copy performance**, deliberately. Hyperliquid's public
  per-account history is not granular enough to backtest honestly, and a fake backtest is
  worse than none.
- Start on testnet, then start small. `exposure_multiplier` above 1.0 means you take *more*
  risk than the traders you are copying.

`SPEC.md` has the full engineering spec.
