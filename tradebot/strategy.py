"""Signal logic. Pure functions, no I/O, so they can be tested directly."""
import math


def trend_score(closes, lookbacks):
    """Share of lookbacks over which the price is up (0, 1/3, 2/3, 1 for three lookbacks)."""
    if len(closes) <= max(lookbacks):
        return None
    last = closes[-1]
    ups = sum(1 for lb in lookbacks if last / closes[-1 - lb] - 1 > 0)
    return ups / len(lookbacks)


def ann_vol(closes, window, periods_per_year=365):
    if len(closes) <= window:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - window, len(closes))]
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(periods_per_year)


def crypto_targets(closes_by_sym, cfg):
    """Return (weights, details). Weights are fractions of the crypto sleeve."""
    c = cfg["crypto"]
    n = len(c["symbols"])
    details, raw = {}, {}
    for s in c["symbols"]:
        cl = closes_by_sym[s]
        sc = trend_score(cl, c["lookbacks"])
        v = ann_vol(cl, c["vol_window"])
        parts = {}
        if len(cl) > max(c["lookbacks"]):
            for lb in c["lookbacks"]:
                parts[str(lb)] = cl[-1] / cl[-1 - lb] - 1
        if sc is None or v is None or v <= 0:
            raw[s] = 0.0
        else:
            raw[s] = sc * (c["vol_target"] / v) / n
        details[s] = {"score": sc, "vol": v, "returns": parts, "price": cl[-1] if cl else None}
    tot = sum(raw.values())
    scale = c["max_exposure"] / tot if tot > c["max_exposure"] else 1.0
    w = {s: raw[s] * scale for s in raw}
    for s in w:
        details[s]["target"] = w[s]
    return w, details


def nasdaq_signal(closes, cfg):
    """Return (holding_symbol, details) from the QQQ closes."""
    q = cfg["nasdaq"]
    n = q["sma_days"]
    if len(closes) < n:
        return None, {"error": f"need {n} closes, have {len(closes)}"}
    sma = sum(closes[-n:]) / n
    last = closes[-1]
    hold = q["risk_on"] if last > sma else q["risk_off"]
    return hold, {"close": last, "sma": sma, "gap": last / sma - 1, "holding": hold}
