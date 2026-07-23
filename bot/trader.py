#!/usr/bin/env python3
"""
Crypto Signal Desk — Kraken Futures execution bot
=================================================

This is the SERVER-SIDE execution bot that runs the same strategy as the
dashboard, but places real orders on Kraken Futures. It mirrors the dashboard's
engine: a composite trend/momentum score, confidence gates, a pre-trade
backtest, Kelly-fractional sizing, and managed exits.

SIZING — mirrors the dashboard's AI account exactly:
  * Deploys 10%->80% of equity as MARGIN, scaled by confidence + Kelly.
  * Solves leverage per trade so the loss-if-stopped lands on a confidence-scaled
    risk band (~2%->4% of equity, hard cap 6%) — NOT a flat 1%.
  * Per-market leverage ceiling: BTC 40x / ETH 25x / SOL 20x / HYPE 5x (MAX_LEVERAGE_MAP).

SAFETY-FIRST DEFAULTS:
  * DRY_RUN = True         -> logs intended orders, places NOTHING
  * USE_DEMO = True        -> Kraken Futures DEMO/testnet (fake money)
  * EXIT_STYLE = "hard"    -> real stop losses (NOT diamond-hands) by default
  * MAX_LEVERAGE = 0       -> 0 = use per-market map; set >0 as a global hard cap
  * DAILY_LOSS_LIMIT = 0.10 -> kill-switch: stop trading after -10% in a day

Go live only after: (1) weeks of profitable DEMO trading, (2) you flip DRY_RUN
and USE_DEMO off deliberately, (3) you fund with money you can afford to lose.

The bot NEVER needs withdrawal permission on your API key. Create the key with
trading only, and IP-whitelist it to this machine.
"""
import os
import time
import json
import math
import logging
from datetime import datetime, timezone

try:
    import ccxt
except ImportError:
    ccxt = None   # engine/backtest work without it; the Exchange class needs it at runtime

