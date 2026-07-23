# Kraken Futures execution bot

The server-side bot that runs the Crypto Signal Desk strategy on **Kraken Futures**
(BTC / ETH / SOL). It mirrors the dashboard's engine — composite 14-signal score,
confidence gates, pre-trade backtest, Kelly-fractional sizing, whipsaw and
cooldown gates, and managed exits — and places real orders via the Kraken API.

> ⚠️ **This trades money.** Read every step. Start on the free demo. Go live only
> after a long, profitable demo run, and only with money you can afford to lose.
> The strategy is **not yet proven** — treat this as practice software first.

---

## The three safety switches

The bot is safe by default. Two flags in `.env` control everything:

| DRY_RUN | USE_DEMO | What happens |
|---------|----------|--------------|
| `true`  | `true`   | **Default.** Logs intended orders, places nothing, on demo. Zero risk. |
| `false` | `true`   | Places real orders on the **demo** (fake money). This is your practice mode. |
| `false` | `false`  | **REAL MONEY.** Only flip both off deliberately, after months of success. |

There is also a **daily loss kill-switch** (`DAILY_LOSS_LIMIT`, default 10%): if the
account drops that much in a day, the bot stops opening new trades until the next day.

---

## Step 1 — Practice on your own laptop (no server, no money)

You do **not** need a server or a Raspberry Pi to start. Run it on your own computer.

1. Install Python 3.10+ (python.org).
2. In a terminal, from this `bot/` folder:
   ```bash
   pip install -r requirements.txt
   cp .env.example .env
   ```
3. Open `.env`. Leave `DRY_RUN=true` and `USE_DEMO=true` for now. You don't even
   need API keys yet in this mode for reading public market data.
4. Run it:
   ```bash
   python trader.py
   ```
5. Watch `trader.log`. You'll see it scan and print `DRY_RUN order:` lines showing
   exactly what it *would* trade. Let it run for a while. This proves the strategy
   logic end-to-end with zero risk.

### Watch it live in your browser

When the bot starts it also opens a **local dashboard**. Look for this line in the
console:

```
*** WATCH THE BOT LIVE IN YOUR BROWSER:  http://localhost:8899  ***
```

Open that address. You'll see, updating every few seconds:

- **Balance, win rate, realized R** and every **open position** with its live
  unrealized profit, leverage, margin and risk.
- **The live scan** — every chart (BTC/ETH/SOL × each timeframe) with its current
  score, regime and confidence, and for anything it *isn't* trading, the exact gate
  that blocked it (e.g. "confidence 58%, needs 65%"). No more guessing why it's quiet.
- **TradingView charts** of the markets it's analysing.

The page is served only on your own computer (`localhost`) — it is never exposed to
the internet, and your API keys are never shown on it or sent anywhere. Set
`WEB_ENABLED=false` in `.env` to turn it off, or change `WEB_PORT` if 8899 is taken.

## Step 2 — Paper-trade on the Kraken demo (fake money, real fills)

1. Go to **https://demo-futures.kraken.com**, sign up (free), and get demo funds.
2. There, create an **API key** (Settings → API Keys) with **trading** permission.
3. Put those keys in `.env` as `KRAKEN_API_KEY` / `KRAKEN_API_SECRET`.
4. Set `DRY_RUN=false`, keep `USE_DEMO=true`.
5. Run `python trader.py` again. Now it places real orders on the demo with fake
   money. **Let this run for weeks.** This is the real test of whether it makes money.

## Step 3 — Go live (only when the demo has genuinely proven itself)

1. On the **real** site (futures.kraken.com), enable Futures and fund it with an
   amount you are **completely fine losing** (start with e.g. AUD equivalent of
   US$50–100, not your savings).
2. Create a **real** API key — **trading permission only, NO withdrawals**, and
   **IP-whitelist it** to the machine running the bot.
3. Put the real keys in `.env`, set `USE_DEMO=false` and `DRY_RUN=false`.
4. **For real money, set `MAX_LEVERAGE=3`** (a global hard cap) and keep
   `EXIT_STYLE=hard`. The default sizing matches the dashboard and is aggressive.
5. Run it — ideally on an always-on server (below), not your laptop.

---

## Running it 24/7 (a cheap cloud server)

A `$5/month` VPS (Hetzner, DigitalOcean, Vultr) is more reliable than a Raspberry
Pi for live trading. Once you have one:

```bash
# on the server (Ubuntu):
sudo apt update && sudo apt install -y python3-pip
git clone <your repo> && cd desktop-tutorial/bot
pip install -r requirements.txt
cp .env.example .env   # then edit .env with your keys/settings
# keep it running after you log out:
nohup python3 trader.py &        # simplest
# (better: run it as a systemd service or in tmux — ask and I'll provide the unit file)
```

---

## How it sizes trades (same as the dashboard)

By default the bot sizes **exactly like the dashboard's AI account** — not a flat 1%:

1. **Margin** — it deploys **10%→80% of equity** as margin on each trade, scaled by
   confidence and blended with Kelly (`MAX_INVEST_FRAC` caps the top).
2. **Risk** — it then solves the **leverage** so that, if the trade hits its stop, the
   loss is a confidence-scaled **~2%→4% of equity** (`RISK_MIN_FRAC`/`RISK_MAX_FRAC`),
   and **never more than 6%** (`RISK_CAP_FRAC`).
3. **Leverage ceiling** — leverage never exceeds the per-market cap in
   `MAX_LEVERAGE_MAP` (**BTC 40× / ETH 25× / SOL 20× / HYPE 5×**). It uses the *least*
   leverage needed to hit the risk band; the cap only binds on very tight stops.

> ⚠️ **This is aggressive.** 2–4% risk with up-to-40× leverage grows a demo account
> fast but can also draw down hard on a losing streak. It's tuned to match the
> dashboard you built. **For real money, set `MAX_LEVERAGE=3`** (a global hard cap over
> every coin) and consider lowering `RISK_MAX_FRAC` — the per-market map stays, but no
> trade will use more than 3× leverage.

### Every setting

- `MAX_LEVERAGE_MAP` — per-coin leverage ceiling, e.g. `BTC:40,ETH:25,SOL:20,HYPE:5`.
- `MAX_LEVERAGE` — `0` trusts the map; `>0` is an extra **global hard cap** over all
  coins. **Set this to 3 or less for real money.**
- `RISK_MIN_FRAC` / `RISK_MAX_FRAC` — the risk-per-trade band (2%→4%), scaled by confidence.
- `RISK_CAP_FRAC` — absolute ceiling on single-trade risk (6%).
- `MAX_INVEST_FRAC` / `MAX_DEPLOY_FRAC` — most margin one trade / all trades can use.
- `MAX_CONCURRENT` — how many positions open at once.
- `TRIGGER` — `standard` trades regular signals (score ≥18, the balanced default);
  `strong` only takes very strong ones (≥45) — far fewer trades, highest quality.
- `MIN_CONFIDENCE` — the bot won't trade below this measured confidence (default 63).
  Lower it (≈58) to trade more, raise it (≈68) to be pickier.
- `EXIT_STYLE` — `hard` uses real stop losses (recommended). `diamond` holds through
  drawdown and only exits on a strong reversal — higher variance, can lose more per trade.
- `DAILY_LOSS_LIMIT` — daily drawdown kill-switch.

## Honest limitations

- The strategy has **not** been proven profitable yet. The demo is where you find out.
- Backtests and live results differ (fees, funding, slippage, real fills).
- Kraken Futures availability and leverage limits depend on your jurisdiction — verify.
- Never enable withdrawal permission on the API key. Never commit your `.env`.
