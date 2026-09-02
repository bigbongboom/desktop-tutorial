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
python run.py signals           # positioning consensus in the terminal
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

## Testing it with $1,000

```bash
python run.py -c config.1k.yaml serve
```

`config.1k.yaml` runs a **forward paper test**: a simulated $1,000 account that mirrors the
roster at live prices and pays the costs a small copier really pays. It never touches an
exchange — all three live-trading locks stay off — but unlike dry-run it keeps an account,
so the run produces a P&L you can judge instead of a log you have to imagine. State is
persisted, so the test continues across restarts.

**The three costs it charges, because they decide a small account:**

| Cost | Rate | Why it matters at $1,000 |
|---|---|---|
| Taker fee | 4.5 bps | Every mirror order crosses the book |
| Slippage | 5 bps | A copier is never first to the price |
| Funding | measured live | **+11%/yr on most of these markets, and this cohort is 95% long — so you pay** |

Funding is the one people forget. At 2× gross exposure, +11% annualised funding is ~22% of
capital a year bleeding out before a single trade is right or wrong. In the first live run,
simply *opening* the book cost **$1.20 — 0.12% of capital** in fees and slippage alone.

**Copyability, not curve shape, picks the roster.** Discovery ranks on the equity curve,
which counts unrealised gains; for copying that is the wrong test. The account with the
largest realised P&L in one live run — **$2.98M** — had made it across just **six closing
orders**, and got demoted to "marginal". A 102-order trader with $485k realised and a 92%
win rate became the top pick. After research the roster is rebuilt from a gate that asks
whether a trader has *repeatedly banked money in a way a $1,000 account can reproduce*:

- at least 20 closing orders — six decisions is not a track record
- realised P&L positive — gains on paper are not gains
- not "closes winners, holds losers" — a copier inherits the losers
- most of the profit realised, not unrealised — you would be buying it at today's price
- the book expressible at $1,000 (Hyperliquid rejects orders under $10)

On live data 8 of 14 researched accounts cleared it.

**Why `max_leverage: 5` and not 20.** You asked for higher leverage, and the preset caps it
lower than you might expect. At $1,000 and 20×, a 5% adverse move is a liquidation — and
you enter *after* the leader, so you carry their drawdown from a worse price. The paper
account simulates liquidation, so raising it is a decision you can test rather than
discover with real money. Raise `risk.max_leverage` and watch what happens first.

## Where the tracked traders are positioned

The panel at the top of the dashboard (`python run.py signals` in the terminal) ranks coins
by how strongly the researched accounts agree, weighted by how well each of them has
actually done **on that side**.

**It is built on live positioning, not on "today's trades" — for a measured reason.** The
obvious version is "what did the good traders buy today", and that question usually has no
answer. Sampling 16 well-scored accounts: only 3 had traded in the last 24 hours, the median
account had not traded for 4.5 days, and at 04:00 UTC a calendar-day window returned zero
fills for every one of them. A signal resting on three accounts — or on none — is worse than
no signal. So current positioning is the primary reading (live for every account, always),
and recent flow is secondary, over a window that widens 24h → 72h → 7d until enough accounts
are inside it. Every result states the window it used and how many accounts were in it.

Each coin shows: how many accounts hold it and which way, their agreement, their average
position as a share of *their own* equity, a **track-record weight** for that side, and net
flow in the window. Ranking is multiplicative — one whale with a huge position cannot
outrank four good traders who agree.

Two honesty features do real work here:

- **The cohort warning.** On live data, 95% of every position these accounts held was long.
  That is a property of *who got selected*, not of the market: discovery ranks accounts on
  realised profit, so in a rising market it picks long-biased traders and their "consensus"
  is long almost by construction. The page says so above the table.
- **"Crowded, but not evidence."** HYPE was held long by 10 of the tracked accounts — the
  broadest agreement on the board — by accounts whose measured record on that side was poor
  (0.33). Breadth and skill are shown separately so a crowd cannot masquerade as a signal.

It is not a forecast and not advice. You would be entering after they did, at a worse price,
paying your own fees and funding, and these accounts can be wrong together.

## Trader research — the leaderboard

`python run.py research` (or the **Research traders** button) reads each account's public
fills and works out what they actually do. Every row on the leaderboard clicks through to a
full breakdown.

**What gets measured**

- **Win rate by side.** Longs and shorts are scored separately, because they are usually
  different skills. One account here is 29% and −$8.5k on longs while being 41% and
  **+$118k on shorts** — a single blended win rate would have hidden that completely.
- **Profit factor, expectancy, payoff ratio, average win vs average loss, fees.**
- **A name and description** generated from behaviour: "High-Leverage BTC Two-Way Swing
  Trader", plus a sentence of measured facts. These are behavioural labels from public
  trading data - never a claim about who anybody is.
- **How they enter**, by locating every visible entry on real candles: how often they were
  with the trend, breaking out, or buying a pullback, their average RSI at entry, and how
  far from the 20-EMA they typically got in.

**The unit of a trade is one closing order.** This matters more than it sounds. One account
returned 2000 fills that were all partial pieces of just six orders; treating each fill as a
trade would report 2000 wins. Grouping by a 15-minute time window was worse - it merged
wins with losses until every group looked positive, manufacturing a 100% win rate. Grouping
by order id merges the 381 pieces of one unwind without merging two separate decisions.

**Three things the API cannot give you, stated rather than papered over:**

1. The fill feed caps at 2000 records, so every window is bounded and the page says how
   many days it covers.
2. For traders who unwind in pieces, those 2000 fills can be *entirely closes* - their
   opening fills are outside the window and paging back does not recover them. Hold time
   and entry analysis are then unavailable, and the page says so instead of inventing them.
3. Positions rarely return to flat (one account crossed zero 3 times in 2000 fills), so
   "trades" defined flat-to-flat would report almost nothing.

**Backtests test our rule, not their strategy.** Where a pattern is clear enough to write
down, it gets tested mechanically on the same market and period - entries on the next bar's
open, stop checked before target so an ambiguous bar counts as a loss. This answers "does
this pattern have an edge on its own", which is a different question from "is this trader
good". For the dip-buyer above, our reconstruction returned +8.4% on one market and −0.3%
on another while the trader themselves ran a 92% win rate; that gap is the honest answer,
and it is why the two are never presented as the same claim.

**Flags you will see:** `holds losers` marks an account whose realised record is spotless
while its open book is under water - it closes winners and keeps losers, so the losses
simply have not been taken yet. `thin` / `very thin` mark too few closing orders for the
win rate to mean much.

## Share what you see

```bash
python run.py snapshot -o desk.html
```

Writes the whole dashboard - charts, tables, feed - into one self-contained HTML file with
the data baked in. No server, no dependencies, no orders: open it from disk, email it, host
it anywhere. Handy for keeping a record of what the desk looked like at a moment.

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
