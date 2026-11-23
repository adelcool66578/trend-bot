"""Command line:  python -m tradebot <command>

  refresh    every 10 minutes: live prices + account value; runs the daily cycle if due
  run        run today's trading cycle now (after the US close), update the dashboard
  status     print a short summary of the account
  dashboard  rebuild the dashboard from the saved state
  resume     clear a safety halt after you have reviewed it
  reset      start the paper account again from scratch (asks for --yes)
  test-alert send a test notification to your phone/Mac
"""
import argparse
import datetime as dt
import sys

from . import notify
from .bot import load_state, run_once, save_state
from .config import load_config
from .dashboard import build, copy_assets
from .data import LiveData
from .live import alerts_for, daily_due, load_live, save_live, snapshot


def _daily(cfg, state, data, a, force=False):
    state, rep = run_once(cfg, state, data, force=force)
    save_state(state, a.state)
    build(state, cfg, a.docs, rep)
    print(f"[{rep['date']}] daily cycle: equity ${rep.get('equity', 0):,.2f}")
    for x in rep["actions"]:
        print("  -", x)
    for w in rep["warnings"]:
        print("  ! warning:", w)
    for f in rep.get("fills", []):
        print(f"  * {f['side'].upper()} {f['qty']:.6g} {f['symbol']} @ {f['price']:.2f}  ({f['reason']})")
    if not any("Already ran" in x for x in rep["actions"]):
        url = cfg["account"].get("dashboard_url")
        for title, msg, prio, tags in notify.daily_messages(state, rep, cfg):
            notify.send(title, msg, prio, tags, click=url)
    return state, rep


def _live(cfg, state, data, a, now):
    live = snapshot(cfg, state, data, now, load_live(a.live))
    url = cfg["account"].get("dashboard_url")
    for title, msg, prio, tags in alerts_for(live, cfg, now):
        notify.send(title, msg, prio, tags, click=url)
    save_live(live, a.live, a.docs)
    print(f"[{live['time']}] live: equity ${live['equity']:,.2f} "
          f"({live['change_vs_last_daily']:+.2%} vs last daily run)")
    for s, p in live["prices"].items():
        print(f"  {s:4s} ${p:,.2f}  daily bar {live['bars_end'].get(s)}")
    for w in live["warnings"]:
        print("  ! warning:", w)
    for line in data.log:
        print("  data:", line)
    return live


def main(argv=None):
    p = argparse.ArgumentParser(prog="tradebot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["refresh", "run", "status", "dashboard", "resume", "reset", "test-alert"])
    p.add_argument("--config", default="config.toml")
    p.add_argument("--state", default="state/state.json")
    p.add_argument("--live", default="state/live.json")
    p.add_argument("--docs", default="docs")
    p.add_argument("--force", action="store_true", help="run again even if today already ran")
    p.add_argument("--yes", action="store_true")
    a = p.parse_args(argv)
    cfg = load_config(a.config)
    state = load_state(a.state)

    if a.command == "refresh":
        now = dt.datetime.now(dt.timezone.utc)
        data = LiveData(now)
        if daily_due(state, now):
            state, rep = _daily(cfg, state, data, a)
        elif state is not None and not state.get("history"):
            # first refresh after an upgrade: fill in price history so the charts work right away
            hist = state.setdefault("history", {})
            for s in cfg["crypto"]["symbols"]:
                try:
                    hist[s] = [list(b) for b in data.crypto(s)[0][-400:]]
                except Exception:
                    pass
            n = cfg["nasdaq"]
            for s in [n["signal_symbol"], n["risk_on"], n["risk_off"]]:
                try:
                    hist[s] = [list(b) for b in data.stock(s)[0][-400:]]
                except Exception:
                    pass
            save_state(state, a.state)
            build(state, cfg, a.docs)
        copy_assets(a.docs)
        _live(cfg, state, data, a, now)
        return 0  # refresh never fails the workflow: problems are reported as alerts instead
    if a.command == "run":
        now = dt.datetime.now(dt.timezone.utc)
        data = LiveData(now)
        state, rep = _daily(cfg, state, data, a, force=a.force)
        _live(cfg, state, data, a, now)
        return 2 if rep.get("data_failed") else 0
    if a.command == "test-alert":
        ok = notify.send("Trend Bot test", "Alerts are working. You'll get trades, signal changes, "
                         "problems and a daily summary here.", "default", "white_check_mark",
                         click=cfg["account"].get("dashboard_url"))
        print("sent" if ok else "not sent: set the NTFY_TOPIC environment variable / secret")
        return 0 if ok else 1
    if state is None:
        print("No state yet. Run:  python -m tradebot run")
        return 1
    if a.command == "status":
        e = state["equity"][-1] if state["equity"] else {}
        start = state.get("starting_cash", cfg["account"]["starting_cash"])
        print(f"Started {state['start_date']}, last run {state['last_run']}, {state['runs']} runs")
        print(f"Equity ${e.get('total', 0):,.2f}  ({e.get('total', start) / start - 1:+.2%})"
              f"  crypto ${e.get('crypto', 0):,.2f}  nasdaq ${e.get('nasdaq', 0):,.2f}")
        for pv in state.get("positions_view", []):
            print(f"  {pv['symbol']:4s} {pv['qty']:.6g} = ${pv['value']:,.2f}")
        if state["pending_stock"]:
            print("  pending:", state["pending_stock"])
        if state["halted"]:
            print("  HALTED:", state["halt_reason"])
    elif a.command == "dashboard":
        print("Wrote", build(state, cfg, a.docs))
    elif a.command == "resume":
        state["halted"] = False
        state["halt_reason"] = None
        state["peak_equity"] = state["equity"][-1]["total"] if state["equity"] else state["peak_equity"]
        state["events"].append({"date": dt.date.today().isoformat(), "level": "info", "msg": "Halt cleared by user"})
        save_state(state, a.state)
        print("Halt cleared. The bot will trade again on its next run.")
    elif a.command == "reset":
        if not a.yes:
            print("This wipes the paper account history. Re-run with --yes to confirm.")
            return 1
        import os
        os.remove(a.state)
        print("State removed. The next run starts a fresh paper account.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
