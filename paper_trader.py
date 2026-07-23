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

No-overnight guarantee (bug fix):
  - Force-close window starts at 15:45 ET (three cron slots instead of one)
  - If a run happens while the market is CLOSED and positions are still open
    (because the close-window runs were skipped by GitHub's scheduler), those
    positions are immediately closed at the last available price and logged
    with forced_close=True. Positions can no longer survive overnight.

Data source: Yahoo Finance via the `yfinance` package (free, no API key required).
State: positions and trade history are stored in data/state.json and data/trades.csv.

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
FORCE_CLOSE_TIME = dtime(15, 45)  # widened from 15:55 so multiple runs can catch it


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
    """Pull the most recent session's 5-minute bars for a symbol via yfinance."""
    t = yf.Ticker(symbol)
    df = t.history(period="1d", interval="5m", prepost=False)
    if df.empty:
        return None
    df = df.reset_index()
    df.rename(columns={"Datetime": "dt", "Close": "close", "High": "high",
                        "Low": "low", "Volume": "volume"}, inplace=True)
    return df


def last_available_price(symbol):
    """Best-effort last price for closing stale positions when market is closed."""
    df = fetch_today_intraday(symbol)
    if df is not None and len(df) > 0:
        return float(df["close"].iloc[-1])
    t = yf.Ticker(symbol)
    daily = t.history(period="5d", interval="1d")
    if not daily.empty:
        return float(daily["Close"].iloc[-1])
    return None


def close_position(symbol, state, exit_price, reason):
    """Close an open position at exit_price and log the trade."""
    open_pos = state["open_positions"].get(symbol)
    if open_pos is None:
        return
    entry_price = open_pos["entry_price"]
    ret_pct = (exit_price - entry_price) / entry_price * 100
    append_trade({
        "symbol": symbol, "strategy": open_pos.get("strategy", "unknown"),
        "entry_time": open_pos["entry_time"],
        "exit_time": datetime.now(timezone.utc).isoformat(),
        "entry_price": entry_price, "exit_price": exit_price,
        "return_pct": round(ret_pct, 3),
        "forced_close": reason != "signal",
    })
    del state["open_positions"][symbol]
    print(f"[{symbol}] EXIT @ {exit_price:.2f} return={ret_pct:.2f}% ({reason})")


def reconcile_stale_positions(state):
    """Market is closed but positions are still open (close-window runs were
    skipped). Close everything at the last available price. This is the
    overnight-hold bug fix."""
    stale = list(state["open_positions"].keys())
    if not stale:
        return False
    print(f"RECONCILE: market closed with open positions {stale} — force-closing.")
    for symbol in stale:
        price = last_available_price(symbol)
        if price is None:
            print(f"[{symbol}] could not fetch a price to reconcile; will retry next run",
                  file=sys.stderr)
            continue
        close_position(symbol, state, price, "reconcile: closed after hours at last price")
    return True


def is_above_200sma(symbol):
    """Trend filter: only allow mean-reversion longs when price is above its
    200-day simple moving average. Returns True/False, or None if insufficient data."""
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

    if open_pos is None:
        trend_ok = is_above_200sma(symbol)
        trend_note = "above 200SMA" if trend_ok else ("below 200SMA" if trend_ok is False else "200SMA unknown")
        signal_met = rsi < MEANREV_ENTRY_RSI and dist_pct < -MEANREV_MIN_DIST_PCT
        # No fresh entries inside the force-close window
        in_close_window = datetime.now(ET).time() >= FORCE_CLOSE_TIME
        if signal_met and trend_ok and not in_close_window:
            state["open_positions"][symbol] = {
                "strategy": "vwap_meanrev",
                "entry_price": price,
                "entry_time": datetime.now(timezone.utc).isoformat(),
            }
            print(f"[{symbol}] ENTER long @ {price:.2f} (RSI2={rsi:.1f}, dist_vwap={dist_pct:.2f}%, {trend_note})")
        elif signal_met and not trend_ok:
            print(f"[{symbol}] signal met but BLOCKED by trend filter "
                  f"(price={price:.2f}, dist={dist_pct:.2f}%, rsi2={rsi:.1f}, {trend_note})")
        else:
            print(f"[{symbol}] no entry (price={price:.2f}, vwap={vwap:.2f}, "
                  f"dist={dist_pct:.2f}%, rsi2={rsi:.1f}, {trend_note})")
    else:
        force_close = datetime.now(ET).time() >= FORCE_CLOSE_TIME
        if dist_pct >= 0:
            close_position(symbol, state, price, "signal")
        elif force_close:
            close_position(symbol, state, price, "forced EOD close")
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
    now_et = datetime.now(ET)
    force_close = now_et.time() >= FORCE_CLOSE_TIME

    if open_pos is None:
        already_entered_today = state.get("orb_entered_date", {}).get(symbol) == now_et.date().isoformat()
        if not already_entered_today and price > range_high and not force_close:
            state["open_positions"][symbol] = {
                "strategy": "orb", "entry_price": price,
                "entry_time": datetime.now(timezone.utc).isoformat(),
            }
            state.setdefault("orb_entered_date", {})[symbol] = now_et.date().isoformat()
            print(f"[{symbol}] ENTER long @ {price:.2f} (breakout above range high {range_high:.2f})")
        else:
            print(f"[{symbol}] no breakout (price={price:.2f}, range={range_low:.2f}-{range_high:.2f})")
    else:
        if price < range_low:
            close_position(symbol, state, price, "stop hit")
        elif force_close:
            close_position(symbol, state, price, "forced EOD close")
        else:
            print(f"[{symbol}] holding position, entry={open_pos['entry_price']:.2f} current={price:.2f}")


def main():
    now_et = datetime.now(ET)
    print(f"=== Watchlist check @ {now_et.isoformat()} ===")

    state = load_state()

    if not market_is_open(now_et):
        # Bug fix: never skip silently if positions are still open
        if state["open_positions"]:
            reconcile_stale_positions(state)
            state["last_run"] = now_et.isoformat()
            save_state(state)
        else:
            print("Market is closed and no open positions. Skipping.")
        return

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