# --------------------------------------------------------------------------
# Configuration (override via environment variables / a .env file)
# --------------------------------------------------------------------------
def _load_env_file(path=".env"):
    """Minimal .env loader (no extra dependency) so KEY=value lines are picked up."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass

_load_env_file()

def _bool(name, default):
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")

def _num(name, default):
    v = os.getenv(name)
    try:
        return float(v) if v is not None else default
    except ValueError:
        return default

# Per-market EXCHANGE max leverage — mirrors the dashboard's maxLevFor().
# The bot solves the *actual* leverage per trade to hit the risk band below; this
# is only the ceiling it will never exceed for each coin.
DEFAULT_MAX_LEV = {"BTC": 40, "ETH": 25, "SOL": 20, "HYPE": 5}

def _parse_lev_map(s):
    m = dict(DEFAULT_MAX_LEV)
    for part in (s or "").split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            try:
                m[k.strip().upper()] = float(v)
            except ValueError:
                pass
    return m

CFG = {
    "DRY_RUN":         _bool("DRY_RUN", True),          # place no real orders
    "USE_DEMO":        _bool("USE_DEMO", True),         # Kraken Futures demo/testnet
    "SYMBOLS":         os.getenv("SYMBOLS", "BTC,ETH,SOL").split(","),  # base coins; resolved to real perp symbols
    "TIMEFRAMES":      os.getenv("TIMEFRAMES", "15m,1h,4h,1d").split(","),  # NEVER 1m — noise
    "POLL_SECONDS":    int(_num("POLL_SECONDS", 60)),
    "MIN_CONFIDENCE":  _num("MIN_CONFIDENCE", 65),      # confidence floor
    "ENTER_THRESHOLD": _num("ENTER_THRESHOLD", 18),
    "STRONG_THRESHOLD":_num("STRONG_THRESHOLD", 45),
    "TRIGGER":         os.getenv("TRIGGER", "strong"),  # "strong" or "standard"
    "EXIT_STYLE":      os.getenv("EXIT_STYLE", "hard"), # "hard" (stops) or "diamond"
    # Leverage: per-market ceiling (BTC 40x / ETH 25x / SOL 20x / HYPE 5x) — same as
    # the dashboard. MAX_LEVERAGE>0 acts as an extra hard cap over ALL markets (for
    # cautious real-money use). 0 = trust the per-market map.
    "MAX_LEVERAGE_MAP":_parse_lev_map(os.getenv("MAX_LEVERAGE_MAP", "")),
    "MAX_LEVERAGE":    _num("MAX_LEVERAGE", 0),
    # Risk band (fraction of equity lost if the trade hits its stop). The dashboard
    # scales this ~2%->4% by confidence and never lets a single trade risk past 6%.
    "RISK_MIN_FRAC":   _num("RISK_MIN_FRAC", 0.02),
    "RISK_MAX_FRAC":   _num("RISK_MAX_FRAC", 0.04),
    "RISK_CAP_FRAC":   _num("RISK_CAP_FRAC", 0.06),
    "MAX_INVEST_FRAC": _num("MAX_INVEST_FRAC", 0.80),   # max margin per trade (of equity)
    "MAX_DEPLOY_FRAC": _num("MAX_DEPLOY_FRAC", 0.80),   # max total margin deployed
    "MAX_CONCURRENT":  int(_num("MAX_CONCURRENT", 2)),
    "MIN_NOTIONAL":    _num("MIN_NOTIONAL", 10),
    "STOP_ATR":        _num("STOP_ATR", 1.5),
    "TARGET_ATR":      _num("TARGET_ATR", 2.5),
    "DAILY_LOSS_LIMIT":_num("DAILY_LOSS_LIMIT", 0.10),  # kill-switch
    "STATE_FILE":      os.getenv("STATE_FILE", "bot_state.json"),
    "WEB_ENABLED":     _bool("WEB_ENABLED", True),      # serve the local watch page
    "WEB_HOST":        os.getenv("WEB_HOST", "127.0.0.1"),  # localhost only — never exposed
    "WEB_PORT":        int(_num("WEB_PORT", 8899)),  # 8787 avoided (construction AI uses it)
}

# Live snapshot the local web page reads (never persisted, never leaves the laptop).
LATEST = {"status": None, "analysis": [], "closest": None, "scan_ts": None}

# Make the console tolerate any character on Windows (cp1252 can't draw some).
try:
    import sys as _sys
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("trader.log", encoding="utf-8")],
)
log = logging.getLogger("trader")

# --------------------------------------------------------------------------
# Indicators — faithful ports of the dashboard engine
# --------------------------------------------------------------------------
def ema(v, p):
    out = [None] * len(v)
    k = 2 / (p + 1)
    s = 0.0
    for i, x in enumerate(v):
        s += x
        if i == p - 1:
            out[i] = s / p
        elif i >= p:
            out[i] = x * k + out[i - 1] * (1 - k)
    return out

def sma(v, p):
    out = [None] * len(v)
    s = 0.0
    for i, x in enumerate(v):
        s += x
        if i >= p:
            s -= v[i - p]
        if i >= p - 1:
            out[i] = s / p
    return out

def rsi(v, p=14):
    out = [None] * len(v)
    if len(v) <= p:
        return out
    ag = al = 0.0
    for i in range(1, p + 1):
        d = v[i] - v[i - 1]
        ag += max(d, 0); al += max(-d, 0)
    ag /= p; al /= p
    out[p] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(p + 1, len(v)):
        d = v[i] - v[i - 1]
        ag = (ag * (p - 1) + max(d, 0)) / p
        al = (al * (p - 1) + max(-d, 0)) / p
        out[i] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    return out

def macd(v, fast=12, slow=26, sig=9):
    ef, es = ema(v, fast), ema(v, slow)
    line = [ (ef[i] - es[i]) if (ef[i] is not None and es[i] is not None) else None for i in range(len(v)) ]
    first = next((i for i, x in enumerate(line) if x is not None), len(v))
    sig_valid = ema([x for x in line[first:]], sig)
    signal = [None] * len(v)
    for i, x in enumerate(sig_valid):
        if x is not None:
            signal[first + i] = x
    hist = [ (line[i] - signal[i]) if (line[i] is not None and signal[i] is not None) else None for i in range(len(v)) ]
    return line, signal, hist

def bollinger(v, p=20, mult=2):
    mid = sma(v, p)
    up, lo = [None] * len(v), [None] * len(v)
    for i in range(p - 1, len(v)):
        m = mid[i]
        s = sum((v[j] - m) ** 2 for j in range(i - p + 1, i + 1)) / p
        sd = math.sqrt(s)
        up[i] = m + mult * sd; lo[i] = m - mult * sd
    return mid, up, lo

def atr(c, p=14):
    out = [None] * len(c)
    if len(c) <= p:
        return out
    acc = 0.0
    for i in range(1, p + 1):
        h, l, pc = c[i]["h"], c[i]["l"], c[i - 1]["c"]
        acc += max(h - l, abs(h - pc), abs(l - pc))
    prev = acc / p
    out[p] = prev
    for i in range(p + 1, len(c)):
        h, l, pc = c[i]["h"], c[i]["l"], c[i - 1]["c"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        prev = (prev * (p - 1) + tr) / p
        out[i] = prev
    return out

def stochastic(c, p=14):
    k = [None] * len(c)
    for i in range(p - 1, len(c)):
        hi = max(c[j]["h"] for j in range(i - p + 1, i + 1))
        lo = min(c[j]["l"] for j in range(i - p + 1, i + 1))
        k[i] = 50 if hi == lo else (c[i]["c"] - lo) / (hi - lo) * 100
    return k

def obv(c):
    out = [0.0] * len(c)
    for i in range(1, len(c)):
        d = 1 if c[i]["c"] > c[i - 1]["c"] else -1 if c[i]["c"] < c[i - 1]["c"] else 0
        out[i] = out[i - 1] + d * c[i]["v"]
    return out

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

# --------------------------------------------------------------------------
# The engine — composite 14-signal score, mirrors the dashboard
# --------------------------------------------------------------------------
def analyze(candles):
    closes = [c["c"] for c in candles]
    i = len(candles) - 1
    price = closes[i]
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    r = rsi(closes, 14)
    line, signal, hist = macd(closes)
    mid, bbu, bbl = bollinger(closes, 20, 2)
    a = atr(candles, 14)
    stk = stochastic(candles, 14)
    ob = obv(candles)
    vsma = sma([c["v"] for c in candles], 20)
    atr_now = a[i] if a[i] else price * 0.01
    atr_pct = atr_now / price * 100
    pts = []
    def add(v):
        pts.append(clamp(v, -14, 14))

    add(10 if (e200[i] is None or price >= e200[i]) else -10)
    add(0 if (e20[i] is None or e50[i] is None) else (10 if e20[i] >= e50[i] else -10))
    add(0 if (e50[i] is None or e200[i] is None) else (8 if e50[i] >= e200[i] else -8))
    if e20[i] is not None and e20[i - 5] is not None:
        slope = (e20[i] - e20[i - 5]) / e20[i - 5] * 100
        add(clamp(slope / max(atr_pct * 0.5, 0.05), -1, 1) * 7)
    else:
        add(0)
    if r[i] is not None:
        v = clamp((r[i] - 50) / 25, -1, 1) * 12
        if r[i] > 78 or r[i] < 22:
            v *= 0.3
        add(v)
    else:
        add(0)
    if line[i] is not None and signal[i] is not None:
        add(clamp((line[i] - signal[i]) / (0.5 * atr_now), -1, 1) * 12)
    else:
        add(0)
    if hist[i] is not None and hist[i - 3] is not None:
        add(clamp((hist[i] - hist[i - 3]) / (0.3 * atr_now), -1, 1) * 6)
    else:
        add(0)
    if closes[i - 10] is not None:
        roc = (price - closes[i - 10]) / closes[i - 10] * 100
        add(clamp(roc / (2 * atr_pct), -1, 1) * 5)
    else:
        add(0)
    if bbu[i] is not None and bbu[i] != bbl[i]:
        pb = (price - bbl[i]) / (bbu[i] - bbl[i])
        if pb > 0.95:
            add(-6)
        elif pb < 0.05:
            add(6)
        else:
            add((pb - 0.5) * 8)
    else:
        add(0)
    if stk[i] is not None:
        add(5 if stk[i] < 80 and stk[i] > 20 else (2 if stk[i] <= 20 else -2))
    else:
        add(0)
    if i >= 10:
        obv_slope = ob[i] - ob[i - 10]
        pv = 8 if obv_slope >= 0 else -8
        if (obv_slope >= 0) != (price - closes[i - 10] >= 0):
            pv *= 0.5
        add(pv)
    else:
        add(0)
    if vsma[i] and vsma[i] > 0:
        ratio = candles[i]["v"] / vsma[i]
        d = 1 if candles[i]["c"] >= candles[i]["o"] else -1
        add(d * min((ratio - 1) * 7, 7) if ratio > 1.3 else 0)
    else:
        add(0)
    # breakout(20)
    if i >= 20:
        hi = max(candles[j]["h"] for j in range(i - 20, i))
        lo = min(candles[j]["l"] for j in range(i - 20, i))
        add(8 if price > hi else (-8 if price < lo else 0))
    else:
        add(0)
    # rsi divergence (simplified)
    add(0)

    score = round(clamp(sum(pts), -100, 100))
    bulls = sum(1 for p in pts if p > 1)
    bears = sum(1 for p in pts if p < -1)
    return {"price": price, "score": score, "atr": atr_now, "atr_pct": atr_pct,
            "e20": e20, "e50": e50, "e200": e200, "rsi": r, "bbu": bbu, "bbl": bbl,
            "bulls": bulls, "bears": bears}

def regime_of(an):
    i = len(an["e20"]) - 1
    if an["e20"][i] and an["e50"][i] and an["atr"] > 0:
        ts = abs(an["e20"][i] - an["e50"][i]) / an["atr"]
    else:
        ts = 0
    return "trend" if ts >= 1 else "mixed" if ts >= 0.5 else "chop"

def signal_stability(closes, e20):
    i = len(closes) - 1
    crossings = 0
    for k in range(i - 11, i + 1):
        if k < 1 or e20[k] is None or e20[k - 1] is None:
            continue
        a = closes[k - 1] - e20[k - 1]; b = closes[k] - e20[k]
        if a != 0 and b != 0 and (a > 0) != (b > 0):
            crossings += 1
    return crossings

def backtest(candles):
    """Replay the composite strategy over history for the pre-trade edge gate."""
    warm = 210
    trades = []
    pos = None
    for i in range(warm, len(candles) - 1):
        if pos:
            c = candles[i]
            exit_px = None; res = None
            if (c["l"] <= pos["stop"]) if pos["dir"] > 0 else (c["h"] >= pos["stop"]):
                exit_px, res = pos["stop"], "sl"
            elif (c["h"] >= pos["tgt"]) if pos["dir"] > 0 else (c["l"] <= pos["tgt"]):
                exit_px, res = pos["tgt"], "tp"
            elif i - pos["open_i"] >= pos["max_bars"]:
                exit_px, res = c["c"], "time"
            if exit_px is not None:
                trades.append(pos["dir"] * (exit_px - pos["entry"]) / pos["risk"])
                pos = None
            continue
        sub = candles[: i + 1]
        an = analyze(sub)
        need = CFG["STRONG_THRESHOLD"] if CFG["TRIGGER"] == "strong" else CFG["ENTER_THRESHOLD"]
        if abs(an["score"]) < need:
            continue
        d = 1 if an["score"] > 0 else -1
        atr_now = an["atr"]
        entry = candles[i + 1]["o"]
        stop_d = CFG["STOP_ATR"] * atr_now
        tgt_d = CFG["TARGET_ATR"] * atr_now
        pos = {"dir": d, "entry": entry, "stop": entry - d * stop_d, "tgt": entry + d * tgt_d,
               "risk": stop_d, "open_i": i + 1, "max_bars": math.ceil(tgt_d / (0.65 * atr_now) * 1.7)}
    n = len(trades)
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    gw = sum(wins); gl = abs(sum(losses))
    return {"n": n, "win_rate": (len(wins) / n * 100) if n else 0,
            "avg_r": ((gw - gl) / n) if n else 0,
            "profit_factor": (gw / gl) if gl > 0 else (999 if gw > 0 else 0)}

def tv_rating(candles):
    """TradingView-style consensus (12 MAs + 6 oscillators) — a faithful port of the
    dashboard's tvRating. Returns a -1..+1 score (buy minus sell, over total votes)."""
    closes = [c["c"] for c in candles]
    i = len(closes) - 1
    price = closes[i]
    buy = sell = total = 0
    def vote(b, s):
        nonlocal buy, sell, total
        total += 1
        if b: buy += 1
        elif s: sell += 1
    for p in (10, 20, 30, 50, 100, 200):
        if i + 1 >= p:
            avg = sum(closes[i - p + 1:i + 1]) / p
            vote(price > avg, price < avg)
            e = ema(closes, p)[i]
            if e is not None:
                vote(price > e, price < e)
    r = rsi(closes, 14)[i]
    if r is not None: vote(r < 30, r > 70)
    stk = stochastic(candles, 14)
    if stk[i] is not None: vote(stk[i] < 20, stk[i] > 80)
    line, signal, _ = macd(closes)
    if line[i] is not None and signal[i] is not None:
        vote(line[i] > signal[i], line[i] < signal[i])
    if i >= 10: vote(closes[i] > closes[i - 10], closes[i] < closes[i - 10])
    if i >= 19:
        n = 20
        tp = [(candles[k]["h"] + candles[k]["l"] + candles[k]["c"]) / 3 for k in range(i - n + 1, i + 1)]
        mean = sum(tp) / n
        md = sum(abs(x - mean) for x in tp) / n
        cci = (tp[-1] - mean) / (0.015 * md) if md > 0 else 0
        vote(cci < -100, cci > 100)
    if i >= 13:
        hh = max(candles[k]["h"] for k in range(i - 13, i + 1))
        ll = min(candles[k]["l"] for k in range(i - 13, i + 1))
        wr = ((hh - price) / (hh - ll)) * -100 if hh > ll else -50
        vote(wr < -80, wr > -20)
    return {"score": (buy - sell) / total if total else 0, "buy": buy, "sell": sell, "total": total}

