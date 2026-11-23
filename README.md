# Trend Bot

A paper-trading bot for the "aggressive" strategy from the research:

| Sleeve | Share | Rule | Checked |
|---|---|---|---|
| Crypto | 50% | Hold BTC/ETH sized by trend score × 50% ÷ volatility ÷ 2; rest in cash | Weekly (Monday UTC) |
| Nasdaq | 50% | QQQ above its 200-day average → hold **QLD** (2x Nasdaq-100); below → **SHY** (Treasuries). Switch the next trading day | Every US close |
| Both | | Reset to 50/50 on the first US trading day of each month (skipped if within 2%) | Monthly |

**Every 10 minutes** GitHub runs a live check: fresh prices, account value, and alerts. It never trades on these checks (the strategy was tested on daily closes). The daily trade check runs on the first live check after 21:30 UTC (about 10:30am NZ), and any later check catches up if that one was missed.

Backtest, Jan 2018 – May 2026, after fees: about 28–32% a year, worst drop about -45%. About 2 months in 5 lost money. Not a promise.

## Safety built in
- **Paper only by default.** The Alpaca adapter refuses any non-paper endpoint.
- **Halts** if the account falls 55% below its peak (`python -m tradebot resume` after review).
- **Won't trade on bad data**: stale prices, stock series out of sync, or missing sources skip that sleeve for the day.
- **Blocks oversized orders**, never spends cash it doesn't have, never borrows.
- **Runs once a day**: repeat runs the same day do nothing; a day with no data can be retried.
- Every decision is written to the log on the dashboard's Activity tab.

## The app (Mac)
Open the dashboard in **Safari → File → Add to Dock** (macOS Sonoma or later) or **Chrome → ⋮ → Cast, save, and share → Install page as app**. It opens in its own window with its own icon, reloads live prices every minute, and has ⌘1–⌘7 shortcuts. Sections: Overview, Charts, Positions, Trades, Signals, Activity (with a live data check), How it works.

## Alerts on your phone or Mac (ntfy, free)
1. Install the **ntfy** app (iPhone/Android) or open ntfy.sh/app in a browser.
2. Pick a long random topic name, made of random letters and numbers (not a word or your name), and subscribe to it in the app.
3. In the repo: **Settings → Secrets and variables → Actions → New repository secret**, name `NTFY_TOPIC`, value = your topic.
You'll get every trade, signal change, warning, halt, a 4% intraday move (once a day) and a daily summary. Anyone who knows the topic name can read it, so keep it random.

## Run it for the 1-month paper test (recommended: GitHub, free, laptop can be off)
1. Create a new GitHub repository (public is simplest: GitHub Pages for private repos needs a paid plan. Nothing secret is stored in the repo).
2. Upload this folder to it (or `git push`).
3. In the repo: **Settings → Actions → General → Workflow permissions → Read and write**.
4. **Settings → Pages → Deploy from a branch → `main` / `/docs`**. Your dashboard will be at `https://<you>.github.io/<repo>/`.
5. **Actions → Trend Bot → Run workflow** to do the first check now. After that it runs itself every 10 minutes (GitHub sometimes delays scheduled runs by a few minutes).
6. Optional but recommended: make a free Alpaca account, open its **paper** dashboard, create API keys, and add them under **Settings → Secrets and variables → Actions** as `ALPACA_KEY_ID` and `ALPACA_SECRET_KEY`. With keys present the bot can fall back to Alpaca's market data if Yahoo blocks GitHub's servers. To route paper orders through Alpaca instead of the built-in simulator, also set `broker = "alpaca"` in `config.toml`.

If a run fails (for example every data source is down), the workflow turns red and GitHub emails you.

## Or run it on your Mac
```bash
cd tradebot
python3 -m tradebot run        # one daily cycle (needs Python 3.11+)
open docs/index.html           # the dashboard
python3 -m tradebot status     # quick summary in the terminal
```
To run daily automatically, edit the path inside `scripts/com.trendbot.daily.plist`, then:
```bash
cp scripts/com.trendbot.daily.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.trendbot.daily.plist
```

## Commands
| Command | What it does |
|---|---|
| `python -m tradebot refresh` | Live prices + account value; runs the daily cycle if due (what GitHub runs every 10 min) |
| `python -m tradebot run` | Run today's trading cycle now |
| `python -m tradebot test-alert` | Send a test notification (needs `NTFY_TOPIC`) |
| `python -m tradebot status` | Print equity, positions, pending switch, halt state |
| `python -m tradebot dashboard` | Rebuild the dashboard from saved state |
| `python -m tradebot resume` | Clear a safety halt |
| `python -m tradebot reset --yes` | Start the paper account over |

## Tests
```bash
python -m unittest discover -s tests -v   # 28 tests: strategy maths, fees, parsers, schedule, halts, live checks, alerts, Alpaca (mock server)
```
`tests/replay_check.py` replays the bot day by day over 2018–2026 history and `tests/render_check.py` opens the dashboard in Chrome. Both need the research data files and are not run in GitHub Actions.

## Data sources
Crypto: Kraken public API (fallback Coinbase). Stocks: Yahoo Finance chart API (fallbacks Nasdaq.com, then Alpaca if keys are set). No API keys are needed for the default setup.

## After the paper month, before real money
- Every day ran, no unexplained warnings on the Activity tab.
- Each trade matches the rules above.
- Decide where real money goes: crypto at Kraken, QLD/SHY at Interactive Brokers (or both at one broker that offers both). The monthly 50/50 reset then means moving cash between brokers yourself.
- NZ tax: trading gains are taxable income. Keep the trade log.

## Files
```
config.toml               all settings
tradebot/strategy.py      signal maths (pure functions)
tradebot/bot.py           the daily cycle
tradebot/broker.py        paper simulator + Alpaca paper adapter
tradebot/data.py          price sources
tradebot/dashboard.py     writes docs/ (index.html + data.js)
dashboard/index.html      dashboard template
state/state.json          the bot's memory (created on first run)
.github/workflows/bot.yml daily schedule
```
