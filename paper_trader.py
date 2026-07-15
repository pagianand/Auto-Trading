#!/usr/bin/env python3
"""
Paper trading engine — runs the strategies from the watchlist every time it's invoked.
Designed to be called on a schedule by a GitHub Action (see .github/workflows/watchlist.yml).

Strategies:
  - SNOW, AMD, GOOGL, ROKU, META, ARM: intraday VWAP mean-reversion, trend-filtered
      BUY  when RSI(2) < 25 AND price is >0.2% below today's session VWAP
           AND price is above its 200-day SMA (only buy dips in an uptrend)
      EXIT when price crosses back above VWAP, or at end of trading day
  - TSLA: 15-minute opening range breakout (ORB)
      BUY  on a close above the high of the first 15 minutes of trading
      STOP if price falls back below the opening range low
      EXIT otherwise at end of trading day

Data source: Yahoo Finance via the `yfinance` package (free, no API key required).
State: positions and trade history are stored in data/state.json and data/trades.csv,
which the GitHub Action commits back to the repo after each run so state persists
between runs.

IMPORTANT: this places no real orders. It only simulates trades and logs them.
"""

import json
import os
import sys
from datetime import datetime, timezone, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(REPO_ROOT, "data", "state.json")
TRADES_CSV = os.path.join(REPO_ROOT, "data", "trades.csv")

ET = ZoneInfo("America/New_York")

WATCHLIST = {
    "SNOW": {"strategy": "vwap_meanrev"},
    "AMD": {"strategy": "vwap_meanrev"},
    "GOOGL": {"strategy": "vwap_meanrev"},
    "ROKU": {"strategy": "vwap_meanrev"},
    "META": {"strategy": "vwap_meanrev"},
    "ARM": {"strategy": "vwap_meanrev"},
    "TSLA": {"strategy": "orb"},
}

MEANREV_ENTRY_RSI = 25
MEANREV_MIN_DIST_PCT = 0.2


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"open_positions": {}, "last_run": None}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, default=str)


def append_trade(row):
    os.makedirs(os.path.dirname(TRADES_CSV), exist_ok=True)
    file_exists = os.path.exists(TRADES_CSV)
    df = pd.DataFrame([row])
    df.to_csv(TRADES_CSV, mode="a", header=not file_exists, index=False)


def market_is_open(now_et):
    if now_et.weekday() >= 5:  # Sat/Sun
        return False
    open_t, close_t = dtime(9, 30), dtime(16, 0)
    return open_t <= now_et.time() <= close_t


def rsi2(closes: pd.Series) -> float:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(2).mean()
    avg_loss = loss.rolling(2).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - 100 / (1 + rs)
    rsi = rsi.where(avg_loss != 0, 100)
    return float(rsi.iloc[-1])


def fetch_today_intraday(symbol):
    """Pull today's 5-minute bars for a symbol via yfinance."""
    t = yf.Ticker(symbol)
    df = t.history(period="1d", interval="5m", prepost=False)
    if df.empty:
        return None
    df = df.reset_index()
    df.rename(columns={"Datetime": "dt", "Close": "close", "High": "high",
                        "Low": "low", "Volume": "volume"}, inplace=True)
    return df


def is_above_200sma(symbol):
    """Trend filter: only allow mean-reversion longs when price is above its
    200-day simple moving average, i.e. the stock is in a longer-term uptrend.
    Returns True/False, or None if there isn't enough daily history yet."""
    t = yf.Ticker(symbol)
    daily = t.history(period="300d", interval="1d")
    if daily.empty or len(daily) < 200:
        return None
    sma200 = daily["Close"].rolling(200).mean().iloc[-1]
    last_close = daily["Close"].iloc[-1]
    return bool(last_close > sma200)