def confidence(an, bt, trend_strength, agree, tv_pts):
    """Measured confidence — mirrors the dashboard's conviction formula EXACTLY so the
    bot clears the same 65 floor the dashboard does (the old version was missing the
    alignment and TV terms, which is why the bot almost never traded)."""
    conf = 25
    conf += min(abs(an["score"]), 70) * 0.35            # signal strength
    if bt["n"]:                                          # replayed edge on this chart
        shrink = bt["n"] / (bt["n"] + 10)
        conf += clamp(clamp(bt["avg_r"], -1, 1) * shrink * 30, -15, 15)
    conf += min(agree, 3) * 5                            # timeframe alignment (up to +15)
    conf += clamp(trend_strength, 0, 1.5) * 8           # regime clarity (up to +12)
    conf += tv_pts                                       # TradingView consensus (±12)
    return round(clamp(conf, 5, 95))

def max_lev_for(symbol):
    """Exchange leverage ceiling for this market's base coin (BTC 40x / ETH 25x /
    SOL 20x / HYPE 5x by default), optionally tightened by a global MAX_LEVERAGE."""
    base = symbol.split("/")[0].split(":")[0].upper().replace("XBT", "BTC")
    cap = CFG["MAX_LEVERAGE_MAP"].get(base, 5)
    if CFG["MAX_LEVERAGE"] and CFG["MAX_LEVERAGE"] > 0:
        cap = min(cap, CFG["MAX_LEVERAGE"])
    return max(1, cap)

