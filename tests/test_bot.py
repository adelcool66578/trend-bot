"""Unit + integration tests. Run:  python -m unittest discover -s tests -v"""
import copy
import datetime as dt
import json
import math
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tradebot.bot import load_state, run_once, save_state
from tradebot.broker import AlpacaBroker, PaperBroker, fee_and_price, qty_for_budget
from tradebot.config import load_config
from tradebot.dashboard import build
from tradebot.data import (DataError, ReplayData, parse_coinbase, parse_kraken,
                           parse_nasdaq, parse_yahoo)
from tradebot.strategy import ann_vol, crypto_targets, nasdaq_signal, trend_score

CFG = load_config(ROOT / "config.toml")


def days(start, n):
    d = dt.date.fromisoformat(start)
    return [(d + dt.timedelta(days=i)).isoformat() for i in range(n)]


def weekdays(start, n):
    out, d = [], dt.date.fromisoformat(start)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def synth_series(trend_crypto=0.002, trend_qqq=0.001, n=500, start="2024-01-01"):
    """Deterministic wiggly trending prices so every code path runs."""
    s = {}
    for sym, base, tr in [("BTC", 40000, trend_crypto), ("ETH", 2500, trend_crypto * 1.2)]:
        ds = days(start, n)
        s[sym] = [(d, base * math.exp(tr * i + 0.03 * math.sin(i / 3))) for i, d in enumerate(ds)]
    wd = weekdays(start, int(n * 5 / 7))
    for sym, base, tr in [("QQQ", 400, trend_qqq), ("QLD", 80, 2 * trend_qqq), ("SHY", 82, 0.0001)]:
        s[sym] = [(d, base * math.exp(tr * i + 0.01 * math.sin(i / 4))) for i, d in enumerate(wd)]
    return s


def run_days(series, start, end, cfg=CFG, state=None):
    data = ReplayData(series)
    d = dt.date.fromisoformat(start)
    reports = []
    while d <= dt.date.fromisoformat(end):
        data.set_date(d.isoformat())
        state, rep = run_once(cfg, state, data, today=d.isoformat())
        reports.append(rep)
        d += dt.timedelta(days=1)
    return state, reports


def equity_identity(state):
    """cash + positions must equal recorded equity; sleeve cash must sum to broker cash."""
    pos, cash, prices = state["paper"]["positions"], state["paper"]["cash"], state["prices"]
    tot = cash + sum(q * prices[s] for s, q in pos.items())
    sc = state["sleeve_cash"]["crypto"] + state["sleeve_cash"]["nasdaq"]
    return tot, state["equity"][-1]["total"], sc, cash


class TestStrategy(unittest.TestCase):
    def test_trend_score(self):
        closes = list(range(1, 200))
        self.assertEqual(trend_score(closes, [20, 60, 120]), 1.0)
        self.assertEqual(trend_score(closes[::-1], [20, 60, 120]), 0.0)
        self.assertIsNone(trend_score(closes[:50], [20, 60, 120]))
        mixed = [100] * 80 + [50] * 60 + [80] * 30   # up vs 20d ago? no: flat; vs 60d: up; vs 120d: down
        self.assertAlmostEqual(trend_score(mixed, [20, 60, 120]), 1 / 3)

    def test_vol(self):
        closes = [100 * math.exp(0.01 * ((-1) ** i)) for i in range(40)]
        v = ann_vol(closes, 30)
        self.assertGreater(v, 0.3); self.assertLess(v, 0.5)

    def test_crypto_targets_cap_and_zero(self):
        up = [100 * math.exp(0.001 * i + 0.0005 * ((-1) ** i)) for i in range(200)]  # very calm uptrend
        down = up[::-1]
        w, det = crypto_targets({"BTC": up, "ETH": down}, CFG)
        self.assertEqual(w["ETH"], 0.0)
        self.assertLessEqual(sum(w.values()), CFG["crypto"]["max_exposure"] + 1e-12)
        self.assertGreater(w["BTC"], 0.0)
        w2, _ = crypto_targets({"BTC": up, "ETH": up}, CFG)
        self.assertAlmostEqual(sum(w2.values()), 1.0)  # calm coins hit the 100% cap

    def test_nasdaq_signal(self):
        self.assertEqual(nasdaq_signal(list(range(1, 300)), CFG)[0], "QLD")
        self.assertEqual(nasdaq_signal(list(range(300, 1, -1)), CFG)[0], "SHY")
        self.assertIsNone(nasdaq_signal(list(range(50)), CFG)[0])


