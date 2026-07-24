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
from collections import deque
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
    "DATA_SOURCE":     os.getenv("DATA_SOURCE", "binance").lower(),     # where CHART DATA comes from: "binance" or "kraken"
    "TIMEFRAMES":      os.getenv("TIMEFRAMES", "15m,1h,4h,1d").split(","),  # NEVER 1m — noise
    "POLL_SECONDS":    int(_num("POLL_SECONDS", 60)),
    "MIN_CONFIDENCE":  _num("MIN_CONFIDENCE", 60),      # confidence floor
    "ENTER_THRESHOLD": _num("ENTER_THRESHOLD", 18),
    "STRONG_THRESHOLD":_num("STRONG_THRESHOLD", 45),
    "TRIGGER":         os.getenv("TRIGGER", "standard"),  # momentum only: "standard"/"strong"
    # STRATEGY: "reversion" = buy proven bullish reversals at oversold extremes / short
    # bearish reversals at overbought extremes (candlestick patterns, NOT chasing).
    # "momentum" = the old trend-following score. Reversion is the new default.
    "STRATEGY":        os.getenv("STRATEGY", "reversion"),
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
    # Real-world trading costs the backtest must subtract (so its edge isn't fake).
    "FEE_RATE":        _num("FEE_RATE", 0.0005),   # taker fee per side (Kraken Futures ~0.05%)
    "SLIPPAGE":        _num("SLIPPAGE", 0.0002),   # assumed slippage per side (~0.02%)
    "FUNDING_DAILY":   _num("FUNDING_DAILY", 0.0003),  # avg funding paid per day held (~0.03%/day)
    "DAILY_LOSS_LIMIT":_num("DAILY_LOSS_LIMIT", 0.10),  # kill-switch
    "STATE_FILE":      os.getenv("STATE_FILE", "bot_state.json"),
    "WEB_ENABLED":     _bool("WEB_ENABLED", True),      # serve the local watch page
    "WEB_HOST":        os.getenv("WEB_HOST", "127.0.0.1"),  # localhost only — never exposed
    "WEB_PORT":        int(_num("WEB_PORT", 8899)),  # 8787 avoided (construction AI uses it)
}

# Live snapshot the local web page reads (never persisted, never leaves the laptop).
LATEST = {"status": None, "analysis": [], "closest": None, "scan_ts": None}
LOG_LINES = deque(maxlen=300)   # rolling activity feed shown on the web page
# Manual controls from the web page (buttons write here; the main loop obeys them).
CONTROL = {"paused": False, "close": set(), "close_all": False}

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

# Mirror every log line into a buffer the web page shows as a live activity feed.
class _WebLogHandler(logging.Handler):
    def emit(self, record):
        try:
            LOG_LINES.append(self.format(record))
        except Exception:
            pass
_wlh = _WebLogHandler()
_wlh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(message)s", "%H:%M:%S"))
log.addHandler(_wlh)

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
            "macd_line": line, "macd_signal": signal, "stoch": stk,
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

# --------------------------------------------------------------------------
# Candlestick pattern detection — the "scan candles for patterns that win"
# --------------------------------------------------------------------------
def _body(c):  return abs(c["c"] - c["o"])
def _rng(c):   return max(c["h"] - c["l"], 1e-9)
def _upw(c):   return c["h"] - max(c["o"], c["c"])
def _low(c):   return min(c["o"], c["c"]) - c["l"]
def _green(c): return c["c"] >= c["o"]

def detect_patterns(candles, i):
    """Reversal candlestick patterns ending at bar i. Returns (name, dir, strength).
    A pattern REQUIRES the candle to actually reject/turn — a big red candle that
    keeps falling with no lower-wick rejection produces NO bullish pattern, so the
    bot won't try to catch a knife that's 'obviously going to keep going down'."""
    out = []
    if i < 2:
        return out
    c, p = candles[i], candles[i - 1]
    body, rng = _body(c), _rng(c)
    upw, low = _upw(c), _low(c)
    # Hammer (bullish): long lower wick rejection, small body up top, closes green
    if body > 0 and low >= 2 * body and upw <= body and _green(c):
        out.append(("Hammer", 1, clamp(low / rng, 0.3, 1)))
    # Shooting star (bearish): long upper wick rejection, closes red
    if body > 0 and upw >= 2 * body and low <= body and not _green(c):
        out.append(("Shooting star", -1, clamp(upw / rng, 0.3, 1)))
    # Bullish engulfing: green candle whose body swallows the prior red body
    if _green(c) and not _green(p) and c["c"] >= p["o"] and c["o"] <= p["c"] and body > _body(p):
        out.append(("Bullish engulfing", 1, clamp(body / _rng(p), 0.4, 1)))
    # Bearish engulfing
    if not _green(c) and _green(p) and c["o"] >= p["c"] and c["c"] <= p["o"] and body > _body(p):
        out.append(("Bearish engulfing", -1, clamp(body / _rng(p), 0.4, 1)))
    # Tweezer bottom / top: matched extreme two bars running, second one reverses
    if abs(c["l"] - p["l"]) <= 0.12 * rng and _green(c) and not _green(p):
        out.append(("Tweezer bottom", 1, 0.55))
    if abs(c["h"] - p["h"]) <= 0.12 * rng and not _green(c) and _green(p):
        out.append(("Tweezer top", -1, 0.55))
    return out

def _swing(candles, i, look=40):
    w = candles[max(0, i - look):i + 1]
    return min(x["l"] for x in w), max(x["h"] for x in w)

