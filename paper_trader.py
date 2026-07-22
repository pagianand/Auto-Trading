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
        json.d