class TestBroker(unittest.TestCase):
    def test_fees(self):
        fp, fee = fee_and_price(CFG, "BTC", "buy", 100.0, 2)
        self.assertAlmostEqual(fp, 100.1); self.assertAlmostEqual(fee, 2 * 100.1 * 0.0025)
        fp, fee = fee_and_price(CFG, "QLD", "sell", 100.0, 1)
        self.assertAlmostEqual(fp, 99.95); self.assertAlmostEqual(fee, 0.35)

    def test_budget_fits(self):
        for sym in ("BTC", "QLD", "SHY"):
            q = qty_for_budget(CFG, sym, 123.45, 1000)
            fp, fee = fee_and_price(CFG, sym, "buy", 123.45, q)
            self.assertLessEqual(q * fp + fee, 1000 + 1e-6)
            self.assertGreater(q * fp + fee, 990)

    def test_never_negative_cash_and_sells_first(self):
        b = PaperBroker(CFG, {"cash": 100.0, "positions": {"ETH": 1.0}})
        fills = b.execute([{"symbol": "BTC", "side": "buy", "qty": 1.0, "sleeve": "crypto", "reason": "t"},
                           {"symbol": "ETH", "side": "sell", "qty": 1.0, "sleeve": "crypto", "reason": "t"}],
                          {"BTC": 1000.0, "ETH": 500.0}, "2026-01-01")
        self.assertEqual(fills[0]["side"], "sell")
        self.assertGreaterEqual(b.cash(), -1e-9)
        self.assertLess(b.positions()["BTC"], 1.0)  # shrunk to what cash allows

    def test_cannot_sell_more_than_held(self):
        b = PaperBroker(CFG, {"cash": 0.0, "positions": {"SHY": 2.0}})
        f = b.execute([{"symbol": "SHY", "side": "sell", "qty": 5.0, "sleeve": "nasdaq", "reason": "t"}],
                      {"SHY": 80.0}, "d")
        self.assertAlmostEqual(f[0]["qty"], 2.0)
        self.assertEqual(b.positions(), {})


class TestParsers(unittest.TestCase):
    def test_kraken_drops_forming_candle(self):
        t0 = int(dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc).timestamp())
        rows = [[t0 + 86400 * i, "1", "1", "1", str(100 + i), "1", "1", 1] for i in range(4)]
        bars, cur = parse_kraken({"error": [], "result": {"XXBTZUSD": rows, "last": 1}}, "2026-11-23")
        self.assertEqual(bars[-1], ("2026-11-22", 102.0))
        self.assertEqual(cur, 103.0)
        with self.assertRaises(DataError):
            parse_kraken({"error": ["EGeneral:Too many requests"]}, "2026-11-23")

    def test_coinbase_sorted(self):
        t0 = int(dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc).timestamp())
        rows = [[t0 + 86400 * i, 1, 1, 1, 100 + i, 1] for i in range(4)][::-1]  # newest first
        bars, cur = parse_coinbase(rows, "2026-11-23")
        self.assertEqual([b[0] for b in bars], ["2026-11-20", "2026-11-21", "2026-11-22"])
        self.assertEqual(cur, 103)

    def _yahoo(self, closes, start_day, reg_start, reg_end, price):
        ts = [int(dt.datetime(2026, 11, start_day + i, 14, 30, tzinfo=dt.timezone.utc).timestamp()) for i in range(len(closes))]
        return {"chart": {"result": [{"meta": {"regularMarketPrice": price, "exchangeTimezoneName": "America/New_York",
                                               "currentTradingPeriod": {"regular": {"start": reg_start, "end": reg_end}}},
                                      "timestamp": ts, "indicators": {"quote": [{"close": closes}]}}], "error": None}}

    def test_yahoo_after_close_keeps_today(self):
        rs = int(dt.datetime(2026, 11, 20, 14, 30, tzinfo=dt.timezone.utc).timestamp())
        p = self._yahoo([1.0, 2.0, None, 3.0], 17, rs, rs + 6.5 * 3600, 3.0)
        bars, cur = parse_yahoo(p, now_ts=rs + 8 * 3600)
        self.assertEqual(bars[-1], ("2026-11-20", 3.0)); self.assertEqual(len(bars), 3)  # None skipped

    def test_yahoo_during_session_drops_today(self):
        rs = int(dt.datetime(2026, 11, 20, 14, 30, tzinfo=dt.timezone.utc).timestamp())
        p = self._yahoo([1.0, 2.0, 2.5, 3.0], 17, rs, rs + 6.5 * 3600, 3.1)
        bars, cur = parse_yahoo(p, now_ts=rs + 3600)
        self.assertEqual(bars[-1][0], "2026-11-19"); self.assertEqual(cur, 3.1)

    def test_nasdaq(self):
        p = {"data": {"tradesTable": {"rows": [{"date": "11/21/2026", "close": "$1,512.30"},
                                               {"date": "11/20/2026", "close": "$1,500.00"}]}}}
        bars, cur = parse_nasdaq(p)
        self.assertEqual(bars, [("2026-11-20", 1500.0), ("2026-11-21", 1512.3)]); self.assertEqual(cur, 1512.3)


