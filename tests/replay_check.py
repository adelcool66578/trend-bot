"""Replay the real bot code day by day over history and compare with the backtest.

Uses Coin Metrics BTC/ETH, QQQ and SHY ETF data; QLD is built from QQQ (2x daily,
minus financing and fees) exactly as in the backtest. Run from the repo root:
    python tests/replay_check.py [start] [end]
"""
import datetime as dt
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tradebot.bot import run_once
from tradebot.config import load_config
from tradebot.data import ReplayData, load_csv_series

DATA = Path("/home/claude/data")
EXT = Path("/home/claude/ext")


def build_series():
    import pandas as pd
    s = {}
    for sym in ("btc", "eth"):
        s[sym.upper()] = load_csv_series(DATA / f"{sym}.csv", "time", "PriceUSD")
    a = pd.read_csv(EXT / "portfolio-risk-lab/data/raw/etf_adjclose_daily.csv", index_col=0, parse_dates=True)
    q = pd.read_csv(EXT / "tsmom-etf/data/QQQ.csv", parse_dates=["date"]).set_index("date")["close"]
    df = pd.concat([q.rename("QQQ"), a["SHY"]], axis=1, sort=True).dropna()
    r = df["QQQ"].pct_change().fillna(0)
    fin = df["SHY"].pct_change().fillna(0) + 0.005 / 252
    qld = (1 + (2 * r - fin - 0.0095 / 252)).cumprod() * 50
    df["QLD"] = qld
    for c in ("QQQ", "SHY", "QLD"):
        s[c] = [(d.date().isoformat(), float(v)) for d, v in df[c].items()]
    return s


def replay(start, end, cfg, series, quiet=True):
    data = ReplayData(series)
    state = None
    d = dt.date.fromisoformat(start)
    stop = dt.date.fromisoformat(end)
    while d <= stop:
        data.set_date(d.isoformat())
        state, rep = run_once(cfg, state, data, today=d.isoformat())
        if not quiet and rep["fills"]:
            print(d, rep["actions"])
        d += dt.timedelta(days=1)
    return state


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else "2018-01-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-05-23"
    cfg = load_config(ROOT / "config.toml")
    t = time.time()
    st = replay(start, end, cfg, build_series())
    eq = st["equity"]
    yrs = (dt.date.fromisoformat(eq[-1]["date"]) - dt.date.fromisoformat(eq[0]["date"])).days / 365.25
    tot = [p["total"] for p in eq]
    peak, mdd = tot[0], 0
    for v in tot:
        peak = max(peak, v); mdd = min(mdd, v / peak - 1)
    cagr = (tot[-1] / tot[0]) ** (1 / yrs) - 1
    print(f"replay {start}..{end}: {len(eq)} days in {time.time()-t:.1f}s")
    print(f"CAGR {cagr:.1%}  max DD {mdd:.1%}  final ${tot[-1]:,.0f}  trades {len(st['trades'])}  "
          f"halted={st['halted']}  warnings={sum(1 for e in st['events'] if e['level'] in ('warn','error'))}")
    Path(ROOT / "tests" / "replay_state.json").write_text(json.dumps(st, default=float))
