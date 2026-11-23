"""10-minute refresh: live prices and account value, no trading.

The strategy only trades on daily closes (that is what was backtested), so the
refresh never places orders. It does three things:
  1. marks the account to market with the latest prices and records an intraday point,
  2. tells the caller whether the daily cycle is due (missed-run guard),
  3. alerts on a big intraday move (once per day).
Output goes to small files (state/live.json, docs/live.js) so frequent commits stay tiny.
"""
import datetime as dt
import json
import os
from pathlib import Path

from .bot import _sleeve_of
from .data import DataError
from .strategy import nasdaq_signal, trend_score

DAILY_AFTER_UTC = dt.time(21, 30)   # after the US close all year


def daily_due(state, now):
    """True if the daily trading cycle should run now."""
    if state is None:
        return True
    last = state.get("last_run")
    today = now.date()
    if now.time() >= DAILY_AFTER_UTC:
        return last != today.isoformat()
    # before the cut-off: only catch up if a whole day was missed
    return last is None or last < (today - dt.timedelta(days=1)).isoformat()


def snapshot(cfg, state, data, now, live_prev=None):
    """Fetch live prices and value the account. Returns the live dict."""
    c, n = cfg["crypto"], cfg["nasdaq"]
    prices, bars_end, warnings, ages = {}, {}, [], {}
    for s in c["symbols"]:
        try:
            bars, cur = data.crypto(s)
            prices[s] = cur; bars_end[s] = bars[-1][0]
        except DataError as e:
            warnings.append(f"{s}: {str(e)[:160]}")
    stock_closes = {}
    for s in [n["signal_symbol"], n["risk_on"], n["risk_off"]]:
        try:
            bars, cur = data.stock(s)
            prices[s] = cur; bars_end[s] = bars[-1][0]; stock_closes[s] = [b[1] for b in bars]
        except DataError as e:
            warnings.append(f"{s}: {str(e)[:160]}")
    known = dict(state.get("prices", {})) if state else {}
    for s, p in prices.items():
        known[s] = p
    pos = (state or {}).get("paper", {}).get("positions", {})
    cash = (state or {}).get("paper", {}).get("cash", cfg["account"]["starting_cash"])
    sc = (state or {}).get("sleeve_cash", {"crypto": 0, "nasdaq": 0})
    vals = {s: q * known[s] for s, q in pos.items() if q and s in known}
    cv = sum(v for s, v in vals.items() if _sleeve_of(s, cfg) == "crypto")
    nv = sum(v for s, v in vals.items() if _sleeve_of(s, cfg) == "nasdaq")
    equity = cash + cv + nv
    eq = (state or {}).get("equity") or []
    last_daily = eq[-1]["total"] if eq else equity

    # informational: where the signals would stand if today closed at the live price
    q_live = None
    qc = stock_closes.get(n["signal_symbol"])
    if qc and len(qc) >= n["sma_days"]:
        sma = sum(qc[-n["sma_days"]:]) / n["sma_days"]
        q_live = {"price": prices[n["signal_symbol"]], "sma": sma, "gap": prices[n["signal_symbol"]] / sma - 1,
                  "would_hold": n["risk_on"] if prices[n["signal_symbol"]] > sma else n["risk_off"]}
    hist = (state or {}).get("history", {})
    c_live = {}
    for s in c["symbols"]:
        h = [b[1] for b in hist.get(s, [])]
        if h and s in prices:
            sc_live = trend_score(h + [prices[s]], c["lookbacks"])
            c_live[s] = {"price": prices[s], "score": sc_live}

    intraday = list((live_prev or {}).get("intraday", []))
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    intraday.append({"t": stamp, "equity": equity})
    cutoff = (now - dt.timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    intraday = [p for p in intraday if p["t"] >= cutoff][-300:]

    for s, d in bars_end.items():
        ages[s] = (now.date() - dt.date.fromisoformat(d)).days
    return {
        "time": stamp, "prices": prices, "bars_end": bars_end, "bar_age_days": ages,
        "sources": list(getattr(data, "log", []))[-10:], "warnings": warnings,
        "equity": equity, "crypto": cv + sc.get("crypto", 0), "nasdaq": nv + sc.get("nasdaq", 0), "cash": cash,
        "values": vals, "change_vs_last_daily": equity / last_daily - 1 if last_daily else 0.0,
        "last_daily_total": last_daily, "nasdaq_live": q_live, "crypto_live": c_live,
        "intraday": intraday, "alerted_on": (live_prev or {}).get("alerted_on"),
        "summary_sent_on": (live_prev or {}).get("summary_sent_on"),
        "review": (live_prev or {}).get("review"),
        "schedule_confirmed": (live_prev or {}).get("schedule_confirmed"),
        "fail_streak": ((live_prev or {}).get("fail_streak", 0) + 1) if len(warnings) >= 5 else 0,
    }


def load_live(path):
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else None


def save_live(live, state_path, docs_dir):
    p = Path(state_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps(live, default=float)); os.replace(tmp, p)
    d = Path(docs_dir); d.mkdir(parents=True, exist_ok=True)
    (d / "live.js").write_text("window.BOT_LIVE = " + json.dumps(live, default=float, separators=(",", ":")) + ";\n")


def alerts_for(live, cfg, now):
    """Intraday alerts, at most one big-move alert per day."""
    out = []
    move = live["change_vs_last_daily"]
    today = now.date().isoformat()
    if abs(move) >= cfg.get("alerts", {}).get("intraday_move", 0.04) and live.get("alerted_on") != today:
        out.append(("Trend Bot: big move", f"Account {move:+.1%} since the last daily run "
                    f"(${live['equity']:,.2f}). No action needed; the bot only trades on daily closes.",
                    "high", "chart_with_downwards_trend" if move < 0 else "chart_with_upwards_trend"))
        live["alerted_on"] = today
    if live.get("fail_streak") == 6:  # about an hour of failed price fetches
        out.append(("Trend Bot: price feeds down", "No prices for about an hour. The bot will keep retrying.",
                    "default", "warning"))
    return out