class TestBotCycle(unittest.TestCase):
    def test_full_run_accounting_and_schedule(self):
        s = synth_series()
        state, reps = run_days(s, "2024-08-01", "2025-03-31")
        tot, rec, sc, cash = equity_identity(state)
        self.assertAlmostEqual(tot, rec, places=6)
        self.assertAlmostEqual(sc, cash, places=6)
        self.assertGreater(len(state["trades"]), 5)
        self.assertFalse(state["halted"])
        # crypto trades only happen on Mondays (weekly) or on monthly-rebalance days
        for t in state["trades"]:
            if t["sleeve"] == "crypto":
                d = dt.date.fromisoformat(t["time"])
                self.assertTrue(d.weekday() == 0 or "rebalance" in t["reason"] or "initial" in t["reason"], t)
        # one equity point per day, no duplicates
        dates = [p["date"] for p in state["equity"]]
        self.assertEqual(len(dates), len(set(dates)))
        # the monthly check ran through the last month, and the Nasdaq sleeve got invested
        self.assertEqual(state["last_month_rebalance"], "2025-03")
        months = {t["time"][:7] for t in state["trades"] if t["reason"] in ("monthly rebalance", "initial allocation")}
        self.assertGreaterEqual(len(months), 2)
        first_stock = min(t["time"] for t in state["trades"] if t["sleeve"] == "nasdaq" and t["side"] == "buy")
        enough = s["QQQ"][CFG["nasdaq"]["sma_days"] - 1][0]  # first day with a full 200-day average
        self.assertGreater(first_stock, enough)               # never before the signal exists
        self.assertLessEqual((dt.date.fromisoformat(first_stock) - dt.date.fromisoformat(enough)).days, 4)
        # sleeves never drift absurdly far from 50/50 at a month start
        for p in state["equity"]:
            if p["date"][8:] == "05":
                self.assertLess(abs(p["crypto"] - p["nasdaq"]) / p["total"], 0.25, p)

    def test_idempotent_same_day(self):
        s = synth_series()
        state, _ = run_days(s, "2024-08-01", "2024-09-02")
        n = len(state["trades"]); snapshot = json.dumps(state, sort_keys=True, default=float)
        data = ReplayData(s); data.set_date("2024-09-02")
        state2, rep = run_once(CFG, copy.deepcopy(state), data, today="2024-09-02")
        self.assertIn("Already ran today", rep["actions"][0])
        self.assertEqual(len(state2["trades"]), n)
        self.assertEqual(json.dumps(state2, sort_keys=True, default=float), snapshot)

    def test_switch_executes_next_trading_day(self):
        s = synth_series(trend_qqq=0.001)
        # crash QQQ late so the 200-day signal flips to SHY
        q = s["QQQ"]; cut = len(q) - 60
        s["QQQ"] = q[:cut] + [(d, v * 0.6) for d, v in q[cut:]]
        state, _ = run_days(s, "2024-08-01", s["QQQ"][-1][0])
        sig = [e for e in state["events"] if e["level"] == "signal"]
        self.assertTrue(sig, "expected a switch signal")
        first_sig_day = sig[0]["date"]
        sw = [t for t in state["trades"] if t["reason"] == "trend switch to SHY" and t["side"] == "buy"]
        self.assertTrue(sw, "expected the switch to be filled")
        self.assertGreater(sw[0]["time"], first_sig_day)  # never the same day as the signal
        self.assertTrue(any(p["symbol"] == "SHY" for p in state["positions_view"]))

    def test_stale_data_blocks_trading(self):
        s = synth_series()
        state, _ = run_days(s, "2024-08-01", "2024-09-01")
        n = len(state["trades"])
        cut = {k: [x for x in v if x[0] <= "2024-09-01"] for k, v in s.items()}  # no new data after Sep 1
        state, reps = run_days(cut, "2024-09-09", "2024-09-10", state=state)
        self.assertEqual(len(state["trades"]), n)
        self.assertTrue(any("old" in w for r in reps for w in r["warnings"]))

    def test_halt_on_drawdown(self):
        s = synth_series()
        state, _ = run_days(s, "2024-08-01", "2024-10-01")
        n = len(state["trades"])
        state["peak_equity"] = state["equity"][-1]["total"] * 3  # pretend we are 67% below the peak
        state, reps = run_days(s, "2024-10-02", "2024-11-10", state=state)
        self.assertTrue(state["halted"])
        self.assertEqual(len(state["trades"]), n)              # nothing traded while halted
        self.assertTrue(any(e["level"] == "error" for e in state["events"]))
        # resume clears it and trading continues
        from tradebot.__main__ import main
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.json"; save_state(state, p)
            main(["resume", "--config", str(ROOT / "config.toml"), "--state", str(p)])
            st2 = load_state(p)
            self.assertFalse(st2["halted"])
        st2, _ = run_days(s, "2024-11-11", "2024-12-10", state=st2)
        self.assertGreater(len(st2["trades"]), n)

    def test_total_data_failure_allows_retry(self):
        class Dead:
            today = "2024-09-03"; log = []
            def crypto(self, s): raise DataError("down")
            def stock(self, s): raise DataError("down")
        s = synth_series()
        state, _ = run_days(s, "2024-08-01", "2024-09-02")
        n = len(state["trades"])
        state, rep = run_once(CFG, state, Dead(), today="2024-09-03")
        self.assertTrue(rep["data_failed"])
        self.assertEqual(state["last_run"], "2024-09-02")   # not marked as done
        self.assertEqual(len(state["trades"]), n)
        data = ReplayData(s); data.set_date("2024-09-03")   # retry later that day works
        state, rep = run_once(CFG, state, data, today="2024-09-03")
        self.assertEqual(state["last_run"], "2024-09-03")

    def test_out_of_sync_stock_data(self):
        s = synth_series()
        state, _ = run_days(s, "2024-08-01", "2024-09-02")
        s["QLD"] = [x for x in s["QLD"] if x[0] <= "2024-09-02"]  # one day behind QQQ, not stale
        state, reps = run_days(s, "2024-09-03", "2024-09-03", state=state)
        self.assertTrue(any("out of sync" in w for w in reps[0]["warnings"]))

    def test_state_roundtrip_and_dashboard(self):
        s = synth_series()
        state, rep = run_days(s, "2024-08-01", "2024-10-01")
        with tempfile.TemporaryDirectory() as td:
            save_state(state, Path(td) / "state" / "s.json")
            self.assertEqual(load_state(Path(td) / "state" / "s.json")["runs"], state["runs"])
            out = build(state, CFG, Path(td) / "docs", rep[-1])
            js = (Path(td) / "docs" / "data.js").read_text()
            self.assertTrue(js.startswith("window.BOT_DATA = "))
            json.loads(js[len("window.BOT_DATA = "):-2])
            self.assertTrue(out.exists())


