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
4. Keep leverage low (`MAX_LEVERAGE=3` or less) and `EXIT_STYLE=hard` for real money.
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

## What each risk setting does

- `MAX_LEVERAGE` — hard cap on leverage. **Keep this low (2–3) for real money.**
- `RISK_PER_TRADE` — fraction of equity lost if a trade hits its stop (0.01 = 1%).
- `MAX_INVEST_FRAC` / `MAX_DEPLOY_FRAC` — most margin one trade / all trades can use.
- `MAX_CONCURRENT` — how many positions open at once.
- `MIN_CONFIDENCE` — the bot won't trade below this measured confidence (default 65).
- `EXIT_STYLE` — `hard` uses real stop losses (recommended). `diamond` holds through
  drawdown and only exits on a strong reversal — higher variance, can lose more per trade.
- `DAILY_LOSS_LIMIT` — daily drawdown kill-switch.

## Honest limitations

- The strategy has **not** been proven profitable yet. The demo is where you find out.
- Backtests and live results differ (fees, funding, slippage, real fills).
- Kraken Futures availability and leverage limits depend on your jurisdiction — verify.
- Never enable withdrawal permission on the API key. Never commit your `.env`.