def conviction_risk_frac(conf):
    """Risk-per-trade as a fraction of equity, scaled by confidence — mirrors the
    dashboard's convictionRisk: ~2% at the confidence floor up to ~4% when very
    confident, hard-capped at 6%."""
    conf_norm = clamp((conf - CFG["MIN_CONFIDENCE"]) / max(95 - CFG["MIN_CONFIDENCE"], 1), 0, 1)
    band = CFG["RISK_MIN_FRAC"] + conf_norm * (CFG["RISK_MAX_FRAC"] - CFG["RISK_MIN_FRAC"])
    return clamp(band, CFG["RISK_MIN_FRAC"], CFG["RISK_CAP_FRAC"])

def kelly_fraction(conf, journal):
    """Margin to deploy as a fraction of equity (the dashboard's convictionInvest):
    Kelly-blended, 10% at the confidence floor scaling toward 80% when very
    confident and the track record supports it."""
    conf_norm = clamp((conf - 50) / 45, 0, 1)
    conf_bet = 0.10 + conf_norm * 0.70
    closed = [t for t in journal if t.get("r") is not None]
    if len(closed) < 8:
        return clamp(conf_bet, 0.10, CFG["MAX_INVEST_FRAC"])
    wins = [t for t in closed if t["r"] > 0]
    losses = [t for t in closed if t["r"] <= 0]
    W = len(wins) / len(closed)
    aw = (sum(t["r"] for t in wins) / len(wins)) if wins else 0
    al = abs(sum(t["r"] for t in losses) / len(losses)) if losses else 1
    R = aw / al if al > 0 else aw
    kelly = clamp((W - (1 - W) / R if R > 0 else 0) * 0.5, 0, CFG["MAX_DEPLOY_FRAC"])
    sized = kelly * (0.6 + conf_norm * 0.8)
    return clamp(0.6 * sized + 0.4 * conf_bet, 0.10, CFG["MAX_INVEST_FRAC"])

# --------------------------------------------------------------------------
# Exchange wrapper (Kraken Futures via ccxt)
# --------------------------------------------------------------------------
class Exchange:
    def __init__(self):
        if ccxt is None:
            raise SystemExit("Install dependencies first:  pip install -r requirements.txt")
        self.ex = ccxt.krakenfutures({
            "apiKey": os.getenv("KRAKEN_API_KEY", ""),
            "secret": os.getenv("KRAKEN_API_SECRET", ""),
            "enableRateLimit": True,
        })
        # Only use the demo endpoint when actually placing orders. In dry-run we
        # read PUBLIC market data from production (more reliable, real symbols).
        if CFG["USE_DEMO"] and not CFG["DRY_RUN"]:
            self.ex.set_sandbox_mode(True)  # Kraken Futures demo/testnet
        self.ex.load_markets()
        self.trade_symbols = self._resolve_symbols([b.strip().upper() for b in CFG["SYMBOLS"] if b.strip()])
        self._equity_warned = False

    def _resolve_symbols(self, bases):
        """Find the real linear USD perpetual symbol for each requested base coin,
        so we never hardcode an exchange-specific ticker (Kraken calls BTC 'XBT')."""
        out = []
        markets = list(self.ex.markets.values())
        for base in bases:
            aliases = {base}
            if base == "BTC":
                aliases.add("XBT")
            cands = [m for m in markets
                     if m.get("base") in aliases and m.get("swap") and m.get("active", True)]
            linear = [m for m in cands if m.get("linear")]
            pref = [m for m in linear if m.get("settle") in ("USD", "USDC")] or linear or cands
            if pref:
                out.append(pref[0]["symbol"])
                log.info("resolved %s -> %s", base, pref[0]["symbol"])
            else:
                log.warning("no perpetual market found for %s on this exchange", base)
        if not out:
            log.error("Could not resolve ANY trading symbols. Here are up to 20 "
                      "perpetual symbols this exchange DOES offer (share these if it fails):")
            swaps = [m["symbol"] for m in markets if m.get("swap")][:20]
            for s in swaps:
                log.error("  available: %s", s)
        return out

    def candles(self, symbol, timeframe, limit=400):
        raw = self.ex.fetch_ohlcv(symbol, timeframe, limit=limit)
        return [{"t": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]} for r in raw]

    def equity(self):
        try:
            bal = self.ex.fetch_balance()
            # USD collateral; fields vary by account — fall back gracefully
            return float(bal.get("USD", {}).get("total") or bal.get("total", {}).get("USD") or 0) or 100.0
        except Exception as e:
            if not self._equity_warned:
                log.warning("equity fetch failed (%s) — assuming 100 for sizing. "
                            "This is expected in dry-run without API keys.", e)
                self._equity_warned = True
            return 100.0

    def create_order(self, symbol, side, amount, params=None):
        if CFG["DRY_RUN"]:
            log.info("DRY_RUN order: %s %s %.6f %s", side, symbol, amount, params or "")
            return {"dry_run": True}
        return self.ex.create_order(symbol, "market", side, amount, None, params or {})

# --------------------------------------------------------------------------
# State (positions, journal, day-loss tracking) persisted to disk
# --------------------------------------------------------------------------
def load_state():
    try:
        with open(CFG["STATE_FILE"]) as f:
            return json.load(f)
    except Exception:
        return {"positions": [], "journal": [], "day": None, "day_start_equity": None}

def save_state(s):
    with open(CFG["STATE_FILE"], "w") as f:
        json.dump(s, f, indent=2)

def in_cooldown(journal, symbol, tf_minutes):
    last = next((e for e in reversed(journal) if e["symbol"] == symbol), None)
    if not last:
        return False
    base = max(6 * tf_minutes * 60, 60 * 60)
    mult = 1 if last.get("r", 0) > 0 else 4
    return (time.time() - last["t"] / 1000 if last["t"] > 1e10 else time.time() - last["t"]) < base * mult

TF_MIN = {"1m": 1, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}