class TestLiveRefresh(unittest.TestCase):
    def test_daily_due_rules(self):
        from tradebot.live import daily_due
        U = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc)
        self.assertTrue(daily_due(None, U("2026-11-24T05:00")))                       # never ran
        st = {"last_run": "2026-11-23"}
        self.assertFalse(daily_due(st, U("2026-11-24T05:00")))   # before the US close: wait
        self.assertTrue(daily_due(st, U("2026-11-24T21:40")))    # after the close: run
        self.assertFalse(daily_due({"last_run": "2026-11-24"}, U("2026-11-24T23:50")))  # already done
        self.assertTrue(daily_due({"last_run": "2026-11-22"}, U("2026-11-24T05:00")))   # missed a whole day

    def test_snapshot_values_account_and_never_trades(self):
        from tradebot.live import snapshot, alerts_for, save_live
        s = synth_series()
        state, _ = run_days(s, "2024-08-01", "2024-12-01")
        before = json.dumps(state, sort_keys=True, default=float)
        data = ReplayData(s); data.set_date("2024-12-02")
        now = dt.datetime(2024, 12, 2, 3, 0, tzinfo=dt.timezone.utc)
        live = snapshot(CFG, state, data, now, None)
        self.assertEqual(json.dumps(state, sort_keys=True, default=float), before)  # state untouched
        pos, prices = state["paper"]["positions"], live["prices"]
        expect = state["paper"]["cash"] + sum(q * prices.get(k, state["prices"][k]) for k, q in pos.items())
        self.assertAlmostEqual(live["equity"], expect, places=6)
        self.assertEqual(len(live["intraday"]), 1)
        live2 = snapshot(CFG, state, data, now + dt.timedelta(minutes=10), live)
        self.assertEqual(len(live2["intraday"]), 2)
        self.assertIn("would_hold", live2["nasdaq_live"])
        # big-move alert fires once a day only
        live2["change_vs_last_daily"] = -0.06
        self.assertEqual(len(alerts_for(live2, CFG, now)), 1)
        self.assertEqual(len(alerts_for(live2, CFG, now)), 0)
        with tempfile.TemporaryDirectory() as td:
            save_live(live2, Path(td) / "live.json", Path(td) / "docs")
            js = (Path(td) / "docs" / "live.js").read_text()
            json.loads(js[len("window.BOT_LIVE = "):-2])

    def test_daily_alert_messages(self):
        from tradebot import notify
        s = synth_series()
        state, reps = run_days(s, "2024-08-01", "2024-08-05")
        msgs = notify.daily_messages(state, reps[-1], CFG)
        self.assertTrue(any("summary" in m[0] for m in msgs))
        first = [r for r in reps if r["fills"]][0]
        self.assertTrue(any("trades" in m[0] for m in notify.daily_messages(state, first, CFG)))
        self.assertFalse(notify.send("x", "y", topic=""))  # no topic: silently skipped

    def test_notify_posts_to_server(self):
        from tradebot import notify
        got = []
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                got.append((self.path, self.headers["Title"], self.rfile.read(int(self.headers["Content-Length"])).decode()))
                self.send_response(200); self.end_headers()
        srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            ok = notify.send("Trend Bot test", "hello", topic="abc", server=f"http://127.0.0.1:{srv.server_port}")
        finally:
            srv.shutdown()
        self.assertTrue(ok); self.assertEqual(got[0], ("/abc", "Trend Bot test", "hello"))


