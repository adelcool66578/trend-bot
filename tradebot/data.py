"""Market data. Live sources use only the standard library.

A source returns daily bars as a list of (date_iso, close) sorted ascending,
containing COMPLETED days only, plus a current price used for fills.

Crypto: Kraken public OHLC (primary), Coinbase Exchange candles (fallback).
Stocks: Yahoo chart API (primary), Nasdaq.com historical API, Alpaca data (if keys).
"""
import csv
import datetime as dt
import json
import os
import time
import urllib.parse
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) TrendBot/1.0",
      "Accept": "application/json"}


class DataError(Exception):
    pass


def _get_json(url, headers=None, timeout=20, retries=2):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # network or parse error
            last = e
            time.sleep(1.5 * (i + 1))
    raise DataError(f"{url}: {last}")


def _utc_date(ts):
    return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).date().isoformat()


# ---------------- crypto ----------------
KRAKEN_PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}


def parse_kraken(payload, today_iso):
    if payload.get("error"):
        raise DataError(f"kraken error {payload['error']}")
    res = payload["result"]
    key = [k for k in res if k != "last"][0]
    rows = res[key]
    bars, current = [], None
    for row in rows:
        d = _utc_date(row[0]); close = float(row[4])
        if d >= today_iso:
            current = close  # today's candle is still forming
        else:
            bars.append((d, close))
    if current is None and bars:
        current = bars[-1][1]
    return bars, current


def parse_coinbase(payload, today_iso):
    rows = sorted(payload, key=lambda r: r[0])
    bars, current = [], None
    for row in rows:
        d = _utc_date(row[0]); close = float(row[4])
        if d >= today_iso:
            current = close
        else:
            bars.append((d, close))
    if current is None and bars:
        current = bars[-1][1]
    return bars, current


# ---------------- stocks ----------------
def parse_yahoo(payload, now_ts=None):
    res = payload["chart"]["result"][0]
    ts = res["timestamp"]; closes = res["indicators"]["quote"][0]["close"]
    meta = res.get("meta", {})
    tzname = meta.get("exchangeTimezoneName", "America/New_York")
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tzname)
    except Exception:
        tz = dt.timezone(dt.timedelta(hours=-5))
    bars = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        d = dt.datetime.fromtimestamp(int(t), tz).date().isoformat()
        bars.append((d, float(c)))
    # Yahoo includes the in-progress session while the market is open; drop it then
    reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    now_ts = now_ts if now_ts is not None else time.time()
    if reg and bars and now_ts < reg.get("end", 0):
        session_day = dt.datetime.fromtimestamp(int(reg["start"]), tz).date().isoformat()
        if bars[-1][0] == session_day:
            bars = bars[:-1]
    dedup = {}
    for d, c in bars:
        dedup[d] = c
    bars = sorted(dedup.items())
    current = float(meta.get("regularMarketPrice") or bars[-1][1])
    return bars, current


def parse_nasdaq(payload):
    rows = payload["data"]["tradesTable"]["rows"]
    bars = []
    for r in rows:
        m, d, y = r["date"].split("/")
        c = float(str(r["close"]).replace("$", "").replace(",", ""))
        bars.append((f"{y}-{int(m):02d}-{int(d):02d}", c))
    bars.sort()
    return bars, bars[-1][1]


def parse_alpaca_bars(payload, symbol):
    rows = payload["bars"].get(symbol, []) if isinstance(payload["bars"], dict) else payload["bars"]
    bars = [(r["t"][:10], float(r["c"])) for r in rows]
    bars.sort()
    return bars, bars[-1][1]