# --------------------------------------------------------------------------
# Local web page — watch the bot run (served on localhost only, no keys exposed)
# --------------------------------------------------------------------------
WATCH_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crypto Signal Desk — Bot Monitor</title>
<script src="https://s3.tradingview.com/tv.js"></script>
<style>
  :root{--bg:#0b0e14;--card:#141922;--line:#232a36;--tx:#e6edf3;--dim:#8b98a9;--grn:#26d07c;--red:#f0616d;--amb:#f5b74e;--acc:#4f9dff}
  *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial}
  .wrap{max-width:1200px;margin:0 auto;padding:18px}
  h1{font-size:18px;margin:0 0 2px}.sub{color:var(--dim);font-size:12px;margin:0 0 16px}
  .badge{display:inline-block;padding:3px 10px;border-radius:999px;font-weight:700;font-size:12px}
  .dry{background:#1d2a44;color:#8fb7ff}.demo{background:#173a2c;color:#6ce3a6}.live{background:#4a1418;color:#ff9aa2}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:14px 0}
  .kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
  .kpi .l{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
  .kpi .v{font-size:22px;font-weight:700;margin-top:3px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:14px 0}
  .card h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim);margin:0 0 10px}
  table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
  th{color:var(--dim);font-weight:600;font-size:11px;text-transform:uppercase}
  .pos{color:var(--grn)}.neg{color:var(--red)}.dim{color:var(--dim)}
  .pill{padding:2px 8px;border-radius:6px;font-size:11px;font-weight:700}
  .p-cand{background:#173a2c;color:#6ce3a6}.p-block{background:#2a2230;color:#c6a9d6}.p-watch{background:#1f2733;color:#9fb2c9}
  .long{color:var(--grn);font-weight:700}.short{color:var(--red);font-weight:700}
  .charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:12px}
  .chartbox{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
  .chart-h{padding:8px 12px;font-size:12px;color:var(--dim);border-bottom:1px solid var(--line)}
  .tvc{height:320px}
  .empty{color:var(--dim);padding:8px 2px}
  .flash{animation:fl .6s}@keyframes fl{from{background:#1c2740}to{background:transparent}}
  .foot{color:var(--dim);font-size:11px;margin-top:20px;line-height:1.7}
</style></head>
<body><div class="wrap">
  <h1>Crypto Signal Desk — Bot Monitor <span id="mode" class="badge dry">starting…</span></h1>
  <p class="sub">Live view of the bot running on this machine. Updates every few seconds. <span id="clock"></span></p>

  <div class="kpis">
    <div class="kpi"><div class="l">Simulated balance</div><div class="v" id="bal">—</div></div>
    <div class="kpi"><div class="l">Realized</div><div class="v" id="cumr">—</div></div>
    <div class="kpi"><div class="l">Win rate</div><div class="v" id="wr">—</div></div>
    <div class="kpi"><div class="l">Closed trades</div><div class="v" id="nt">—</div></div>
    <div class="kpi"><div class="l">Open now</div><div class="v" id="op">—</div></div>
  </div>

  <div class="card"><h2>Open positions</h2><div id="open"></div></div>

  <div class="card"><h2>Live scan — what the bot is looking at right now</h2>
    <div id="closest" class="empty"></div>
    <div id="grid" style="margin-top:8px"></div>
  </div>

  <div class="card"><h2>Charts the bot is analysing</h2><div id="charts" class="charts"></div></div>

  <div class="card"><h2>Recent closed trades</h2><div id="recent"></div></div>

  <div class="foot" id="cfgline"></div>
  <div class="foot">This page is served by the bot on your own computer (localhost). It shows a simulation when in DRY-RUN.
    No orders are placed and no API keys are ever shown here or sent anywhere.</div>
</div>
<script>
var chartsBuilt=false;
function fmtMoney(v){return v==null?'—':'$'+Number(v).toFixed(2);}
function cls(v){return v>0?'pos':v<0?'neg':'dim';}
function sign(v){return (v>0?'+':'')+Number(v).toFixed(2);}
function tvSym(b){return 'BINANCE:'+b+'USDT';}
function buildCharts(syms){
  if(chartsBuilt||!window.TradingView||!syms.length)return;
  var wrap=document.getElementById('charts');wrap.innerHTML='';
  syms.forEach(function(b){
    var id='tv_'+b;var d=document.createElement('div');d.className='chartbox';
    d.innerHTML='<div class="chart-h">'+b+' · 1h</div><div id="'+id+'" class="tvc"></div>';
    wrap.appendChild(d);
    new TradingView.widget({container_id:id,symbol:tvSym(b),interval:'60',theme:'dark',style:'1',
      locale:'en',autosize:true,hide_side_toolbar:true,allow_symbol_change:false,hide_top_toolbar:false});
  });
  chartsBuilt=true;
}
function render(data){
  var s=data.status;
  if(!s){return;}
  var mb=document.getElementById('mode');
  mb.textContent=s.mode;
  mb.className='badge '+(s.mode.indexOf('DRY')>=0?'dry':s.mode.indexOf('DEMO')>=0?'demo':'live');
  document.getElementById('bal').textContent=fmtMoney(s.balance);
  var cr=document.getElementById('cumr');cr.textContent=sign(s.cum_r)+'R';cr.className='v '+cls(s.cum_r);
  document.getElementById('wr').textContent=(s.closed?s.win_rate+'%':'—');
  document.getElementById('nt').textContent=s.closed;
  document.getElementById('op').textContent=s.open_count;
  // open positions
  var oh=document.getElementById('open');
  if(!s.open||!s.open.length){oh.innerHTML='<div class="empty">No open positions — the bot is scanning for a setup that passes every gate.</div>';}
  else{var r='<table><tr><th>Side</th><th>Market</th><th>TF</th><th>Entry</th><th>Now</th><th>Unrealized</th><th>Lev</th><th>Margin</th><th>Risk</th></tr>';
    s.open.forEach(function(p){r+='<tr><td class="'+(p.dir=="LONG"?"long":"short")+'">'+p.dir+'</td><td>'+p.symbol+'</td><td>'+p.tf+'</td><td>'+Number(p.entry).toFixed(2)+'</td><td>'+(p.now==null?'—':Number(p.now).toFixed(2))+'</td><td class="'+cls(p.uR)+'">'+sign(p.uR)+'R</td><td>'+p.lev+'x</td><td>$'+p.invested+'</td><td>'+p.risk_frac+'%</td></tr>';});
    oh.innerHTML=r+'</table>';}
  // scan grid
  var cl=document.getElementById('closest');
  cl.textContent=data.closest?('Closest setup: '+data.closest):(s.open_count?'Fully deployed ('+s.open_count+' open) — not scanning for more right now.':'Scanning…');
  var g=document.getElementById('grid');var a=data.analysis||[];
  if(!a.length){g.innerHTML='<div class="empty">No charts scored yet this cycle.</div>';}
  else{var t='<table><tr><th>Market</th><th>TF</th><th>Score</th><th>Bias</th><th>Regime</th><th>Confidence</th><th>Status</th></tr>';
    a.forEach(function(x){var st=x.status=='candidate'?'p-cand':x.status=='blocked'?'p-block':'p-watch';
      var lbl=x.status=='candidate'?'READY':x.status=='blocked'?'blocked':'watching';
      t+='<tr><td>'+x.symbol+'</td><td>'+x.tf+'</td><td class="'+cls(x.score)+'">'+(x.score>0?'+':'')+x.score+'</td><td class="'+(x.dir=="LONG"?"long":"short")+'">'+x.dir+'</td><td class="dim">'+(x.regime||'—')+'</td><td>'+(x.conf==null?'—':x.conf+'%')+'</td><td><span class="pill '+st+'">'+lbl+'</span> <span class="dim">'+(x.reason||'')+'</span></td></tr>';});
    g.innerHTML=t+'</table>';}
  // recent trades
  var rc=document.getElementById('recent');var j=s.recent||[];
  if(!j.length){rc.innerHTML='<div class="empty">No closed trades yet.</div>';}
  else{var rt='<table><tr><th>Market</th><th>TF</th><th>Side</th><th>Result</th><th>R</th></tr>';
    j.forEach(function(e){rt+='<tr><td>'+(e.symbol||'').split('/')[0]+'</td><td>'+e.tf+'</td><td class="'+(e.dir>0?"long":"short")+'">'+(e.dir>0?'LONG':'SHORT')+'</td><td class="dim">'+e.result+'</td><td class="'+cls(e.r)+'">'+sign(e.r)+'R</td></tr>';});
    rc.innerHTML=rt+'</table>';}
  // config footer + charts
  document.getElementById('cfgline').textContent='Trigger: '+s.trigger+' · confidence floor '+s.min_conf+'% · timeframes '+(s.timeframes||[]).join(', ')+' · risk '+s.risk_band[0]+'–'+s.risk_band[1]+'% (cap '+s.risk_band[2]+'%) · leverage '+Object.keys(s.lev_map||{}).map(function(k){return k+' '+s.lev_map[k]+'x';}).join(' / ');
  var syms=[];(data.analysis||[]).forEach(function(x){if(syms.indexOf(x.symbol)<0)syms.push(x.symbol);});
  if(!syms.length&&s.open)s.open.forEach(function(p){if(syms.indexOf(p.symbol)<0)syms.push(p.symbol);});
  buildCharts(syms);
}
function poll(){
  fetch('/api/state').then(function(r){return r.json();}).then(function(d){
    document.getElementById('clock').textContent='Last update '+new Date().toLocaleTimeString();
    render(d);
  }).catch(function(){document.getElementById('clock').textContent='(waiting for the bot…)';});
}
poll();setInterval(poll,5000);
</script>
</body></html>"""

def _start_web_server():
    if not CFG["WEB_ENABLED"]:
        return
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # keep the console clean
        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            if self.path.startswith("/api/state"):
                payload = json.dumps({
                    "status": LATEST.get("status"),
                    "analysis": LATEST.get("analysis"),
                    "closest": LATEST.get("closest"),
                    "scan_ts": LATEST.get("scan_ts"),
                    "now": time.time(),
                }, default=str).encode("utf-8")
                self._send(200, payload, "application/json")
            else:
                self._send(200, WATCH_PAGE.encode("utf-8"), "text/html; charset=utf-8")
    host, port = CFG["WEB_HOST"], CFG["WEB_PORT"]
    try:
        srv = ThreadingHTTPServer((host, port), H)
    except OSError as e:
        log.warning("web page couldn't start on %s:%d (%s) — bot still runs without it", host, port, e)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    log.info("*** WATCH THE BOT LIVE IN YOUR BROWSER:  http://%s:%d  ***", shown, port)

# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def run():
    log.info("=" * 60)
    log.info("Crypto Signal Desk — Kraken Futures bot starting")
    lev_desc = " ".join("%s=%gx" % (k, v) for k, v in CFG["MAX_LEVERAGE_MAP"].items())
    if CFG["MAX_LEVERAGE"] and CFG["MAX_LEVERAGE"] > 0:
        lev_desc += " (global cap %gx)" % CFG["MAX_LEVERAGE"]
    log.info("DRY_RUN=%s  USE_DEMO=%s  EXIT_STYLE=%s  RISK=%.0f-%.0f%% (cap %.0f%%)  MAX_LEV: %s",
             CFG["DRY_RUN"], CFG["USE_DEMO"], CFG["EXIT_STYLE"],
             CFG["RISK_MIN_FRAC"] * 100, CFG["RISK_MAX_FRAC"] * 100, CFG["RISK_CAP_FRAC"] * 100, lev_desc)
    if not CFG["DRY_RUN"] and not CFG["USE_DEMO"]:
        log.warning("!!! LIVE REAL-MONEY MODE — orders will be placed with real funds !!!")
        time.sleep(5)
    _start_web_server()
    ex = Exchange()
    state = load_state()

    while True:
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            equity = ex.equity()
            if state.get("day") != today:
                state["day"] = today
                state["day_start_equity"] = equity
                save_state(state)
            # kill-switch: stop opening trades after a bad day
            day_pnl = (equity - (state.get("day_start_equity") or equity)) / max(equity, 1)
            trading_allowed = day_pnl > -CFG["DAILY_LOSS_LIMIT"]
            if not trading_allowed:
                log.warning("DAILY LOSS LIMIT hit (%.1f%%) — no new trades today.", day_pnl * 100)

            # 1) manage existing positions
            manage_positions(ex, state)

            # 2) scan for new entries
            if trading_allowed:
                scan_and_trade(ex, state, equity)

            save_state(state)
            log_status(ex, state)
        except Exception as e:
            log.exception("loop error: %s", e)
        time.sleep(CFG["POLL_SECONDS"])

def log_status(ex, state):
    """Print a clear balance + positions summary every cycle (and to trader.log),
    and publish the same picture to the local web page."""
    closed = [t for t in state["journal"] if t.get("r") is not None]
    open_ps = [p for p in state["positions"] if p["status"] == "open"]
    wins = [t for t in closed if t["r"] > 0]
    cum_r = sum(t["r"] for t in closed)
    win_rate = (len(wins) / len(closed) * 100) if closed else 0
    # simulated balance: $100 base; each trade's PnL = its R * the risk $ it staked
    # (variable 2-4% per trade, like the dashboard — not a flat 1%).
    bal = 100.0
    for t in closed:
        bal += t["r"] * bal * t.get("risk_frac", CFG["RISK_MIN_FRAC"])
    mode = "DRY-RUN (no real money)" if CFG["DRY_RUN"] else ("DEMO" if CFG["USE_DEMO"] else "LIVE REAL MONEY")
    log.info("========= STATUS [%s] =========", mode)
    log.info("Simulated balance: $%.2f  (from $100 base, %+.2fR realized)", bal, cum_r)
    log.info("Closed trades: %d, win rate %.0f%%, open positions: %d",
             len(closed), win_rate, len(open_ps))
    open_view = []
    for p in open_ps:
        cur = None
        try:
            cur = ex.candles(p["symbol"], p["tf"], 2)[-1]["c"]
            uR = p["dir"] * (cur - p["entry"]) / p["risk"] if p["risk"] else 0
            log.info("  OPEN %s %s %s | entry %.2f now %.2f | %+.2fR",
                     "LONG" if p["dir"] > 0 else "SHORT", p["symbol"], p["tf"], p["entry"], cur, uR)
        except Exception:
            uR = 0
            log.info("  OPEN %s %s %s | entry %.2f",
                     "LONG" if p["dir"] > 0 else "SHORT", p["symbol"], p["tf"], p["entry"])
        open_view.append({
            "symbol": p["symbol"].split("/")[0], "tf": p["tf"],
            "dir": "LONG" if p["dir"] > 0 else "SHORT", "entry": p["entry"], "now": cur,
            "uR": round(uR, 2), "lev": round(p.get("lev", 1), 1),
            "invested": round(p.get("invested", 0), 2),
            "risk_frac": round(p.get("risk_frac", 0) * 100, 1), "conf": p.get("conf"),
        })
    if not open_ps and not closed:
        log.info("  (no trades yet - scanning for a setup that passes all the gates)")
    log.info("===============================")
    # publish snapshot for the web page (safe fields only — NEVER any API key)
    LATEST["status"] = {
        "mode": mode, "balance": round(bal, 2), "cum_r": round(cum_r, 2),
        "closed": len(closed), "win_rate": round(win_rate), "open_count": len(open_ps),
        "open": open_view,
        "recent": list(reversed(state["journal"][-12:])),
        "trigger": CFG["TRIGGER"], "min_conf": CFG["MIN_CONFIDENCE"],
        "timeframes": [t.strip() for t in CFG["TIMEFRAMES"]],
        "risk_band": [CFG["RISK_MIN_FRAC"] * 100, CFG["RISK_MAX_FRAC"] * 100, CFG["RISK_CAP_FRAC"] * 100],
        "lev_map": CFG["MAX_LEVERAGE_MAP"], "ts": time.time(),
    }

def scan_and_trade(ex, state, equity):
    open_syms = {p["symbol"] for p in state["positions"] if p["status"] == "open"}
    if len(open_syms) >= CFG["MAX_CONCURRENT"]:
        return
    invested = sum(p.get("invested", 0) for p in state["positions"] if p["status"] == "open")
    enter_th = CFG["ENTER_THRESHOLD"]
    strong_th = CFG["STRONG_THRESHOLD"]
    need = strong_th if CFG["TRIGGER"] == "strong" else enter_th
    candidates = []
    near = []   # near-misses: (abs_score, "SYMBOL tf DIR — why it was blocked")
    grid = []   # one row per chart scanned, for the live web view
    for symbol in ex.trade_symbols:
        if not symbol or symbol in open_syms:
            continue
        # Pass 1: analyze EVERY timeframe for this symbol so we can score cross-
        # timeframe alignment (the confidence term the old bot was missing).
        tfs = {}
        for tf in CFG["TIMEFRAMES"]:
            tf = tf.strip()
            try:
                candles = ex.candles(symbol, tf, 400)
            except Exception as e:
                log.warning("candles %s %s failed: %s", symbol, tf, e)
                continue
            if len(candles) < 210:
                continue
            tfs[tf] = {"candles": candles, "an": analyze(candles)}
        # Pass 2: evaluate each tradable timeframe as a candidate.
        for tf, o in tfs.items():
            if tf == "1m":     # scan-only, never a primary trade
                continue
            an = o["an"]; candles = o["candles"]
            base = symbol.split("/")[0]
            d = 1 if an["score"] > 0 else -1
            dirtxt = "LONG" if d > 0 else "SHORT"
            row = {"symbol": base, "tf": tf, "score": an["score"], "dir": dirtxt,
                   "regime": None, "conf": None, "status": "watching", "reason": ""}
            grid.append(row)
            def blocked(reason):
                row["status"] = "blocked"; row["reason"] = reason
                near.append((abs(an["score"]), "%s %s %s — %s" % (base, tf, dirtxt, reason)))
            if abs(an["score"]) < need:
                blocked("score %+d, needs %s%d to trigger" % (an["score"], "±", int(need)))
                continue
            li = len(an["e20"]) - 1
            trend_strength = (abs(an["e20"][li] - an["e50"][li]) / an["atr"]
                              if (an["e20"][li] is not None and an["e50"][li] is not None and an["atr"] > 0) else 0)
            regime = "trend" if trend_strength >= 1 else "mixed" if trend_strength >= 0.5 else "chop"
            row["regime"] = regime
            if regime == "chop" and abs(an["score"]) < strong_th:
                blocked("chop regime, score %+d below STRONG" % an["score"]); continue
            closes = [c["c"] for c in candles]
            if signal_stability(closes, an["e20"]) >= 3:      # whipsaw gate
                blocked("whipsaw (signal keeps flipping)"); continue
            bt = backtest(candles)
            if bt["n"] >= 6 and (bt["avg_r"] <= 0 or bt["profit_factor"] < 1.2):
                blocked("backtest edge too weak (PF %.2f, %+0.2fR over %d trades)"
                        % (bt["profit_factor"], bt["avg_r"], bt["n"])); continue
            # timeframe alignment: how many OTHER timeframes of this symbol agree
            agree = sum(1 for otf, oo in tfs.items()
                        if otf != tf and abs(oo["an"]["score"]) >= enter_th
                        and (1 if oo["an"]["score"] > 0 else -1) == d)
            tv = tv_rating(candles)
            tv_pts = clamp(tv["score"] * d * 12, -12, 12)
            conf = confidence(an, bt, trend_strength, agree, tv_pts)
            row["conf"] = conf
            if conf < CFG["MIN_CONFIDENCE"]:
                blocked("confidence %d%%, needs %d%%" % (conf, int(CFG["MIN_CONFIDENCE"]))); continue
            if in_cooldown(state["journal"], symbol, TF_MIN.get(tf, 60)):
                blocked("cooldown after a recent trade"); continue
            row["status"] = "candidate"
            candidates.append({"symbol": symbol, "tf": tf, "dir": d, "score": an["score"],
                               "conf": conf, "an": an, "regime": regime, "agree": agree,
                               "tv": tv["score"]})
    # publish what the scan saw for the live web page
    grid.sort(key=lambda r: (r["conf"] if r["conf"] is not None else -1, abs(r["score"])), reverse=True)
    LATEST["analysis"] = grid
    LATEST["scan_ts"] = time.time()
    LATEST["closest"] = (sorted(near, reverse=True)[0][1] if near else None)
    if not candidates:
        if near:
            near.sort(reverse=True)
            log.info("No trade this scan. Closest setup: %s", near[0][1])
        else:
            log.info("No trade this scan — no signal on any chart yet (all inside the neutral band).")
        return
    candidates.sort(key=lambda c: c["conf"], reverse=True)
    best = candidates[0]
    open_trade(ex, state, best, equity, invested)

def open_trade(ex, state, d, equity, invested):
    an = d["an"]
    price = an["price"]; atr_now = an["atr"]
    stop_d = CFG["STOP_ATR"] * atr_now
    stop_pct = stop_d / price
    if stop_pct <= 0:
        return

    # --- Sizing that mirrors the dashboard exactly -----------------------------
    # 1) Deploy 10%->80% of equity as MARGIN, by confidence + Kelly (convictionInvest).
    frac = kelly_fraction(d["conf"], state["journal"])
    deploy_left = equity * CFG["MAX_DEPLOY_FRAC"] - invested
    invest = min(max(equity * frac, equity * 0.10), equity * CFG["MAX_INVEST_FRAC"], deploy_left)
    # Never let the margin alone (at 1x) risk more than the 6% cap if stopped.
    invest = min(invest, equity * CFG["RISK_CAP_FRAC"] / stop_pct)
    if invest < equity * 0.10 - 1e-9:
        return
    # 2) Solve leverage so the loss-if-stopped lands on the conviction risk band
    #    (~2%->4%, hard cap 6%), never exceeding this market's exchange max.
    risk_frac = conviction_risk_frac(d["conf"])
    risk_target = equity * risk_frac
    lev_cap = max_lev_for(d["symbol"])
    lev = max(1, min(lev_cap, round(risk_target / (invest * stop_pct))))
    notional = invest * lev
    # Cap notional so leverage rounding can't push realized risk past the 6% cap.
    max_notional = equity * CFG["RISK_CAP_FRAC"] / stop_pct
    if notional > max_notional:
        notional = max_notional
        lev = max(1, notional / invest)
    if notional < CFG["MIN_NOTIONAL"]:
        return
    risk_used = notional * stop_pct
    risk_pct_used = risk_used / equity if equity else 0
    # ---------------------------------------------------------------------------

    qty = notional / price
    stop = price - d["dir"] * stop_d
    tgt = price + d["dir"] * CFG["TARGET_ATR"] * atr_now
    side = "buy" if d["dir"] > 0 else "sell"
    log.info("OPEN %s %s %s  conf=%d%%  score=%+d  margin=$%.2f lev=%.1fx notional=$%.2f  "
             "risk=%.1f%%  entry~%.2f stop=%.2f tgt=%.2f",
             side.upper(), d["symbol"], d["tf"], d["conf"], d["score"], invest, lev, notional,
             risk_pct_used * 100, price, stop, tgt)
    try:
        ex.create_order(d["symbol"], side, qty)
    except Exception as e:
        log.error("order failed: %s", e)
        return
    state["positions"].append({
        "symbol": d["symbol"], "tf": d["tf"], "dir": d["dir"], "entry": price,
        "stop": stop, "tgt": tgt, "qty": qty, "lev": lev, "invested": invest,
        "risk": stop_d, "risk_frac": risk_pct_used, "conf": d["conf"],
        "opened": time.time(), "status": "open",
    })

def manage_positions(ex, state):
    for p in state["positions"]:
        if p["status"] != "open":
            continue
        try:
            candles = ex.candles(p["symbol"], p["tf"], 300)
        except Exception:
            continue
        an = analyze(candles)
        cur = an["price"]; atr_now = an["atr"]
        uR = p["dir"] * (cur - p["entry"]) / p["risk"] if p["risk"] else 0
        dir_score = p["dir"] * an["score"]
        hard_flip = dir_score <= -CFG["STRONG_THRESHOLD"]
        at_stop = (cur <= p["stop"]) if p["dir"] > 0 else (cur >= p["stop"])
        exit_now = None
        if CFG["EXIT_STYLE"] == "hard":
            if at_stop:
                exit_now = "sl"
            elif (cur >= p["tgt"]) if p["dir"] > 0 else (cur <= p["tgt"]):
                exit_now = "tp"
            elif hard_flip:
                exit_now = "reversal"
        else:  # diamond: hold through noise, exit only on strong reversal / target
            if (cur >= p["tgt"]) if p["dir"] > 0 else (cur <= p["tgt"]):
                exit_now = "tp"
            elif hard_flip:
                exit_now = "reversal"
        # breakeven + trail (both styles)
        if uR >= 1 and ((p["stop"] < p["entry"]) if p["dir"] > 0 else (p["stop"] > p["entry"])):
            p["stop"] = p["entry"] + p["dir"] * 0.1 * atr_now
        if uR >= 1.5:
            trail = cur - p["dir"] * 1.6 * atr_now
            if (trail > p["stop"]) if p["dir"] > 0 else (trail < p["stop"]):
                p["stop"] = trail
        if exit_now:
            close_trade(ex, state, p, cur, exit_now, uR)

def close_trade(ex, state, p, price, result, uR):
    side = "sell" if p["dir"] > 0 else "buy"
    log.info("CLOSE %s %s %s  %s  R=%+.2f  ~%.2f", side.upper(), p["symbol"], p["tf"], result, uR, price)
    try:
        ex.create_order(p["symbol"], side, p["qty"], {"reduceOnly": True})
    except Exception as e:
        log.error("close order failed: %s", e)
        return
    p["status"] = "closed"; p["result"] = result; p["exit"] = price; p["r"] = uR
    state["journal"].append({"t": int(time.time()), "symbol": p["symbol"], "tf": p["tf"],
                             "dir": p["dir"], "result": result, "r": uR,
                             "risk_frac": p.get("risk_frac", CFG["RISK_MIN_FRAC"])})

if __name__ == "__main__":
    run()
