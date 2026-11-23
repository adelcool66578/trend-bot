"""One daily cycle of the bot. Call run_once() once per day (after the US close)."""
import copy
import datetime as dt
import json
import os
from pathlib import Path

from .broker import CRYPTO, AlpacaBroker, PaperBroker, qty_for_budget
from .data import DataError
from .strategy import crypto_targets, nasdaq_signal

MAX_EVENTS, MAX_TRADES = 400, 5000


def new_state(cfg, today):
    cash = float(cfg["account"]["starting_cash"])
    return {
        "version": 1, "start_date": today, "last_run": None, "runs": 0,
        "paper": {"cash": cash, "positions": {}},
        "sleeve_cash": {"crypto": cash * cfg["allocation"]["crypto"],
                        "nasdaq": cash * cfg["allocation"]["nasdaq"]},
        "peak_equity": cash, "halted": False, "halt_reason": None,
        "pending_stock": None, "last_stock_bar": None, "last_crypto_bar": None,
        "last_crypto_week": None, "last_month_rebalance": None,
        "bench_start": {}, "prices": {}, "equity": [], "trades": [], "events": [],
        "signals": {}, "data_log": [], "broker": cfg["account"]["broker"],
    }


def load_state(path):
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text())
    return None


def save_state(state, path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, default=float))
    os.replace(tmp, p)  # atomic: a crash never leaves a half-written state


def make_broker(cfg, state):
    if cfg["account"]["broker"] == "alpaca":
        return AlpacaBroker(cfg)
    return PaperBroker(cfg, state["paper"])


def _event(state, today, level, msg):
    state["events"].append({"date": today, "level": level, "msg": msg})
    state["events"] = state["events"][-MAX_EVENTS:]


def _week_id(d):
    y, w, _ = dt.date.fromisoformat(d).isocalendar()
    return f"{y}-W{w:02d}"


def _sleeve_of(sym, cfg):
    return "crypto" if sym in cfg["crypto"]["symbols"] else "nasdaq"


def _values(positions, prices):
    return {s: q * prices[s] for s, q in positions.items() if s in prices}


