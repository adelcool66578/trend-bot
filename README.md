# Trend Bot

A paper-trading bot for the "aggressive" strategy from the research:

| Sleeve | Share | Rule | Checked |
|---|---|---|---|
| Crypto | 50% | Hold BTC/ETH sized by trend score × 50% ÷ volatility ÷ 2; rest in cash | Weekly (Monday UTC) |
| Nasdaq | 50% | QQQ above its 200-day average → hold **QLD** (2x Nasdaq-100); below → **SHY** (Treasuries). Switch the next trading day | Every US close |
| Both | | Reset to 50/50 on the first US trading day of each month (skipped if within 2%) | Monthly |

Backtest, Jan 2018 – May 2026, after fees: about 28–32% a year, worst drop about -45%. About 2 months in 5 lost money. Not a promise.

## Safety built in
- **Paper only by default.** The Alpaca adapter refuses any non-paper endpoint.
- **Halts** if the account falls 55% below its peak (`python -m tradebot resume` after review).
- **Won't trade on bad data**: stale prices, stock series out of sync, or missing sources skip that sleeve for the day.
- **Blocks oversized orders**, never spends cash it doesn't have, never borrows.
- **Runs once a day**: repeat runs the same day do nothing; a day with no data can be retried.
- Every decision is written to the log on the dashboard's Activity tab.

## Run it for the 1-month paper test (recommended: GitHub, free, laptop can be off)
1. Create a new GitHub repository (public is simplest: GitHub Pages for private repos needs a paid plan. Nothing secret is stored in the repo).
2. Upload this folder to it (or `git push`).
3. In the repo: **Settings → Actions → General → Workflow permissions → Read and write**.
4. **Settings → Pages → Deploy from a branch → `main` / `/docs`**. Your dashboard will be at `https://<you>.github.io/<repo>/`.
5. **Actions → Trend Bot daily run → Run workflow** to do the first run now. After that it runs itself every day at 21:45 UTC (10:45am NZ), with a retry at 23:30 UTC.
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
| `python -m tradebot run` | Fetch prices, run today's cycle, update `docs/` |
| `python -m tradebot status` | Print equity, positions, pending switch, halt state |
| `python -m tradebot dashboard` | Rebuild the dashboard from saved state |
| `python -m tradebot resume` | Clear a safety halt |
| `python -m tradebot reset --yes` | Start the paper account over |

## Tests
```bash
python -m unittest discover -s tests -v   # 24 tests: strategy maths, fees, parsers, schedule, halts, Alpaca (mock server)
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
