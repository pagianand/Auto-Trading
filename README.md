# Paper Trading Watchlist Bot

Runs three backtested intraday strategies every 5 minutes during US market hours,
using free Yahoo Finance data, and logs *simulated* trades — no real orders are
ever placed.

| Symbol | Strategy | Backtested (Apr 20 – Jul 14, 2026) |
|---|---|---|
| AMD  | VWAP mean-reversion | 144 trades, 77.8% win rate, 1.70 profit factor |
| META | VWAP mean-reversion | 111 trades, 73.9% win rate, 1.36 profit factor |
| TSLA | 15-min opening range breakout | 32 trades, 62.5% win rate, 2.82 profit factor |

Backtest numbers are historical, from a 3-month sample, and are not a guarantee
of future performance. Treat this as a research/monitoring tool, not investment
advice.

## How it works

- `paper_trader.py` pulls today's 5-minute bars for AMD, META, and TSLA from
  Yahoo Finance (via the free `yfinance` package — no API key needed), computes
  each strategy's signal, and opens/closes simulated positions accordingly.
- State (open positions) lives in `data/state.json`; every closed trade is
  appended to `data/trades.csv`.
- `.github/workflows/watchlist.yml` runs the script on a schedule and commits
  the updated `data/` files back to the repo, so history persists across runs.

## Setup

1. **Create the repo** (if you haven't already):
   ```bash
   git init amd-paper-trader
   cd amd-paper-trader
   # copy all these files in
   git add .
   git commit -m "Initial paper trading bot"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
   git push -u origin main
   ```

2. **Enable Actions**: go to your repo's **Actions** tab on GitHub and enable
   workflows if prompted (they're on by default for public repos, but private
   repos sometimes need a click to confirm).

3. **Check permissions**: Settings → Actions → General → Workflow permissions
   → set to **"Read and write permissions"**. This lets the workflow commit
   the updated trade log back to the repo.

4. That's it. It will start running automatically every 5 minutes, Mon-Fri,
   13:30-20:00 UTC (roughly 9:30am-4pm ET, adjusting is not automatic across
   DST changes — see note below).

## Running it manually

Go to **Actions → Paper Trading Watchlist → Run workflow** to trigger an
immediate check without waiting for the schedule. Useful for testing.

## Honest limitations

- **GitHub's cron scheduler is best-effort.** Under platform load, scheduled
  runs can be delayed by several minutes — don't rely on this for anything
  time-critical.
- **The UTC window doesn't auto-adjust for Daylight Saving Time.** As written,
  `13-20 UTC` matches 9:30am-4pm ET during EDT (summer). During EST (winter)
  it'll be off by an hour. Adjust the cron expression around DST changes, or
  widen the window and let the script's own `market_is_open()` check handle
  the exact cutoff (it already does — the cron window just needs to be wide
  enough to contain market hours).
- **Free-tier GitHub Actions has a monthly minutes cap** (2,000 min/month on
  the free plan for private repos; public repos are unlimited). At ~1 min per
  run, 5-minute intervals for ~6.5 hours a day, 5 days a week, that's roughly
  350 runs/week — comfortably within free limits, but worth knowing if you
  add more symbols or strategies.
- **Yahoo Finance data via `yfinance` is unofficial** and can occasionally be
  delayed, rate-limited, or briefly unavailable. It's fine for monitoring, not
  something to build real order execution on without a proper market data
  subscription.
- **This places no real trades.** To actually act on these signals, you'd
  need to separately connect a real brokerage API and add explicit order
  placement — a meaningfully bigger step with real financial risk, worth
  doing carefully and separately from this monitoring tool.
