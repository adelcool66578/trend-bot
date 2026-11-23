"""Brokers. PaperBroker simulates fills with fees and slippage; AlpacaBroker
talks to an Alpaca *paper* account over its REST API."""
import json
import os
import time
import urllib.error
import urllib.request

CRYPTO = {"BTC", "ETH", "SOL"}


def fee_and_price(cfg, sym, side, price, qty):
    """Return (fill_price, fee) for a market order under the configured cost model."""
    if sym in CRYPTO:
        c = cfg["crypto"]
        fp = price * (1 + c["slippage"]) if side == "buy" else price * (1 - c["slippage"])
        return fp, abs(qty) * fp * c["fee"]
    n = cfg["nasdaq"]
    fp = price * (1 + n["slippage"]) if side == "buy" else price * (1 - n["slippage"])
    notional = abs(qty) * fp
    fee = min(max(n["min_fee"], abs(qty) * n["fee_per_share"]), 0.01 * notional)
    return fp, fee


def qty_for_budget(cfg, sym, price, budget):
    """Largest buy quantity whose cost including fees fits in `budget`."""
    if budget <= 0:
        return 0.0
    if sym in CRYPTO:
        c = cfg["crypto"]
        return budget / (price * (1 + c["slippage"]) * (1 + c["fee"]))
    n = cfg["nasdaq"]
    fp = price * (1 + n["slippage"])
    q = budget / fp
    for _ in range(3):  # converge on the fee
        _, fee = fee_and_price(cfg, sym, "buy", price, q)
        q = max(0.0, (budget - fee) / fp)
    return q


class PaperBroker:
    name = "paper"

    def __init__(self, cfg, book):
        self.cfg = cfg
        self.book = book  # dict persisted in state: {"cash": float, "positions": {sym: qty}}

    def cash(self):
        return self.book["cash"]

    def positions(self):
        return {k: v for k, v in self.book["positions"].items() if abs(v) > 1e-12}

    def execute(self, orders, prices, when):
        """orders: list of dicts {symbol, side, qty, sleeve, reason}. Sells first."""
        fills = []
        for o in sorted(orders, key=lambda o: 0 if o["side"] == "sell" else 1):
            sym, side, qty = o["symbol"], o["side"], o["qty"]
            if qty <= 0:
                continue
            px = prices[sym]
            if side == "sell":
                qty = min(qty, self.book["positions"].get(sym, 0.0))
                if qty <= 0:
                    continue
            fp, fee = fee_and_price(self.cfg, sym, side, px, qty)
            if side == "buy":
                cost = qty * fp + fee
                if cost > self.book["cash"] + 1e-9:  # never go negative: shrink the buy
                    qty = qty_for_budget(self.cfg, sym, px, self.book["cash"])
                    if qty <= 1e-12:
                        continue
                    fp, fee = fee_and_price(self.cfg, sym, side, px, qty)
                    cost = qty * fp + fee
                self.book["cash"] -= cost
                self.book["positions"][sym] = self.book["positions"].get(sym, 0.0) + qty
                flow = -cost
            else:
                self.book["cash"] += qty * fp - fee
                left = self.book["positions"].get(sym, 0.0) - qty
                self.book["positions"][sym] = 0.0 if abs(left) < 1e-12 else left
                flow = qty * fp - fee
            fills.append({"time": when, "symbol": sym, "side": side, "qty": qty, "price": fp,
                          "notional": qty * fp, "fee": fee, "cash_flow": flow,
                          "sleeve": o["sleeve"], "reason": o["reason"], "status": "filled"})
        return fills


class AlpacaBroker:
    """Alpaca paper trading. Needs ALPACA_KEY_ID / ALPACA_SECRET_KEY env vars.
    Crypto symbols map to 'BTC/USD'; stock orders placed after the close queue for the next open."""
    name = "alpaca"

    def __init__(self, cfg, book=None, base_url=None, key=None, secret=None, poll_seconds=10):
        self.cfg = cfg
        self.base = (base_url or os.environ.get("ALPACA_BASE_URL")
                     or "https://paper-api.alpaca.markets").rstrip("/")
        self.key = key or os.environ.get("ALPACA_KEY_ID")
        self.secret = secret or os.environ.get("ALPACA_SECRET_KEY")
        if not (self.key and self.secret):
            raise RuntimeError("Alpaca keys missing: set ALPACA_KEY_ID and ALPACA_SECRET_KEY")
        if "paper-api" not in self.base and "127.0.0.1" not in self.base and "localhost" not in self.base:
            raise RuntimeError("Refusing to run against a non-paper Alpaca endpoint")
        self.poll_seconds = poll_seconds

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret,
            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                txt = r.read().decode()
                return json.loads(txt) if txt else None
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Alpaca {method} {path}: {e.code} {e.read().decode()[:300]}")

    @staticmethod
    def to_alpaca(sym):
        return f"{sym}/USD" if sym in CRYPTO else sym

    @staticmethod
    def from_alpaca(sym):
        s = sym.replace("/", "")
        for c in CRYPTO:
            if s == c + "USD":
                return c
        return sym

    def cash(self):
        return float(self._req("GET", "/v2/account")["cash"])

    def positions(self):
        out = {}
        for p in self._req("GET", "/v2/positions") or []:
            out[self.from_alpaca(p["symbol"])] = float(p["qty"])
        return out

    def execute(self, orders, prices, when):
        fills = []
        for o in sorted(orders, key=lambda o: 0 if o["side"] == "sell" else 1):
            sym = o["symbol"]
            qty = round(o["qty"], 6 if sym in CRYPTO else 4)
            if qty <= 0:
                continue
            body = {"symbol": self.to_alpaca(sym), "qty": str(qty), "side": o["side"],
                    "type": "market", "time_in_force": "gtc" if sym in CRYPTO else "day"}
            res = self._req("POST", "/v2/orders", body)
            oid, status, fp = res["id"], res.get("status"), res.get("filled_avg_price")
            t0 = time.time()
            while status not in ("filled", "canceled", "rejected", "expired") and time.time() - t0 < self.poll_seconds:
                time.sleep(1)
                r = self._req("GET", f"/v2/orders/{oid}")
                status, fp = r.get("status"), r.get("filled_avg_price")
            price = float(fp) if fp else prices[sym]
            fills.append({"time": when, "symbol": sym, "side": o["side"], "qty": qty, "price": price,
                          "notional": qty * price, "fee": 0.0,
                          "cash_flow": (-1 if o["side"] == "buy" else 1) * qty * price,
                          "sleeve": o["sleeve"], "reason": o["reason"],
                          "status": status or "submitted", "order_id": oid})
        return fills
