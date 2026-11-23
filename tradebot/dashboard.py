"""Writes the dashboard: docs/index.html + docs/data.js (opens from disk or GitHub Pages)."""
import datetime as dt
import json
import shutil
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"


def build(state, cfg, out_dir="docs", report=None, demo=None):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "config": {
            "broker": cfg["account"]["broker"],
            "starting_cash": state.get("starting_cash") or cfg["account"]["starting_cash"],
            "paper_test_days": cfg["account"]["paper_test_days"],
            "allocation": cfg["allocation"],
            "crypto": {k: cfg["crypto"][k] for k in ("symbols", "lookbacks", "vol_target", "max_exposure", "band", "fee")},
            "nasdaq": {k: cfg["nasdaq"][k] for k in ("signal_symbol", "risk_on", "risk_off", "sma_days")},
            "halt_at": cfg["safety"]["max_drawdown_halt"],
            "reference": cfg["reference"],
        },
        "state": {k: state.get(k) for k in (
            "start_date", "last_run", "runs", "peak_equity", "halted", "halt_reason",
            "pending_stock", "equity", "events", "signals", "positions_view",
            "sleeve_cash", "prices", "data_log", "last_stock_bar", "last_crypto_bar")},
        "trades": state.get("trades", [])[-1000:],
        "last_report": report,
        "demo": demo,
    }
    js = "window.BOT_DATA = " + json.dumps(data, default=float, separators=(",", ":")) + ";\n"
    (out / "data.js").write_text(js)
    (out / "data.json").write_text(json.dumps(data, default=float))
    shutil.copyfile(TEMPLATE, out / "index.html")
    (out / ".nojekyll").write_text("")
    return out / "index.html"
