#!/usr/bin/env python3
"""
Paper trading engine — runs the strategies from the watchlist every time it's invoked.

Strategies:
  - vwap_meanrev (SNOW, AMD, GOOGL, ROKU, META, ARM): INTRADAY
      BUY  when RSI(2) < 25 AND price >0.2% below session VWAP AND above 200-day SMA
      EXIT on VWAP cross, or forced at end of day (no overnight)
  - orb (TSLA): INTRADAY
      BUY on close above the first-15-min range high; STOP below range low;
      forced exit at end of day (no overnight)
  - macd_trend (all mean-rev symbols + TSLA): SWING (multi-day, EXEMPT from
      the no-overnight rule by design — this strategy holds days to weeks)
      BUY  when MACD(12,26,9) line crosses above signal line while MACD < 0,
           AND price is above the 200-day SMA
      STOP when price closes below the 200-day SMA
      TARGET at entry + 1.5x (entry - 200SMA at entry)   [video's 1.5R rule]
      Backtest note (Feb 2024-Aug 2026, 7 symbols): 23 trades, profits heavily
      concentrated in AMD; treat as UNPROVEN — paper evaluation only.

Risk rules:
  - Intraday strategies: force-close from 15:45 ET; after-hours reconcile closes
    any intraday position that survived (bug fix). Swing positions are exempt.
  - Earnings blackout: no NEW entries (any strategy) from 2 days before through
    1 day after a scheduled earnings date. Exits are never blocked.

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
    "SNOW": {"strategies": ["vwap_meanrev", "macd_trend"]},
    "AMD": {"strategies": ["vwap_meanrev", "macd_trend"]},
    "GOOGL": {"strategies": ["vwap_meanrev", "macd_trend"]},
    "ROKU": {"strategies": ["vwap_meanrev", "macd_trend"]},
    "META": {"strategies": ["vwap_meanrev", "macd_trend"]},
    "ARM": {"strategies": ["vwap_meanrev", "macd_trend"]},
    "TSLA": {"strategies": ["orb", "macd_trend"]},
}

SWING_STRATEGIES = {"macd_trend"}  # exempt from EOD force-close / reconcile

MEANREV_ENTRY_RSI = 25
MEANREV_MIN_DIST_PCT = 0.2
FORCE_CLOSE_TIME = dtime(15, 45)
EARNINGS_DAYS_BEFORE = 2
EARNINGS_DAYS_AFTER = 1
MACD_RR = 1.5  # reward:risk multiple for the macd_trend profit target


def pos_key(symbol, strategy):
    return f"{symbol}:{strategy}"


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
    if now_et.weekday() >= 5:
        return False
    return dtime(9, 30) <= now_et.time() <= dtime(16, 0)


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
    t = yf.Ticker(symbol)
    df = t.history(period="1d", interval="5m", prepost=False)
    if df.empty:
        return None
    df = df.reset_index()
    df.rename(columns={"Datetime": "dt", "Close": "close", "High": "high",
                        "Low": "low", "Volume": "volume"}, inplace=True)
    return df


def fetch_daily(symbol, period="400d"):
    t = yf.Ticker(symbol)
    daily = t.history(period=period, interval="1d")
    if daily.empty:
        return None
    return daily


def last_available_price(symbol):
    df = fetch_today_intraday(symbol)
    if df is not None and len(df) > 0:
        return float(df["close"].iloc[-1])
    daily = fetch_daily(symbol, "5d")
    if daily is not None and not daily.empty:
        return float(daily["Close"].iloc[-1])
    return None


def close_position(key, state, exit_price, reason):
    open_pos = state["open_positions"].get(key)
    if open_pos is None:
        return
    entry_price = open_pos["entry_price"]
    ret_pct = (exit_price - entry_price) / entry_price * 100
    append_trade({
        "symbol": open_pos.get("symbol", key.split(":")[0]),
        "strategy": open_pos.get("strategy", "unknown"),
        "entry_time": open_pos["entry_time"],
        "exit_time": datetime.now(timezone.utc).isoformat(),
        "entry_price": entry_price, "exit_price": exit_price,
        "return_pct": round(ret_pct, 3),
        "forced_close": reason != "signal",
    })
    del state["open_positions"][key]
    print(f"[{key}] EXIT @ {exit_price:.2f} return={ret_pct:.2f}% ({reason})")


def reconcile_stale_positions(state):
    """Close INTRADAY positions left open after hours. Swing positions are exempt."""
    stale = [k for k, v in state["open_positions"].items()
             if v.get("strategy") not in SWING_STRATEGIES]
    if not stale:
        return False
    print(f"RECONCILE: market closed with open intraday positions {stale} — force-closing.")
    for key in stale:
        symbol = state["open_positions"][key].get("symbol", key.split(":")[0])
        price = last_available_price(symbol)
        if price is None:
            print(f"[{key}] could not fetch a price to reconcile; will retry next run",
                  file=sys.stderr)
            continue
        close_position(key, state, price, "reconcile: closed after hours at last price")
    return True


def is_above_200sma(symbol, daily=None):
    if daily is None:
        daily = fetch_daily(symbol)
    if daily is None or len(daily) < 200:
        return None
    sma200 = daily["Close"].rolling(200).mean().iloc[-1]
    return bool(daily["Close"].iloc[-1] > sma200)


def in_earnings_blackout(symbol):
    try:
        t = yf.Ticker(symbol)
        ed = t.earnings_dates
        if ed is None or ed.empty:
            return False
        today = pd.Timestamp.now(tz="America/New_York").normalize()
        for ts in ed.index:
            try:
                ts = pd.Timestamp(ts)
                if ts.tzinfo is None:
                    ts = ts.tz_localize("America/New_York")
                else:
                    ts = ts.tz_convert("America/New_York")
                delta_days = (ts.normalize() - today).days
                if -EARNINGS_DAYS_AFTER <= delta_days <= EARNINGS_DAYS_BEFORE:
                    return True
            except Exception:
                continue
        return False
    except Exception as e:
        print(f"[{symbol}] earnings lookup failed ({e}); proceeding without blackout",
              file=sys.stderr)
        return False


def evaluate_vwap_meanrev(symbol, state):
    key = pos_key(symbol, "vwap_meanrev")
    df = fetch_today_intraday(symbol)
    if df is None or len(df) < 3:
        print(f"[{key}] no intraday data yet, skipping")
        return
    df["pv"] = df["close"] * df["volume"]
    vwap = df["pv"].sum() / df["volume"].sum()
    price = float(df["close"].iloc[-1])
    dist_pct = (price - vwap) / vwap * 100
    rsi = rsi2(df["close"])
    open_pos = state["open_positions"].get(key)

    if open_pos is None:
        signal_met = rsi < MEANREV_ENTRY_RSI and dist_pct < -MEANREV_MIN_DIST_PCT
        in_close_window = datetime.now(ET).time() >= FORCE_CLOSE_TIME
        if signal_met and not in_close_window:
            if in_earnings_blackout(symbol):
                print(f"[{key}] signal met but BLOCKED: earnings blackout")
                return
            trend_ok = is_above_200sma(symbol)
            if trend_ok is False:
                print(f"[{key}] signal met but BLOCKED: below 200SMA")
                return
            state["open_positions"][key] = {
                "symbol": symbol, "strategy": "vwap_meanrev",
                "entry_price": price,
                "entry_time": datetime.now(timezone.utc).isoformat(),
            }
            print(f"[{key}] ENTER long @ {price:.2f} (RSI2={rsi:.1f}, dist={dist_pct:.2f}%)")
        else:
            print(f"[{key}] no entry (price={price:.2f}, vwap={vwap:.2f}, "
                  f"dist={dist_pct:.2f}%, rsi2={rsi:.1f})")
    else:
        force_close = datetime.now(ET).time() >= FORCE_CLOSE_TIME
        if dist_pct >= 0:
            close_position(key, state, price, "signal")
        elif force_close:
            close_position(key, state, price, "forced EOD close")
        else:
            print(f"[{key}] holding, entry={open_pos['entry_price']:.2f} current={price:.2f}")


def evaluate_orb(symbol, state):
    key = pos_key(symbol, "orb")
    df = fetch_today_intraday(symbol)
    if df is None or len(df) < 4:
        print(f"[{key}] not enough bars yet for opening range, skipping")
        return
    range_high = df["high"].iloc[:3].max()
    range_low = df["low"].iloc[:3].min()
    price = float(df["close"].iloc[-1])
    open_pos = state["open_positions"].get(key)
    now_et = datetime.now(ET)
    force_close = now_et.time() >= FORCE_CLOSE_TIME

    if open_pos is None:
        entered = state.get("orb_entered_date", {}).get(symbol) == now_et.date().isoformat()
        if not entered and price > range_high and not force_close:
            if in_earnings_blackout(symbol):
                print(f"[{key}] breakout but BLOCKED: earnings blackout")
                return
            state["open_positions"][key] = {
                "symbol": symbol, "strategy": "orb", "entry_price": price,
                "entry_time": datetime.now(timezone.utc).isoformat(),
            }
            state.setdefault("orb_entered_date", {})[symbol] = now_et.date().isoformat()
            print(f"[{key}] ENTER long @ {price:.2f} (breakout above {range_high:.2f})")
        else:
            print(f"[{key}] no breakout (price={price:.2f}, range={range_low:.2f}-{range_high:.2f})")
    else:
        if price < range_low:
            close_position(key, state, price, "stop hit")
        elif force_close:
            close_position(key, state, price, "forced EOD close")
        else:
            print(f"[{key}] holding, entry={open_pos['entry_price']:.2f} current={price:.2f}")


def evaluate_macd_trend(symbol, state):
    """SWING strategy from user's video: MACD(12,26,9) cross-up below zero +
    price above 200SMA. Stop below 200SMA; target = entry + 1.5R. Holds
    overnight BY DESIGN. Unproven — paper evaluation."""
    key = pos_key(symbol, "macd_trend")
    daily = fetch_daily(symbol)
    if daily is None or len(daily) < 210:
        print(f"[{key}] insufficient daily history, skipping")
        return
    close = daily["Close"]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    sma200 = close.rolling(200).mean()
    price = float(close.iloc[-1])
    sma_now = float(sma200.iloc[-1])
    cross_up = macd.iloc[-1] > signal.iloc[-1] and macd.iloc[-2] <= signal.iloc[-2]
    open_pos = state["open_positions"].get(key)

    if open_pos is None:
        if cross_up and macd.iloc[-1] < 0 and price > sma_now:
            if in_earnings_blackout(symbol):
                print(f"[{key}] MACD signal but BLOCKED: earnings blackout")
                return
            risk = price - sma_now
            target = price + MACD_RR * risk
            state["open_positions"][key] = {
                "symbol": symbol, "strategy": "macd_trend",
                "entry_price": price, "target_price": target,
                "entry_time": datetime.now(timezone.utc).isoformat(),
            }
            print(f"[{key}] ENTER swing long @ {price:.2f} (stop<{sma_now:.2f}, target {target:.2f})")
        else:
            print(f"[{key}] no MACD entry (macd={macd.iloc[-1]:.2f}, sig={signal.iloc[-1]:.2f}, "
                  f"price={price:.2f}, sma200={sma_now:.2f})")
    else:
        target = open_pos.get("target_price")
        if price < sma_now:
            close_position(key, state, price, "stop: below 200SMA")
        elif target and price >= target:
            close_position(key, state, price, "signal")
        else:
            print(f"[{key}] holding swing, entry={open_pos['entry_price']:.2f} "
                  f"current={price:.2f} target={target:.2f}")


def main():
    now_et = datetime.now(ET)
    print(f"=== Watchlist check @ {now_et.isoformat()} ===")
    state = load_state()

    if not market_is_open(now_et):
        if any(v.get("strategy") not in SWING_STRATEGIES
               for v in state["open_positions"].values()):
            reconcile_stale_positions(state)
            state["last_run"] = now_et.isoformat()
            save_state(state)
        else:
            print("Market closed; no intraday positions to reconcile. Skipping.")
        return

    for symbol, cfg in WATCHLIST.items():
        for strat in cfg["strategies"]:
            try:
                if strat == "vwap_meanrev":
                    evaluate_vwap_meanrev(symbol, state)
                elif strat == "orb":
                    evaluate_orb(symbol, state)
                elif strat == "macd_trend":
                    evaluate_macd_trend(symbol, state)
            except Exception as e:
                print(f"[{symbol}:{strat}] ERROR: {e}", file=sys.stderr)

    state["last_run"] = now_et.isoformat()
    save_state(state)
    print("=== Done ===")


if __name__ == "__main__":
    main()