def reversion_signal(candles, i, ind):
    """Buy a bullish reversal pattern only when OVERSOLD/stretched-down; short a
    bearish reversal pattern only when OVERBOUGHT/stretched-up. The pattern is the
    proof the move is turning — no pattern, no trade."""
    price = candles[i]["c"]
    atr = ind["atr"]
    if atr <= 0:
        return None
    r = ind["rsi"][i] if ind["rsi"][i] is not None else 50
    bbl, bbu, e20 = ind["bbl"][i], ind["bbu"][i], ind["e20"][i]
    support, resistance = _swing(candles, i)
    pats = detect_patterns(candles, i)
    bull = [p for p in pats if p[1] > 0]
    bear = [p for p in pats if p[1] < 0]
    oversold = (r < 40) or (bbl is not None and price <= bbl) or (e20 is not None and price <= e20 - 1.0 * atr)
    overbought = (r > 60) or (bbu is not None and price >= bbu) or (e20 is not None and price >= e20 + 1.0 * atr)
    if bull and oversold:
        return {"dir": 1, "patterns": [p[0] for p in bull], "strength": max(p[2] for p in bull),
                "extremity": clamp((45 - r) / 25, 0, 1), "ctx": "oversold (RSI %.0f) + bullish reversal" % r,
                "support": support, "resistance": resistance}
    if bear and overbought:
        return {"dir": -1, "patterns": [p[0] for p in bear], "strength": max(p[2] for p in bear),
                "extremity": clamp((r - 55) / 25, 0, 1), "ctx": "overbought (RSI %.0f) + bearish reversal" % r,
                "support": support, "resistance": resistance}
    return None

def momentum_signal(candles, i, ind):
    """Legacy trend-following entry (kept for A/B comparison)."""
    need = CFG["STRONG_THRESHOLD"] if CFG["TRIGGER"] == "strong" else CFG["ENTER_THRESHOLD"]
    if abs(ind["score"]) < need:
        return None
    d = 1 if ind["score"] > 0 else -1
    support, resistance = _swing(candles, i)
    return {"dir": d, "patterns": [], "strength": min(abs(ind["score"]) / 70, 1), "extremity": 0,
            "ctx": "momentum score %+d" % ind["score"], "support": support, "resistance": resistance}

def signal_at(candles, i):
    """Single source of truth for 'is there a trade at bar i?' — used by BOTH the live
    scan and the backtest, so the backtested edge reflects exactly what it trades."""
    ind = analyze(candles[:i + 1])
    if CFG["STRATEGY"] == "momentum":
        return momentum_signal(candles, i, ind), ind
    return reversion_signal(candles, i, ind), ind

def reversion_confidence(strength, extremity, bt):
    conf = 40 + strength * 25 + clamp(extremity, 0, 1) * 15
    if bt["n"]:
        shrink = bt["n"] / (bt["n"] + 10)
        conf += clamp(clamp(bt["avg_r"], -1, 1) * shrink * 30, -15, 15)
    oos = bt.get("oos", {})
    if oos.get("n", 0) >= 5:
        conf += 5 if oos["avg_r"] > 0 else -8
    return round(clamp(conf, 5, 95))

def _mini_candles(candles, n=60):
    w = candles[-n:]
    return [{"o": round(c["o"], 2), "h": round(c["h"], 2), "l": round(c["l"], 2), "c": round(c["c"], 2)} for c in w]

def _bt_stats(rlist):
    n = len(rlist)
    wins = [r for r in rlist if r > 0]
    losses = [r for r in rlist if r <= 0]
    gw = sum(wins); gl = abs(sum(losses))
    return {"n": n, "win_rate": (len(wins) / n * 100) if n else 0,
            "avg_r": ((gw - gl) / n) if n else 0,
            "profit_factor": (gw / gl) if gl > 0 else (999 if gw > 0 else 0)}

def backtest(candles, tf_minutes=60):
    """Replay the composite strategy over history — NET of real trading costs (fees,
    slippage, funding) — and split it into an in-sample and an out-of-sample (most
    recent third) window so we can see whether the edge still holds on unseen data."""
    warm = 210
    fills = []        # (open_index, gross_R, net_R)
    pos = None
    roundtrip = 2 * CFG["FEE_RATE"] + 2 * CFG["SLIPPAGE"]   # cost fraction, entry+exit
    fund_hr = CFG["FUNDING_DAILY"] / 24.0
    for i in range(warm, len(candles) - 1):
        if pos:
            c = candles[i]
            exit_px = None
            if (c["l"] <= pos["stop"]) if pos["dir"] > 0 else (c["h"] >= pos["stop"]):
                exit_px = pos["stop"]
            elif (c["h"] >= pos["tgt"]) if pos["dir"] > 0 else (c["l"] <= pos["tgt"]):
                exit_px = pos["tgt"]
            elif i - pos["open_i"] >= pos["max_bars"]:
                exit_px = c["c"]
            if exit_px is not None:
                gross_R = pos["dir"] * (exit_px - pos["entry"]) / pos["risk"]
                stop_pct = pos["risk"] / pos["entry"]
                hours = (i - pos["open_i"]) * tf_minutes / 60.0
                # costs are a % of notional; convert to R by dividing by the stop %
                cost_R = ((roundtrip + fund_hr * hours) / stop_pct) if stop_pct > 0 else 0
                fills.append((pos["open_i"], gross_R, gross_R - cost_R))
                pos = None
            continue
        sig, an = signal_at(candles, i)
        if not sig:
            continue
        d = sig["dir"]
        atr_now = an["atr"]
        entry = candles[i + 1]["o"]
        stop_d = CFG["STOP_ATR"] * atr_now
        tgt_d = CFG["TARGET_ATR"] * atr_now
        pos = {"dir": d, "entry": entry, "stop": entry - d * stop_d, "tgt": entry + d * tgt_d,
               "risk": stop_d, "open_i": i + 1, "max_bars": math.ceil(tgt_d / (0.65 * atr_now) * 1.7)}
    net = [f[2] for f in fills]
    gross = [f[1] for f in fills]
    stats = _bt_stats(net)
    stats["gross_avg_r"] = (sum(gross) / len(gross)) if gross else 0
    # out-of-sample = trades opened in the most recent third of the tested window
    lo, hi = warm, len(candles) - 1
    cut = lo + int((hi - lo) * 0.67)
    stats["oos"] = _bt_stats([f[2] for f in fills if f[0] >= cut])
    return stats

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