def evaluate_vwap_meanrev(symbol, state):
    df = fetch_today_intraday(symbol)
    if df is None or len(df) < 3:
        print(f"[{symbol}] no intraday data yet, skipping")
        return

    df["pv"] = df["close"] * df["volume"]
    vwap = df["pv"].sum() / df["volume"].sum()
    price = float(df["close"].iloc[-1])
    dist_pct = (price - vwap) / vwap * 100
    rsi = rsi2(df["close"])

    open_pos = state["open_positions"].get(symbol)
    now_iso = datetime.now(timezone.utc).isoformat()

    if open_pos is None:
        trend_ok = is_above_200sma(symbol)
        trend_note = "above 200SMA" if trend_ok else ("below 200SMA" if trend_ok is False else "200SMA unknown")
        signal_met = rsi < MEANREV_ENTRY_RSI and dist_pct < -MEANREV_MIN_DIST_PCT
        if signal_met and trend_ok:
            state["open_positions"][symbol] = {
                "strategy": "vwap_meanrev",
                "entry_price": price,
                "entry_time": now_iso,
            }
            print(f"[{symbol}] ENTER long @ {price:.2f} (RSI2={rsi:.1f}, dist_vwap={dist_pct:.2f}%, {trend_note})")
        elif signal_met and not trend_ok:
            print(f"[{symbol}] signal met but BLOCKED by trend filter "
                  f"(price={price:.2f}, vwap={vwap:.2f}, dist={dist_pct:.2f}%, rsi2={rsi:.1f}, {trend_note})")
        else:
            print(f"[{symbol}] no entry (price={price:.2f}, vwap={vwap:.2f}, "
                  f"dist={dist_pct:.2f}%, rsi2={rsi:.1f}, {trend_note})")
    else:
        now_et = datetime.now(ET)
        force_close = now_et.time() >= dtime(15, 55)
        if dist_pct >= 0 or force_close:
            entry_price = open_pos["entry_price"]
            ret_pct = (price - entry_price) / entry_price * 100
            append_trade({
                "symbol": symbol, "strategy": "vwap_meanrev",
                "entry_time": open_pos["entry_time"], "exit_time": now_iso,
                "entry_price": entry_price, "exit_price": price,
                "return_pct": round(ret_pct, 3), "forced_close": force_close,
            })
            del state["open_positions"][symbol]
            print(f"[{symbol}] EXIT @ {price:.2f} return={ret_pct:.2f}% "
                  f"({'forced EOD close' if force_close else 'VWAP cross'})")
        else:
            print(f"[{symbol}] holding position, entry={open_pos['entry_price']:.2f} "
                  f"current={price:.2f}")


def evaluate_orb(symbol, state):
    df = fetch_today_intraday(symbol)
    if df is None or len(df) < 4:
        print(f"[{symbol}] not enough bars yet for opening range, skipping")
        return

    range_high = df["high"].iloc[:3].max()
    range_low = df["low"].iloc[:3].min()
    price = float(df["close"].iloc[-1])

    open_pos = state["open_positions"].get(symbol)
    now_iso = datetime.now(timezone.utc).isoformat()
    now_et = datetime.now(ET)
    force_close = now_et.time() >= dtime(15, 55)

    if open_pos is None:
        already_entered_today = state.get("orb_entered_date", {}).get(symbol) == now_et.date().isoformat()
        if not already_entered_today and price > range_high:
            state["open_positions"][symbol] = {
                "strategy": "orb", "entry_price": price, "entry_time": now_iso,
            }
            state.setdefault("orb_entered_date", {})[symbol] = now_et.date().isoformat()
            print(f"[{symbol}] ENTER long @ {price:.2f} (breakout above range high {range_high:.2f})")
        else:
            print(f"[{symbol}] no breakout (price={price:.2f}, range={range_low:.2f}-{range_high:.2f})")
    else:
        entry_price = open_pos["entry_price"]
        stop_hit = price < range_low
        if stop_hit or force_close:
            ret_pct = (price - entry_price) / entry_price * 100
            append_trade({
                "symbol": symbol, "strategy": "orb",
                "entry_time": open_pos["entry_time"], "exit_time": now_iso,
                "entry_price": entry_price, "exit_price": price,
                "return_pct": round(ret_pct, 3), "forced_close": force_close,
            })
            del state["open_positions"][symbol]
            print(f"[{symbol}] EXIT @ {price:.2f} return={ret_pct:.2f}% "
                  f"({'forced EOD close' if force_close else 'stop hit'})")
        else:
            print(f"[{symbol}] holding position, entry={entry_price:.2f} current={price:.2f}")


def main():
    now_et = datetime.now(ET)
    print(f"=== Watchlist check @ {now_et.isoformat()} ===")

    if not market_is_open(now_et):
        print("Market is closed (outside 9:30-16:00 ET, or weekend). Skipping.")
        return

    state = load_state()

    for symbol, cfg in WATCHLIST.items():
        try:
            if cfg["strategy"] == "vwap_meanrev":
                evaluate_vwap_meanrev(symbol, state)
            elif cfg["strategy"] == "orb":
                evaluate_orb(symbol, state)
        except Exception as e:
            print(f"[{symbol}] ERROR: {e}", file=sys.stderr)

    state["last_run"] = now_et.isoformat()
    save_state(state)
    print("=== Done ===")


if __name__ == "__main__":
    main()