class LiveData:
    def __init__(self, now=None):
        self.now = now or dt.datetime.now(dt.timezone.utc)
        self.today = self.now.date().isoformat()
        self.log = []
        self._cache = {}

    def crypto(self, sym):
        key = ("c", sym)
        if key not in self._cache:
            self._cache[key] = self._crypto(sym)
        return self._cache[key]

    def stock(self, sym):
        key = ("s", sym)
        if key not in self._cache:
            self._cache[key] = self._stock(sym)
        return self._cache[key]

    def _crypto(self, sym):
        errors = []
        try:
            pair = KRAKEN_PAIRS.get(sym, sym + "USD")
            p = _get_json(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1440")
            bars, cur = parse_kraken(p, self.today)
            if len(bars) > 150:
                self.log.append(f"{sym}: Kraken, {len(bars)} bars to {bars[-1][0]}")
                return bars, cur
            errors.append("kraken: too few bars")
        except Exception as e:
            errors.append(f"kraken: {e}")
        try:
            p = _get_json(f"https://api.exchange.coinbase.com/products/{sym}-USD/candles?granularity=86400")
            bars, cur = parse_coinbase(p, self.today)
            self.log.append(f"{sym}: Coinbase fallback, {len(bars)} bars to {bars[-1][0]}")
            return bars, cur
        except Exception as e:
            errors.append(f"coinbase: {e}")
        raise DataError(f"no crypto data for {sym}: {errors}")

    def _stock(self, sym):
        errors = []
        for host in ("query1", "query2"):
            try:
                p = _get_json(f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}?range=2y&interval=1d")
                bars, cur = parse_yahoo(p, now_ts=self.now.timestamp())
                if len(bars) > 210:
                    self.log.append(f"{sym}: Yahoo, {len(bars)} bars to {bars[-1][0]}")
                    return bars, cur
                errors.append(f"yahoo {host}: too few bars")
            except Exception as e:
                errors.append(f"yahoo {host}: {e}")
        try:
            start = (self.now.date() - dt.timedelta(days=500)).isoformat()
            p = _get_json(f"https://api.nasdaq.com/api/quote/{sym}/historical?assetclass=etf"
                          f"&fromdate={start}&todate={self.today}&limit=9999")
            bars, cur = parse_nasdaq(p)
            self.log.append(f"{sym}: Nasdaq.com fallback, {len(bars)} bars to {bars[-1][0]}")
            return bars, cur
        except Exception as e:
            errors.append(f"nasdaq: {e}")
        key, sec = os.environ.get("ALPACA_KEY_ID"), os.environ.get("ALPACA_SECRET_KEY")
        if key and sec:
            try:
                start = (self.now.date() - dt.timedelta(days=500)).isoformat()
                q = urllib.parse.urlencode({"symbols": sym, "timeframe": "1Day", "start": start,
                                            "limit": 10000, "feed": "iex", "adjustment": "all"})
                p = _get_json(f"https://data.alpaca.markets/v2/stocks/bars?{q}",
                              headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec})
                bars, cur = parse_alpaca_bars(p, sym)
                self.log.append(f"{sym}: Alpaca data fallback, {len(bars)} bars")
                return bars, cur
            except Exception as e:
                errors.append(f"alpaca: {e}")
        raise DataError(f"no stock data for {sym}: {errors}")


class ReplayData:
    """Historical data for replaying the bot day by day (testing only).

    On run date D: crypto completed bars end at D-1 and the fill price is D's close;
    stock bars include D (the bot runs after the US close).
    """

    def __init__(self, series):
        import bisect
        self._bisect = bisect
        self.series = series  # sym -> list[(date, close)] sorted
        self.dates = {s: [x[0] for x in v] for s, v in series.items()}
        self.today = None
        self.log = []

    def set_date(self, d):
        self.today = d

    def _upto(self, sym, d, inclusive):
        ds = self.dates[sym]
        i = (self._bisect.bisect_right if inclusive else self._bisect.bisect_left)(ds, d)
        return self.series[sym][max(0, i - 400):i]  # 400 bars is plenty for every signal

    def crypto(self, sym):
        bars = self._upto(sym, self.today, inclusive=False)
        ds = self.dates[sym]
        i = self._bisect.bisect_left(ds, self.today)
        cur = self.series[sym][i][1] if i < len(ds) and ds[i] == self.today else None
        if not bars:
            raise DataError("no bars")
        return bars, (cur if cur is not None else bars[-1][1])

    def stock(self, sym):
        bars = self._upto(sym, self.today, inclusive=True)
        if not bars:
            raise DataError("no bars")
        return bars, bars[-1][1]


def load_csv_series(path, date_col, close_col):
    out = []
    with open(path) as f:
        for row in csv.DictReader(f):
            v = row.get(close_col)
            if v:
                out.append((row[date_col][:10], float(v)))
    out.sort()
    return out