def run_once(cfg, state, data, broker=None, today=None, force=False):
    """Run one cycle. Returns (state, report dict)."""
    today = today or data.today
    report = {"date": today, "actions": [], "warnings": []}
    if state is None:
        state = new_state(cfg, today)
        _event(state, today, "info", f"Bot started with ${cfg['account']['starting_cash']:,.0f} paper money")
    if state["last_run"] == today and not force:
        report["actions"].append("Already ran today, nothing to do")
        return state, report
    broker = broker or make_broker(cfg, state)
    if state["runs"] == 0 and broker.name == "alpaca":
        # use the real paper-account balance as the starting point
        cash0 = broker.cash()
        state["sleeve_cash"] = {"crypto": cash0 * cfg["allocation"]["crypto"],
                                "nasdaq": cash0 * cfg["allocation"]["nasdaq"]}
        state["peak_equity"] = cash0
        state["starting_cash"] = cash0
    c, n = cfg["crypto"], cfg["nasdaq"]

    # ---------- 1) data ----------
    closes, prices, ok_c, ok_s = {}, dict(state["prices"]), True, True
    last_bar, crypto_bars = {}, {}
    for s in c["symbols"]:
        try:
            bars, cur = data.crypto(s)
            crypto_bars[s] = bars
            closes[s] = [b[1] for b in bars]; last_bar[s] = bars[-1][0]; prices[s] = cur
            age = (dt.date.fromisoformat(today) - dt.date.fromisoformat(bars[-1][0])).days
            if age > c["max_data_age_days"]:
                ok_c = False; report["warnings"].append(f"{s} data is {age} days old")
        except DataError as e:
            ok_c = False; report["warnings"].append(f"{s}: {e}")
    stock_bars = {}
    for s in [n["signal_symbol"], n["risk_on"], n["risk_off"]]:
        try:
            bars, cur = data.stock(s)
            stock_bars[s] = bars; prices[s] = cur
            age = (dt.date.fromisoformat(today) - dt.date.fromisoformat(bars[-1][0])).days
            if age > n["max_data_age_days"]:
                ok_s = False; report["warnings"].append(f"{s} data is {age} days old")
        except DataError as e:
            ok_s = False; report["warnings"].append(f"{s}: {e}")
    state["data_log"] = list(getattr(data, "log", []))[-20:]
    # keep about a year of daily closes for the dashboard charts
    hist = state.setdefault("history", {})
    for s, bars in crypto_bars.items():
        hist[s] = [list(b) for b in bars[-400:]]
    for s, bars in stock_bars.items():
        hist[s] = [list(b) for b in bars[-400:]]
    for w in report["warnings"]:
        _event(state, today, "warn", w)
    q_bars = stock_bars.get(n["signal_symbol"], [])
    stock_bar = q_bars[-1][0] if q_bars else None
    if ok_s and stock_bars:
        # all three stock series must end on the same day, otherwise prices are inconsistent
        ends = {s: b[-1][0] for s, b in stock_bars.items()}
        if len(set(ends.values())) > 1:
            ok_s = False
            msg = f"Stock data out of sync {ends}; skipping stock trades today"
            report["warnings"].append(msg); _event(state, today, "warn", msg)
    new_stock_bar = ok_s and stock_bar is not None and stock_bar != state["last_stock_bar"]

    # benchmarks start on the first day both prices are known
    for b in [n["signal_symbol"], "BTC"]:
        if b not in state["bench_start"] and b in prices:
            state["bench_start"][b] = prices[b]

    # ---------- 2) mark to market ----------
    def snapshot():
        pos = broker.positions()
        cash = broker.cash()
        vals = _values(pos, prices)
        cv = sum(v for s, v in vals.items() if _sleeve_of(s, cfg) == "crypto")
        nv = sum(v for s, v in vals.items() if _sleeve_of(s, cfg) == "nasdaq")
        return pos, cash, vals, cv, nv, cash + cv + nv

    pos, cash, vals, cv, nv, equity = snapshot()
    state["peak_equity"] = max(state["peak_equity"], equity)
    dd = equity / state["peak_equity"] - 1
    if not state["halted"] and dd <= -cfg["safety"]["max_drawdown_halt"]:
        state["halted"] = True
        state["halt_reason"] = f"Account fell {dd:.0%} from its peak; trading stopped for review"
        _event(state, today, "error", state["halt_reason"])

    all_fills = []

    def do(orders):
        eq = equity
        safe = []
        for o in orders:
            if o["qty"] * prices[o["symbol"]] > cfg["safety"]["max_order_fraction"] * eq:
                _event(state, today, "error", f"Blocked oversized order {o}")
                continue
            if o["qty"] * prices[o["symbol"]] < 1.0:  # ignore dust
                continue
            safe.append(o)
        fills = broker.execute(safe, prices, today)
        for f in fills:
            state["sleeve_cash"][f["sleeve"]] += f["cash_flow"]
            all_fills.append(f)
        return fills

    def crypto_orders(sleeve_value, weights, band, reason):
        orders = []
        for s in c["symbols"]:
            cur_val = pos.get(s, 0.0) * prices[s]
            tgt_val = sleeve_value * weights.get(s, 0.0)
            diff = tgt_val - cur_val
            exiting = tgt_val <= 0 and cur_val > 0
            if not exiting and abs(diff) < band * sleeve_value:
                continue
            if diff < 0:
                orders.append({"symbol": s, "side": "sell",
                               "qty": pos.get(s, 0.0) if exiting else -diff / prices[s],
                               "sleeve": "crypto", "reason": reason})
            elif diff > 0:
                orders.append({"symbol": s, "side": "buy", "qty": qty_for_budget(cfg, s, prices[s], diff),
                               "sleeve": "crypto", "reason": reason})
        return orders

    cw, cdet = (crypto_targets(closes, cfg) if ok_c else ({}, {}))
    sig_hold, sdet = (nasdaq_signal([b[1] for b in q_bars], cfg) if q_bars else (None, {}))
    stock_syms = [n["risk_on"], n["risk_off"]]
    holding = next((s for s in stock_syms if pos.get(s, 0) > 0), None)

    if not state["halted"]:
        month = today[:7]
        # ---------- 3) monthly reset to 50/50 (needs a fresh US trading day) ----------
        due_month = state["last_month_rebalance"] != month
        if due_month and new_stock_bar and ok_c:
            target_stock = (state["pending_stock"] or {}).get("to") or holding or sig_hold
            ac, an = cfg["allocation"]["crypto"], cfg["allocation"]["nasdaq"]
            first = state["last_month_rebalance"] is None
            off = max(abs(cv + state["sleeve_cash"]["crypto"] - ac * equity),
                      abs(nv + state["sleeve_cash"]["nasdaq"] - an * equity)) / equity
            if first or off > cfg["allocation"]["rebalance_band"] or state["pending_stock"]:
                orders = crypto_orders(ac * equity, cw, 0.0, "monthly rebalance" if not first else "initial allocation")
                tgt_val = an * equity
                for s in stock_syms:
                    have = pos.get(s, 0.0) * prices[s]
                    want = tgt_val if s == target_stock else 0.0
                    if want == 0 and have > 0:
                        orders.append({"symbol": s, "side": "sell", "qty": pos[s], "sleeve": "nasdaq",
                                       "reason": "switch" if s == holding else "rebalance"})
                    elif want > have * 1.0 + 1:
                        orders.append({"symbol": s, "side": "buy",
                                       "qty": qty_for_budget(cfg, s, prices[s], want - have),
                                       "sleeve": "nasdaq", "reason": "initial allocation" if first else "monthly rebalance"})
                    elif have > want + 1:
                        orders.append({"symbol": s, "side": "sell", "qty": (have - want) / prices[s],
                                       "sleeve": "nasdaq", "reason": "monthly rebalance"})
                do(orders)
                # reset the sleeve cash buckets so both sleeves sit exactly at target
                pos, cash, vals, cv, nv, equity = snapshot()
                state["sleeve_cash"]["crypto"] = ac * equity - cv
                state["sleeve_cash"]["nasdaq"] = cash - state["sleeve_cash"]["crypto"]
                report["actions"].append("Initial allocation" if first else "Monthly 50/50 rebalance")
                _event(state, today, "info", "Initial allocation done" if first else "Monthly rebalance to 50/50")
            state["last_month_rebalance"] = month
            state["last_crypto_week"] = _week_id(today)
            state["pending_stock"] = None
        else:
            # ---------- 4) Nasdaq sleeve: fill yesterday's decision, then decide ----------
            if new_stock_bar and state["pending_stock"]:
                to = state["pending_stock"]["to"]
                orders = []
                for s in stock_syms:
                    if s != to and pos.get(s, 0) > 0:
                        orders.append({"symbol": s, "side": "sell", "qty": pos[s], "sleeve": "nasdaq",
                                       "reason": f"trend switch to {to}"})
                do(orders)
                pos, cash, vals, cv, nv, equity = snapshot()
                budget = state["sleeve_cash"]["nasdaq"]
                have = pos.get(to, 0.0) * prices[to]
                if budget > 1:
                    do([{"symbol": to, "side": "buy", "qty": qty_for_budget(cfg, to, prices[to], min(budget, cash)),
                         "sleeve": "nasdaq", "reason": f"trend switch to {to}"}])
                report["actions"].append(f"Switched Nasdaq sleeve to {to}")
                _event(state, today, "trade", f"Nasdaq sleeve switched to {to}")
                state["pending_stock"] = None
                pos, cash, vals, cv, nv, equity = snapshot()
            # ---------- 5) crypto sleeve: weekly rebalance ----------
            wk = _week_id(today)
            wd = dt.date.fromisoformat(today).weekday()
            if ok_c and state["last_crypto_week"] != wk and wd >= c["rebalance_weekday"]:
                sleeve_val = cv + state["sleeve_cash"]["crypto"]
                orders = crypto_orders(sleeve_val, cw, c["band"], "weekly crypto rebalance")
                if orders:
                    do(orders)
                    report["actions"].append(f"Crypto rebalance: {len(orders)} order(s)")
                    _event(state, today, "trade", f"Weekly crypto rebalance ({len(orders)} orders)")
                else:
                    report["actions"].append("Crypto checked: no change needed")
                state["last_crypto_week"] = wk
                pos, cash, vals, cv, nv, equity = snapshot()
        # ---------- 6) Nasdaq signal for tomorrow ----------
        holding = next((s for s in stock_syms if pos.get(s, 0) > 0), None)
        if new_stock_bar and sig_hold and holding and sig_hold != holding and not state["pending_stock"]:
            state["pending_stock"] = {"to": sig_hold, "decided_on": stock_bar, "created": today}
            report["actions"].append(f"Signal: switch Nasdaq sleeve to {sig_hold} next trading day")
            _event(state, today, "signal", f"QQQ crossed its {n['sma_days']}-day average: "
                                            f"switch to {sig_hold} on the next trading day")
        elif (new_stock_bar and sig_hold and holding is None and not state["pending_stock"]
              and state["sleeve_cash"]["nasdaq"] > 1):
            # Nasdaq sleeve is all cash (e.g. not enough history at start): invest next trading day
            state["pending_stock"] = {"to": sig_hold, "decided_on": stock_bar, "created": today}
            report["actions"].append(f"Signal: invest Nasdaq sleeve in {sig_hold} next trading day")
            _event(state, today, "signal", f"Nasdaq sleeve is in cash: buy {sig_hold} on the next trading day")
    else:
        report["actions"].append("Trading halted: " + (state["halt_reason"] or ""))

    if new_stock_bar:
        state["last_stock_bar"] = stock_bar
    if ok_c and last_bar:
        state["last_crypto_bar"] = max(last_bar.values())

    # ---------- 7) record ----------
    pos, cash, vals, cv, nv, equity = snapshot()
    state["peak_equity"] = max(state["peak_equity"], equity)
    start_cash = float(state.get("starting_cash") or cfg["account"]["starting_cash"])
    state["starting_cash"] = start_cash
    bench = {}
    for b, key in [(n["signal_symbol"], "bench_qqq"), ("BTC", "bench_btc")]:
        if b in state["bench_start"] and b in prices:
            bench[key] = start_cash * prices[b] / state["bench_start"][b]
    point = {"date": today, "total": equity, "crypto": cv + state["sleeve_cash"]["crypto"],
             "nasdaq": nv + state["sleeve_cash"]["nasdaq"], "cash": cash, **bench}
    if state["equity"] and state["equity"][-1]["date"] == today:
        state["equity"][-1] = point
    else:
        state["equity"].append(point)
    for f in all_fills:
        state["trades"].append(f)
    state["trades"] = state["trades"][-MAX_TRADES:]
    state["prices"] = {k: v for k, v in prices.items()}
    state["positions_view"] = [
        {"symbol": s, "qty": q, "price": prices.get(s), "value": q * prices.get(s, 0),
         "sleeve": _sleeve_of(s, cfg), "avg_cost": _avg_cost(state["trades"], s, q)}
        for s, q in pos.items()]
    state["signals"] = {
        "crypto": cdet, "crypto_ok": ok_c, "crypto_last_bar": state["last_crypto_bar"],
        "nasdaq": sdet, "nasdaq_ok": ok_s, "nasdaq_last_bar": stock_bar,
        "pending_stock": state["pending_stock"],
        "next_crypto_rebalance": _next_weekday(today, c["rebalance_weekday"], state["last_crypto_week"]),
        "next_month_rebalance": _next_month(today),
    }
    report["data_failed"] = (not ok_c) and (not ok_s)
    if report["data_failed"]:
        # leave last_run untouched so a later retry the same day can still run
        _event(state, today, "error", "No usable market data; nothing traded. Will retry on the next run.")
    else:
        state["last_run"] = today
        state["runs"] += 1
    if not report["actions"]:
        report["actions"].append("No trades needed today")
    _event(state, today, "run", "; ".join(report["actions"]))
    report["equity"] = equity
    report["fills"] = all_fills
    return state, report


def _avg_cost(trades, sym, qty):
    """Average cost of the current position from the trade log (FIFO-free running average)."""
    q, cost = 0.0, 0.0
    for t in trades:
        if t["symbol"] != sym:
            continue
        if t["side"] == "buy":
            q += t["qty"]; cost += t["qty"] * t["price"] + t["fee"]
        else:
            if q > 0:
                cost *= max(0.0, (q - t["qty"]) / q)
            q -= t["qty"]
            if q <= 1e-12:
                q, cost = 0.0, 0.0
    return cost / q if q > 1e-12 else None


def _next_weekday(today, wd, last_week):
    d = dt.date.fromisoformat(today)
    if last_week != _week_id(today) and d.weekday() >= wd:
        return today
    days = (wd - d.weekday()) % 7 or 7
    return (d + dt.timedelta(days=days)).isoformat()


def _next_month(today):
    d = dt.date.fromisoformat(today)
    y, m = (d.year + (d.month // 12), d.month % 12 + 1)
    return dt.date(y, m, 1).isoformat()