class FakeAlpaca(BaseHTTPRequestHandler):
    orders, positions, cash = {}, {}, 100000.0

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        if self.headers.get("APCA-API-KEY-ID") != "k":
            return self._send(401, {"message": "unauthorized"})
        if self.path == "/v2/account":
            return self._send(200, {"cash": str(FakeAlpaca.cash)})
        if self.path == "/v2/positions":
            return self._send(200, [{"symbol": s, "qty": str(q)} for s, q in FakeAlpaca.positions.items()])
        if self.path.startswith("/v2/orders/"):
            o = FakeAlpaca.orders[self.path.split("/")[-1]]
            o["status"], o["filled_avg_price"] = "filled", "101.5"
            return self._send(200, o)
        self._send(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body["side"] not in ("buy", "sell") or body["type"] != "market":
            return self._send(422, {"message": "bad order"})
        oid = f"o{len(FakeAlpaca.orders)}"
        o = {"id": oid, "status": "accepted", "filled_avg_price": None, **body}
        FakeAlpaca.orders[oid] = o
        q = float(body["qty"]) * (1 if body["side"] == "buy" else -1)
        FakeAlpaca.positions[body["symbol"]] = FakeAlpaca.positions.get(body["symbol"], 0) + q
        self._send(200, o)


class TestAlpaca(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), FakeAlpaca)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_orders_and_symbol_mapping(self):
        b = AlpacaBroker(CFG, base_url=self.url, key="k", secret="s", poll_seconds=3)
        self.assertEqual(b.cash(), 100000.0)
        fills = b.execute([{"symbol": "BTC", "side": "buy", "qty": 0.0123456789, "sleeve": "crypto", "reason": "t"},
                           {"symbol": "QLD", "side": "buy", "qty": 3.0, "sleeve": "nasdaq", "reason": "t"}],
                          {"BTC": 100.0, "QLD": 100.0}, "2026-11-23")
        self.assertEqual([f["status"] for f in fills], ["filled", "filled"])
        self.assertEqual(fills[0]["price"], 101.5)
        sent = list(FakeAlpaca.orders.values())
        self.assertEqual(sent[0]["symbol"], "BTC/USD"); self.assertEqual(sent[0]["time_in_force"], "gtc")
        self.assertEqual(sent[1]["time_in_force"], "day")
        self.assertIn("BTC", b.positions()); self.assertIn("QLD", b.positions())

    def test_refuses_live_endpoint_and_missing_keys(self):
        with self.assertRaises(RuntimeError):
            AlpacaBroker(CFG, base_url="https://api.alpaca.markets", key="k", secret="s")
        with self.assertRaises(RuntimeError):
            AlpacaBroker(CFG, base_url=self.url, key="", secret="")

    def test_bad_key_raises(self):
        b = AlpacaBroker(CFG, base_url=self.url, key="wrong", secret="s")
        with self.assertRaises(RuntimeError):
            b.cash()


if __name__ == "__main__":
    unittest.main()
