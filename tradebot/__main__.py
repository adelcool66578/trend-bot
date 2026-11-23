"""Command line:  python -m tradebot <command>

  run        fetch live prices, run today's cycle, update the dashboard
  status     print a short summary of the account
  dashboard  rebuild the dashboard from the saved state
  resume     clear a safety halt after you have reviewed it
  reset      start the paper account again from scratch (asks for --yes)
"""
import argparse
import datetime as dt
import sys

from .bot import load_state, run_once, save_state
from .config import load_config
from .dashboard import build
from .data import LiveData


def main(argv=None):
    p = argparse.ArgumentParser(prog="tradebot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["run", "status", "dashboard", "resume", "reset"])
    p.add_argument("--config", default="config.toml")
    p.add_argument("--state", default="state/state.json")
    p.add_argument("--docs", default="docs")
    p.add_argument("--force", action="store_true", help="run again even if today already ran")
    p.add_argument("--yes", action="store_true")
    a = p.parse_args(argv)
    cfg = load_config(a.config)
    state = load_state(a.state)

    if a.command == "run":
        data = LiveData()
        state, rep = run_once(cfg, state, data, force=a.force)
        save_state(state, a.state)
        build(state, cfg, a.docs, rep)
        print(f"[{rep['date']}] equity ${rep.get('equity', 0):,.2f}")
        for x in rep["actions"]:
            print("  -", x)
        for w in rep["warnings"]:
            print("  ! warning:", w)
        for f in rep.get("fills", []):
            print(f"  * {f['side'].upper()} {f['qty']:.6g} {f['symbol']} @ {f['price']:.2f}  ({f['reason']})")
        for line in data.log:
            print("  data:", line)
        return 2 if rep.get("data_failed") else 0
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