def chart_readout(an, candles):
    """Turn the indicators into a plain-language read of the chart + the key price
    levels (support/resistance/trend) — what a trader would draw and note."""
    i = len(candles) - 1
    price = an["price"]
    read = []
    def row(k, v, b):
        read.append({"k": k, "v": v, "b": b})
    e20, e50, e200 = an["e20"][i], an["e50"][i], an["e200"][i]
    if e200 is not None:
        row("Primary trend (EMA200)", ("UP — price above" if price >= e200 else "DOWN — price below")
            + " %.2f" % e200, "bull" if price >= e200 else "bear")
    if e20 is not None and e50 is not None:
        row("Short vs mid trend", ("EMA20 above EMA50 (bullish)" if e20 >= e50 else "EMA20 below EMA50 (bearish)"),
            "bull" if e20 >= e50 else "bear")
    r = an["rsi"][i]
    if r is not None:
        zone = "overbought" if r > 70 else "oversold" if r < 30 else "neutral"
        row("Momentum (RSI 14)", "%.0f — %s" % (r, zone), "bull" if r >= 50 else "bear")
    ml, ms = an["macd_line"][i], an["macd_signal"][i]
    if ml is not None and ms is not None:
        row("MACD", "bullish (line above signal)" if ml >= ms else "bearish (line below signal)",
            "bull" if ml >= ms else "bear")
    st = an["stoch"][i]
    if st is not None:
        row("Stochastic", "%.0f — %s" % (st, "high" if st > 80 else "low" if st < 20 else "mid"),
            "bull" if st >= 50 else "bear")
    bbu, bbl = an["bbu"][i], an["bbl"][i]
    if bbu is not None and bbu != bbl:
        pb = (price - bbl) / (bbu - bbl) * 100
        row("Bollinger position", "%.0f%% of the band" % pb, "bull" if pb >= 50 else "bear")
    row("Volatility (ATR)", "%.2f%% of price" % an["atr_pct"], "neutral")
    # support / resistance the bot is watching (recent swing high & low)
    win = candles[-40:] if len(candles) >= 40 else candles
    resistance = max(c["h"] for c in win)
    support = min(c["l"] for c in win)
    return read, {"support": support, "resistance": resistance,
                  "e20": e20, "e50": e50, "e200": e200, "price": price}

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
        self._equity_warned = False
        bases = [b.strip().upper() for b in CFG["SYMBOLS"] if b.strip()]

        # CHART DATA source: Binance (what the user asked for) with a safe fallback to
        # Kraken if Binance can't be reached (some networks/regions block it).
        self.source = CFG["DATA_SOURCE"]
        self.data_ex = None
        if self.source == "binance":
            try:
                self.data_ex = ccxt.binance({"enableRateLimit": True})
                self.data_ex.load_markets()
                self.trade_symbols = self._resolve_binance(bases)
                if not self.trade_symbols:
                    raise RuntimeError("no BTC/ETH/SOL USDT pairs found")
                log.info("Chart data: Binance (live)")
            except Exception as e:
                log.warning("Binance data unavailable (%s) — falling back to Kraken data.", e)
                self.source = "kraken"; self.data_ex = None
        if self.data_ex is None:
            self.trade_symbols = self._resolve_symbols(bases)
            log.info("Chart data: Kraken Futures")

    def _resolve_binance(self, bases):
        """Map base coins to Binance USDT spot pairs (BTC/USDT, ...) for live charts."""
        out = []
        for base in bases:
            sym = base + "/USDT"
            if sym in self.data_ex.markets:
                out.append(sym); log.info("resolved %s -> %s (Binance)", base, sym)
            else:
                log.warning("Binance has no %s pair", sym)
        return out

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
        ex = self.data_ex or self.ex
        raw = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
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
<title>BAT-TRADER // Crypto Signal Desk</title>
<script src="https://s3.tradingview.com/tv.js"></script>
<style>
  :root{--bg:#08090c;--card:#0f1115;--card2:#0c0d11;--line:#1c1f26;--edge:#2a2e37;--tx:#e8eaed;--dim:#767d8a;--grn:#24d17e;--red:#ff5266;--amb:#ffd23f}
  *{box-sizing:border-box}
  body{margin:0;background:
      radial-gradient(1100px 380px at 50% -140px,rgba(255,210,63,.06),transparent 70%),var(--bg);
    color:var(--tx);font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial}
  .wrap{max-width:1240px;margin:0 auto;padding:16px 18px 40px}
  .top{border-top:3px solid var(--amb);margin:-16px -18px 14px;padding:16px 18px 0}
  h1{font-size:19px;margin:0 0 2px;letter-spacing:.14em;text-transform:uppercase;font-weight:800}
  h1 .bat{color:var(--amb)}
  .sub{color:var(--dim);font-size:12px;margin:0 0 6px}
  .badge{display:inline-block;padding:3px 10px;border-radius:4px;font-weight:800;font-size:11px;letter-spacing:.06em}
  .dry{background:#141a2e;color:#9fb7ff}.demo{background:#0f2c22;color:#5fe3a2}.live{background:#3a1014;color:#ff8f98}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:14px 0}
  .kpi{background:linear-gradient(180deg,var(--card),var(--card2));border:1px solid var(--line);border-radius:8px;padding:11px 13px}
  .kpi .l{color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:.09em}
  .kpi .v{font-size:23px;font-weight:800;margin-top:3px;font-variant-numeric:tabular-nums}
  .card{background:linear-gradient(180deg,var(--card),var(--card2));border:1px solid var(--line);border-radius:8px;padding:13px 15px;margin:12px 0}
  .card h2{font-size:12px;text-transform:uppercase;letter-spacing:.11em;color:var(--amb);margin:0 0 10px;font-weight:800}
  table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
  th{color:var(--dim);font-weight:700;font-size:10px;text-transform:uppercase;letter-spacing:.06em}
  td{font-variant-numeric:tabular-nums}
  .pos{color:var(--grn)}.neg{color:var(--red)}.dim{color:var(--dim)}
  .long{color:var(--grn);font-weight:800}.short{color:var(--red);font-weight:800}
  .empty{color:var(--dim);padding:10px 2px}
  .logfeed{background:#050608;border:1px solid var(--line);border-radius:6px;padding:10px 12px;height:250px;overflow:auto;margin:0;
    font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#9fb0bf;white-space:pre-wrap;word-break:break-word}
  #live{font-weight:800;letter-spacing:.04em}
  .controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:12px 0 2px}
  .btn{background:#12151b;color:var(--tx);border:1px solid var(--edge);border-radius:6px;padding:8px 14px;font-size:12px;font-weight:700;cursor:pointer;letter-spacing:.03em;text-transform:uppercase}
  .btn:hover{border-color:var(--amb)}
  .btn.warn{color:var(--amb);border-color:#4a3d12}
  .btn.danger{color:#ff8f98;border-color:#4a1c22}
  .btn.mini{padding:4px 10px;font-size:11px}
  .pausebar{background:#241d08;border:1px solid #4a3d12;color:var(--amb);border-radius:6px;padding:8px 12px;font-weight:700;font-size:12px;display:none;margin:10px 0}
  .scan{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:12px}
  .sc{background:#0b0c10;border:1px solid var(--line);border-radius:8px;overflow:hidden}
  .sc.ready{border-color:#1f6b46;box-shadow:0 0 0 1px rgba(36,209,126,.25) inset}
  .sc-h{display:flex;justify-content:space-between;align-items:center;padding:9px 12px;border-bottom:1px solid var(--line)}
  .sc-h .t{font-weight:800;letter-spacing:.05em}
  .sc-h .cf{font-size:12px;color:var(--dim)}
  .cv{display:block;width:100%;height:170px;background:#070809}
  .pro{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:12px}
  .pbox{background:#0b0c10;border:1px solid var(--line);border-radius:8px;overflow:hidden}
  .pbox .ph{padding:8px 12px;font-size:11px;color:var(--dim);border-bottom:1px solid var(--line);text-transform:uppercase;letter-spacing:.06em}
  .tvc{height:420px}
  .sc-b{padding:9px 12px;font-size:12px}
  .sc-b .pat{color:var(--amb);font-weight:700}
  .sc-b .row2{color:var(--dim);margin-top:4px;line-height:1.5}.sc-b .row2 b{color:var(--tx)}
  .tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:800;letter-spacing:.05em;text-transform:uppercase}
  .t-ready{background:#0f2c22;color:#5fe3a2}.t-block{background:#241a10;color:#d8a86a}.t-watch{background:#15181f;color:#8b94a3}
  .foot{color:var(--dim);font-size:11px;margin-top:20px;line-height:1.7}
</style></head>
<body><div class="wrap">
  <div class="top">
    <h1><span class="bat">◤</span> BAT-TRADER <span class="bat">◢</span> <span class="dim" style="font-weight:600;letter-spacing:.06em">CRYPTO SIGNAL DESK</span> <span id="mode" class="badge dry">starting…</span></h1>
    <p class="sub"><span id="live">○ connecting…</span> · pattern-reversion engine · watching for proven reversals at extremes · <span id="clock"></span></p>
  </div>

  <div class="controls">
    <button id="pauseBtn" class="btn warn" onclick="togglePause()">⏸ Pause new trades</button>
    <button class="btn danger" onclick="closeAll()">✕ Close ALL positions</button>
    <span class="dim" style="font-size:11px;text-transform:none;letter-spacing:0">Acts within ~2s. Full stop = close the terminal / Ctrl+C.</span>
  </div>
  <div id="pausebar" class="pausebar">⏸ PAUSED — not opening new trades. Still manages and lets you close open ones.</div>

  <div class="kpis">
    <div class="kpi"><div class="l">Balance</div><div class="v" id="bal">—</div></div>
    <div class="kpi"><div class="l">Realized</div><div class="v" id="cumr">—</div></div>
    <div class="kpi"><div class="l">Win rate</div><div class="v" id="wr">—</div></div>
    <div class="kpi"><div class="l">Closed</div><div class="v" id="nt">—</div></div>
    <div class="kpi"><div class="l">Open now</div><div class="v" id="op">—</div></div>
  </div>

  <div class="card"><h2>▸ Open positions</h2><div id="open"></div></div>

  <div class="card"><h2>▸ Pattern scan — the charts it's reading, live</h2>
    <div id="closest" class="empty"></div>
    <div id="scan" class="scan" style="margin-top:10px"></div>
  </div>

  <div class="card"><h2>▸ Pro charts — full TradingView with the indicators the bot reads (live from Binance)</h2>
    <div id="prochart" class="pro"></div></div>

  <div class="card"><h2>▸ Live activity feed</h2><pre id="logfeed" class="logfeed">waiting for the bot…</pre></div>

  <div class="card"><h2>▸ Recent closed trades</h2><div id="recent"></div></div>

  <div class="foot" id="cfgline"></div>
  <div class="foot">Served by the bot on your own machine (localhost). Simulation while in DRY-RUN — no orders placed, no API keys shown or sent anywhere.</div>
</div>
<script>
function setLive(ok,txt){var e=document.getElementById('live');e.textContent=(ok?'● ':'○ ')+txt;e.style.color=ok?'var(--grn)':'var(--dim)';}
function renderLog(lines){var lf=document.getElementById('logfeed');if(!lines||!lines.length)return;
  var atBottom=lf.scrollHeight-lf.scrollTop-lf.clientHeight<40;
  lf.textContent=lines.join('\\n');
  if(atBottom)lf.scrollTop=lf.scrollHeight;}
function drawChart(cv,row){
  var cs=row.candles;if(!cv||!cs||!cs.length)return;
  var dpr=window.devicePixelRatio||1,w=cv.clientWidth,h=cv.clientHeight;
  cv.width=w*dpr;cv.height=h*dpr;var g=cv.getContext('2d');g.setTransform(dpr,0,0,dpr,0,0);g.clearRect(0,0,w,h);
  var lo=1e18,hi=-1e18;cs.forEach(function(c){if(c.l<lo)lo=c.l;if(c.h>hi)hi=c.h;});
  if(row.plan){[row.plan.stop,row.plan.target,row.plan.entry].forEach(function(v){if(v<lo)lo=v;if(v>hi)hi=v;});}
  if(row.levels){[row.levels.support,row.levels.resistance].forEach(function(v){if(v<lo)lo=v;if(v>hi)hi=v;});}
  var pad=(hi-lo)*0.08||1;lo-=pad;hi+=pad;
  var pL=4,pR=54,pT=6,pB=6,plotW=w-pL-pR,plotH=h-pT-pB,n=cs.length,cw=plotW/n;
  function Y(p){return pT+(hi-p)/(hi-lo)*plotH;}
  function X(i){return pL+i*cw+cw/2;}
  function hline(p,col,dash,lab){g.strokeStyle=col;g.setLineDash(dash);g.lineWidth=1;g.beginPath();g.moveTo(pL,Y(p));g.lineTo(pL+plotW,Y(p));g.stroke();g.setLineDash([]);if(lab){g.fillStyle=col;g.font='9px ui-monospace';g.fillText(lab,pL+plotW+3,Y(p)+3);}}
  if(row.levels){hline(row.levels.support,'#333844',[3,3],'S');hline(row.levels.resistance,'#333844',[3,3],'R');}
  if(row.plan){hline(row.plan.target,'#1f7d50',[4,3],'TP');hline(row.plan.entry,'#7c828d',[2,2],'E');hline(row.plan.stop,'#7d2a32',[4,3],'SL');}
  cs.forEach(function(c,i){var up=c.c>=c.o,col=up?'#24d17e':'#ff5266',x=X(i);
    g.strokeStyle=col;g.lineWidth=1;g.beginPath();g.moveTo(x,Y(c.h));g.lineTo(x,Y(c.l));g.stroke();
    var bw=Math.max(cw*0.62,1),yo=Y(c.o),yc=Y(c.c);g.fillStyle=col;g.fillRect(x-bw/2,Math.min(yo,yc),bw,Math.max(Math.abs(yc-yo),1));});
  if(row.marker!=null&&cs[row.marker]){var mi=row.marker,mc=cs[mi],x=X(mi),bw=Math.max(cw*0.95,5);
    g.strokeStyle='#ffd23f';g.lineWidth=1.5;g.setLineDash([]);g.strokeRect(x-bw/2,Y(mc.h)-3,bw,(Y(mc.l)+3)-(Y(mc.h)-3));
    if(row.pattern){g.fillStyle='#ffd23f';g.font='bold 10px ui-monospace';var lab=(''+row.pattern).split(',')[0];g.fillText(lab,Math.max(2,Math.min(x-18,w-pR-56)),Math.max(11,Y(mc.h)-6));}}
}
function renderScan(rows){
  var wrap=document.getElementById('scan');
  var top=(rows||[]).filter(function(r){return r.candles&&r.candles.length;}).slice(0,6);
  if(!top.length){wrap.innerHTML='<div class="empty">Scanning the candles for reversal patterns…</div>';return;}
  wrap.innerHTML=top.map(function(r,i){
    var dir=r.dir?'<span class="'+(r.dir=="LONG"?"long":"short")+'">'+r.dir+'</span>':'<span class="dim">no setup</span>';
    var tag=r.status=='candidate'?'<span class="tag t-ready">ready</span>':r.status=='blocked'?'<span class="tag t-block">blocked</span>':'<span class="tag t-watch">watching</span>';
    var pat=r.pattern?'<span class="pat">'+r.pattern+'</span>':'<span class="dim">'+(r.reason||'watching')+'</span>';
    var bt='';if(r.backtest){var b=r.backtest;bt='<div class="row2">Backtest net of costs: <b>'+b.win_rate+'%</b> win · PF <b class="'+(b.pf>=1.15?'pos':'neg')+'">'+b.pf+'</b> · '+b.n+' trades'+(b.oos_n?' · OOS PF <b class="'+(b.oos_pf>=1?'pos':'neg')+'">'+b.oos_pf+'</b>':'')+'</div>';}
    var pl='';if(r.plan){pl='<div class="row2">Plan: entry <b>'+r.plan.entry+'</b> · SL <b>'+r.plan.stop+'</b> · TP <b>'+r.plan.target+'</b> · R:R '+r.plan.rr+':1</div>';}
    var why=(r.status=='blocked'&&r.backtest)?'<div class="row2">Standing aside: '+(r.reason||'')+'</div>':'';
    return '<div class="sc'+(r.status=='candidate'?' ready':'')+'"><div class="sc-h"><span class="t">'+r.symbol+' · '+r.tf+' &nbsp;'+dir+'</span><span class="cf">'+tag+' &nbsp; '+(r.conf==null?'':r.conf+'%')+'</span></div>'
      +'<canvas class="cv" id="cv'+i+'"></canvas>'
      +'<div class="sc-b"><div>'+pat+'</div>'+bt+pl+why+'</div></div>';
  }).join('');
  top.forEach(function(r,i){drawChart(document.getElementById('cv'+i),r);});
}
function fmtMoney(v){return v==null?'—':'$'+Number(v).toFixed(2);}
function cls(v){return v>0?'pos':v<0?'neg':'dim';}
function sign(v){return (v>0?'+':'')+Number(v).toFixed(2);}
var proBuilt=false;
function buildProCharts(syms){
  if(proBuilt||!window.TradingView||!syms.length)return;
  var wrap=document.getElementById('prochart');wrap.innerHTML='';
  syms.forEach(function(b){
    var id='pv_'+b;var d=document.createElement('div');d.className='pbox';
    d.innerHTML='<div class="ph">'+b+'/USDT · 15m · Binance</div><div id="'+id+'" class="tvc"></div>';
    wrap.appendChild(d);
    new TradingView.widget({container_id:id,symbol:'BINANCE:'+b+'USDT',interval:'15',theme:'dark',
      style:'1',locale:'en',autosize:true,hide_side_toolbar:false,allow_symbol_change:false,
      studies:['MASimple@tv-basicstudies','MAExp@tv-basicstudies','BB@tv-basicstudies',
               'RSI@tv-basicstudies','MACD@tv-basicstudies']});
  });
  proBuilt=true;
}
function render(data){
  var s=data.status;
  if(!s){return;}
  var mb=document.getElementById('mode');
  mb.textContent=s.mode;
  mb.className='badge '+(s.mode.indexOf('DRY')>=0?'dry':s.mode.indexOf('DEMO')>=0?'demo':'live');
  window._paused=!!s.paused;
  document.getElementById('pausebar').style.display=s.paused?'block':'none';
  document.getElementById('pauseBtn').textContent=s.paused?'▶ Resume trading':'⏸ Pause new trades';
  document.getElementById('bal').textContent=fmtMoney(s.balance);
  var cr=document.getElementById('cumr');cr.textContent=sign(s.cum_r)+'R';cr.className='v '+cls(s.cum_r);
  document.getElementById('wr').textContent=(s.closed?s.win_rate+'%':'—');
  document.getElementById('nt').textContent=s.closed;
  document.getElementById('op').textContent=s.open_count;
  // open positions
  var oh=document.getElementById('open');
  if(!s.open||!s.open.length){oh.innerHTML='<div class="empty">No open positions — the bot is scanning for a setup that passes every gate.</div>';}
  else{var r='<table><tr><th>Side</th><th>Market</th><th>TF</th><th>Invested</th><th>Lev</th><th>Entry</th><th>Now</th><th>P&amp;L %</th><th>P&amp;L $</th><th>R</th><th></th></tr>';
    s.open.forEach(function(p){
      r+='<tr><td class="'+(p.dir=="LONG"?"long":"short")+'">'+p.dir+'</td><td>'+p.symbol+'</td><td>'+p.tf+'</td>'
        +'<td><b>$'+p.invested+'</b></td><td>'+p.lev+'x</td>'
        +'<td>'+Number(p.entry).toFixed(2)+'</td><td>'+(p.now==null?'—':Number(p.now).toFixed(2))+'</td>'
        +'<td class="'+cls(p.pnl_pct)+'"><b>'+(p.pnl_pct>0?'+':'')+p.pnl_pct+'%</b></td>'
        +'<td class="'+cls(p.pnl_usd)+'">'+(p.pnl_usd>0?'+':'')+'$'+Math.abs(p.pnl_usd).toFixed(2)+'</td>'
        +'<td class="'+cls(p.uR)+'">'+sign(p.uR)+'R</td>'
        +'<td><button class="btn mini danger" onclick="closePos(\\''+p.id+'\\')">Close</button></td></tr>';});
    oh.innerHTML=r+'</table>';}
  // scan grid
  var cl=document.getElementById('closest');
  cl.textContent=data.closest?('Closest setup: '+data.closest):(s.open_count?'Fully deployed ('+s.open_count+' open) — not scanning for more right now.':'Scanning…');
  renderScan(data.analysis);
  var syms=[];(data.analysis||[]).forEach(function(x){if(syms.indexOf(x.symbol)<0)syms.push(x.symbol);});
  if(!syms.length&&s.open)s.open.forEach(function(p){if(syms.indexOf(p.symbol)<0)syms.push(p.symbol);});
  buildProCharts(syms);
  // recent trades
  var rc=document.getElementById('recent');var j=s.recent||[];
  if(!j.length){rc.innerHTML='<div class="empty">No closed trades yet.</div>';}
  else{var rt='<table><tr><th>Market</th><th>TF</th><th>Side</th><th>Result</th><th>R</th></tr>';
    j.forEach(function(e){rt+='<tr><td>'+(e.symbol||'').split('/')[0]+'</td><td>'+e.tf+'</td><td class="'+(e.dir>0?"long":"short")+'">'+(e.dir>0?'LONG':'SHORT')+'</td><td class="dim">'+e.result+'</td><td class="'+cls(e.r)+'">'+sign(e.r)+'R</td></tr>';});
    rc.innerHTML=rt+'</table>';}
  // config footer + charts
  var costTxt=s.costs?(' · costs: fee '+s.costs.fee.toFixed(2)+'%/side, slippage '+s.costs.slip.toFixed(2)+'%/side, funding '+s.costs.funding.toFixed(2)+'%/day'):'';
  document.getElementById('cfgline').textContent='Strategy: '+(s.strategy||'reversion')+' · confidence floor '+s.min_conf+'% · timeframes '+(s.timeframes||[]).join(', ')+' · risk '+s.risk_band[0]+'–'+s.risk_band[1]+'% (cap '+s.risk_band[2]+'%) · leverage '+Object.keys(s.lev_map||{}).map(function(k){return k+' '+s.lev_map[k]+'x';}).join(' / ')+costTxt;
}
function hit(url){return fetch(url).then(function(){setTimeout(poll,300);});}
function togglePause(){hit(window._paused?'/api/resume':'/api/pause');}
function closePos(id){if(confirm('Close this position now at market?'))hit('/api/close?id='+encodeURIComponent(id));}
function closeAll(){if(confirm('Close ALL open positions now at market?'))hit('/api/closeall');}
function poll(){
  fetch('/api/state').then(function(r){return r.json();}).then(function(d){
    document.getElementById('clock').textContent='Last update '+new Date().toLocaleTimeString();
    var fresh=d.status&&d.now&&d.status.ts&&(d.now-d.status.ts)<120;
    setLive(true, fresh?'live — bot is running':'connected, bot is starting up…');
    renderLog(d.log);
    render(d);
  }).catch(function(){setLive(false,'waiting — is the bot running? (python trader.py)');
    document.getElementById('clock').textContent='';});
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
        def _ok(self):
            self._send(200, b'{"ok":true}', "application/json")
        def do_GET(self):
            p = self.path
            # ---- manual controls (localhost only) ----
            if p.startswith("/api/pause"):
                CONTROL["paused"] = True; log.info("PAUSE requested from the monitor."); return self._ok()
            if p.startswith("/api/resume"):
                CONTROL["paused"] = False; log.info("RESUME requested from the monitor."); return self._ok()
            if p.startswith("/api/closeall"):
                CONTROL["close_all"] = True; log.info("CLOSE-ALL requested from the monitor."); return self._ok()
            if p.startswith("/api/close"):
                from urllib.parse import urlparse, parse_qs
                pid = (parse_qs(urlparse(p).query).get("id") or [""])[0]
                if pid:
                    CONTROL["close"].add(pid); log.info("Close requested for position %s", pid)
                return self._ok()
            if p.startswith("/api/state"):
                payload = json.dumps({
                    "status": LATEST.get("status"),
                    "analysis": LATEST.get("analysis"),
                    "closest": LATEST.get("closest"),
                    "scan_ts": LATEST.get("scan_ts"),
                    "log": list(LOG_LINES)[-80:],
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
    # publish an immediate boot status so the page shows life before the first scan
    mode0 = "DRY-RUN (no real money)" if CFG["DRY_RUN"] else ("DEMO" if CFG["USE_DEMO"] else "LIVE REAL MONEY")
    LATEST["status"] = {
        "mode": mode0, "balance": 100.0, "cum_r": 0, "closed": 0, "win_rate": 0,
        "open_count": 0, "open": [], "recent": [], "trigger": CFG["TRIGGER"],
        "min_conf": CFG["MIN_CONFIDENCE"], "timeframes": [t.strip() for t in CFG["TIMEFRAMES"]],
        "risk_band": [CFG["RISK_MIN_FRAC"] * 100, CFG["RISK_MAX_FRAC"] * 100, CFG["RISK_CAP_FRAC"] * 100],
        "lev_map": CFG["MAX_LEVERAGE_MAP"],
        "costs": {"fee": CFG["FEE_RATE"] * 100, "slip": CFG["SLIPPAGE"] * 100, "funding": CFG["FUNDING_DAILY"] * 100},
        "paused": CONTROL["paused"], "ts": time.time(),
    }
    log.info("Connecting to the exchange and loading markets…")
    ex = Exchange()
    log.info("Connected. Scanning %s on %s every %ds. First results within a minute.",
             ",".join(b.strip() for b in CFG["SYMBOLS"]), ",".join(t.strip() for t in CFG["TIMEFRAMES"]),
             CFG["POLL_SECONDS"])
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

            # 2) scan for new entries (unless the user paused new trades)
            if trading_allowed and not CONTROL["paused"]:
                scan_and_trade(ex, state, equity)
            elif CONTROL["paused"]:
                log.info("PAUSED by user — managing open positions only, no new trades.")

            save_state(state)
            log_status(ex, state)
        except Exception as e:
            log.exception("loop error: %s", e)
        # interruptible wait so manual Close / Close-all act within ~2s, not a full cycle
        waited = 0
        while waited < CFG["POLL_SECONDS"]:
            if CONTROL["close"] or CONTROL["close_all"]:
                break
            step = min(2, CFG["POLL_SECONDS"] - waited)
            time.sleep(step); waited += step

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
        cur = None; move = 0
        lev = p.get("lev", 1); inv = p.get("invested", 0)
        try:
            cur = ex.candles(p["symbol"], p["tf"], 2)[-1]["c"]
            uR = p["dir"] * (cur - p["entry"]) / p["risk"] if p["risk"] else 0
            move = p["dir"] * (cur - p["entry"]) / p["entry"] if p["entry"] else 0
            pnl_pct = move * lev * 100          # % return on the margin (leveraged)
            pnl_usd = move * inv * lev          # $ gain/loss on this position
            log.info("  OPEN %s %s %s | entry %.2f now %.2f | %+.2fR | %+.1f%% (%+.2f$)",
                     "LONG" if p["dir"] > 0 else "SHORT", p["symbol"], p["tf"],
                     p["entry"], cur, uR, pnl_pct, pnl_usd)
        except Exception:
            uR = 0; pnl_pct = 0; pnl_usd = 0
            log.info("  OPEN %s %s %s | entry %.2f",
                     "LONG" if p["dir"] > 0 else "SHORT", p["symbol"], p["tf"], p["entry"])
        open_view.append({
            "id": p.get("id"), "symbol": p["symbol"].split("/")[0], "tf": p["tf"],
            "dir": "LONG" if p["dir"] > 0 else "SHORT", "entry": p["entry"], "now": cur,
            "uR": round(uR, 2), "lev": round(lev, 1), "invested": round(inv, 2),
            "pnl_pct": round(pnl_pct, 1), "pnl_usd": round(pnl_usd, 2),
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
        "trigger": CFG["TRIGGER"], "strategy": CFG["STRATEGY"], "min_conf": CFG["MIN_CONFIDENCE"],
        "timeframes": [t.strip() for t in CFG["TIMEFRAMES"]],
        "risk_band": [CFG["RISK_MIN_FRAC"] * 100, CFG["RISK_MAX_FRAC"] * 100, CFG["RISK_CAP_FRAC"] * 100],
        "lev_map": CFG["MAX_LEVERAGE_MAP"],
        "costs": {"fee": CFG["FEE_RATE"] * 100, "slip": CFG["SLIPPAGE"] * 100, "funding": CFG["FUNDING_DAILY"] * 100},
        "paused": CONTROL["paused"], "ts": time.time(),
    }

def scan_and_trade(ex, state, equity):
    open_syms = {p["symbol"] for p in state["positions"] if p["status"] == "open"}
    if len(open_syms) >= CFG["MAX_CONCURRENT"]:
        return
    invested = sum(p.get("invested", 0) for p in state["positions"] if p["status"] == "open")
    candidates = []
    near = []   # near-misses: (strength, "SYMBOL tf DIR — why it was blocked")
    grid = []   # one row per chart scanned, for the live web view
    for symbol in ex.trade_symbols:
        if not symbol or symbol in open_syms:
            continue
        for tf in [t.strip() for t in CFG["TIMEFRAMES"]]:
            if tf == "1m":     # scan-only, never a primary trade
                continue
            try:
                candles = ex.candles(symbol, tf, 400)
            except Exception as e:
                log.warning("candles %s %s failed: %s", symbol, tf, e)
                continue
            if len(candles) < 210:
                continue
            last = len(candles) - 1
            sig, ind = signal_at(candles, last)
            base = symbol.split("/")[0]
            # every scanned chart gets a row (with candle data so the page can draw it)
            row = {"symbol": base, "tf": tf, "status": "watching", "reason": "",
                   "dir": None, "pattern": None, "conf": None, "marker": None,
                   "candles": _mini_candles(candles, 60), "levels": None, "plan": None,
                   "backtest": None, "price": round(ind["price"], 2)}
            grid.append(row)
            if not sig:
                row["reason"] = ("no bullish/bearish reversal pattern at an extreme"
                                 if CFG["STRATEGY"] == "reversion" else "no momentum signal")
                continue
            d = sig["dir"]; dirtxt = "LONG" if d > 0 else "SHORT"
            row["dir"] = dirtxt
            row["pattern"] = ", ".join(sig["patterns"]) or sig["ctx"]
            row["marker"] = len(row["candles"]) - 1   # the signal is on the latest candle
            row["levels"] = {"support": round(sig["support"], 2), "resistance": round(sig["resistance"], 2)}

            def blocked(reason):
                row["status"] = "blocked"; row["reason"] = reason
                near.append((sig["strength"], "%s %s %s — %s" % (base, tf, dirtxt, reason)))

            # PROVEN-EDGE gate: replay THIS strategy over history, net of costs + OOS
            bt = backtest(candles, TF_MIN.get(tf, 60))
            oos = bt.get("oos", {"n": 0, "avg_r": 0, "profit_factor": 0})
            row["backtest"] = {"n": bt["n"], "win_rate": round(bt["win_rate"]),
                               "pf": round(bt["profit_factor"], 2), "avg_r": round(bt["avg_r"], 2),
                               "gross_avg_r": round(bt.get("gross_avg_r", 0), 2),
                               "oos_n": oos["n"], "oos_pf": round(oos["profit_factor"], 2),
                               "oos_avg_r": round(oos["avg_r"], 2)}
            if bt["n"] >= 6 and (bt["avg_r"] <= 0 or bt["profit_factor"] < 1.15):
                blocked("this pattern hasn't paid AFTER costs here (PF %.2f over %d)"
                        % (bt["profit_factor"], bt["n"])); continue
            if oos["n"] >= 6 and oos["avg_r"] <= 0:
                blocked("pattern fails out-of-sample (recent PF %.2f)" % oos["profit_factor"]); continue

            conf = (reversion_confidence(sig["strength"], sig["extremity"], bt)
                    if CFG["STRATEGY"] == "reversion" else confidence(ind, bt, 0, 0, 0))
            row["conf"] = conf
            price = ind["price"]; atr = ind["atr"]
            stop = price - d * CFG["STOP_ATR"] * atr
            tgt = price + d * CFG["TARGET_ATR"] * atr
            row["plan"] = {"entry": round(price, 2), "stop": round(stop, 2), "target": round(tgt, 2),
                           "rr": round(CFG["TARGET_ATR"] / CFG["STOP_ATR"], 2)}
            if conf < CFG["MIN_CONFIDENCE"]:
                blocked("confidence %d%%, needs %d%%" % (conf, int(CFG["MIN_CONFIDENCE"]))); continue
            if in_cooldown(state["journal"], symbol, TF_MIN.get(tf, 60)):
                blocked("cooldown after a recent trade"); continue
            row["status"] = "candidate"
            candidates.append({"symbol": symbol, "tf": tf, "dir": d,
                               "score": d * int(sig["strength"] * 100), "conf": conf, "an": ind,
                               "regime": sig["ctx"], "pattern": row["pattern"]})
    grid.sort(key=lambda r: (r["conf"] if r["conf"] is not None else -1), reverse=True)
    LATEST["analysis"] = grid
    LATEST["scan_ts"] = time.time()
    LATEST["closest"] = (sorted(near, key=lambda x: x[0], reverse=True)[0][1] if near else None)
    if not candidates:
        if near:
            log.info("No trade this scan. Closest: %s", sorted(near, key=lambda x: x[0], reverse=True)[0][1])
        else:
            log.info("No trade this scan — no reversal pattern at a tradeable extreme yet.")
        return
    candidates.sort(key=lambda c: c["conf"], reverse=True)
    best = candidates[0]
    log.info("PATTERN FOUND: %s %s %s (%s) conf=%d%% — placing trade",
             "LONG" if best["dir"] > 0 else "SHORT", best["symbol"], best["tf"], best["pattern"], best["conf"])
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
        "id": "%s|%s|%d" % (d["symbol"], d["tf"], int(time.time())),
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
            candles = None
        # MANUAL CLOSE from the web page (per-position button or "close all")
        if CONTROL["close_all"] or p.get("id") in CONTROL["close"]:
            try:
                cur = candles[-1]["c"] if candles else ex.candles(p["symbol"], p["tf"], 2)[-1]["c"]
            except Exception:
                cur = p["entry"]
            uR = p["dir"] * (cur - p["entry"]) / p["risk"] if p["risk"] else 0
            log.info("MANUAL CLOSE requested from the monitor — closing %s %s", p["symbol"], p["tf"])
            close_trade(ex, state, p, cur, "manual", uR)
            CONTROL["close"].discard(p.get("id"))
            continue
        if candles is None:
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
    CONTROL["close_all"] = False   # one-shot: handled this pass

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
