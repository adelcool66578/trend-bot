"""Daily review: a ranked list of things worth acting on, sent after the morning summary.

Only two kinds of advice are given:
  * operational, from the account's own numbers (cash drag, fees, signals about to flip,
    drift, drawdown, checks not running), and
  * strategy upgrades that were actually backtested, offered only after the paper trial,
    with their measured effect. It never tunes the rules from a few days of results.
"""
import datetime as dt

# measured in the research backtests (Jan 2018 - May 2026, after fees)
TESTED_UPGRADES = [
    ("Earn interest (~4%) on idle crypto cash when trading for real",
     "backtest: +2.4%/yr on the crypto sleeve, so about +1%/yr on the whole account, same risk"),
    ("Let the crypto sleeve go up to 1.25x when volatility is low (needs margin)",
     "backtest with the interest upgrade: whole account 31.9%/yr vs 29.3%/yr, similar worst drop (-43%)"),
]


def build_review(state, live, cfg, now):
    """Return a list of dicts {rank, title, detail, kind}, most important first."""
    recs = []
    start = state.get("starting_cash") or cfg["account"]["starting_cash"]
    eq = live["equity"]
    days = (dt.date.fromisoformat(state.get("last_run") or now.date().isoformat())
            - dt.date.fromisoformat(state.get("start_date") or now.date().isoformat())).days + 1
    trial = cfg["account"].get("paper_test_days", 30)

    def add(score, title, detail, kind="action"):
        recs.append({"score": score, "title": title, "detail": detail, "kind": kind})

    # 1) system health first: nothing else matters if the bot isn't running
    pts = live.get("intraday", [])
    day_ago = (now - dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    n24 = len([p for p in pts if p["t"] >= day_ago])
    if state.get("halted"):
        add(100, "Review the safety halt", state.get("halt_reason") or "", "urgent")
    if live.get("warnings"):
        add(90, "Price data problems on the last check", "; ".join(live["warnings"][:2]), "urgent")
    if n24 and n24 < 100 and days > 1:
        add(80, "Live checks are running less often than expected",
            f"{n24} checks in 24h (about 144 expected). GitHub may be delaying the schedule; no action needed unless it drops further.", "watch")

    # 2) drawdown vs the safety stop
    peak = max([p["total"] for p in state.get("equity", [])] + [eq, start])
    dd = eq / peak - 1
    halt = cfg["safety"]["max_drawdown_halt"]
    if dd <= -0.30:
        add(85, f"Account is {dd:.0%} from its peak",
            f"The safety stop is at -{halt:.0%}. Swings like this happened in the backtest; don't change rules mid-drawdown.", "watch")

    # 3) signals close to flipping (a trade may be coming)
    ql = live.get("nasdaq_live") or (state.get("signals") or {}).get("nasdaq") or {}
    gap = ql.get("gap")
    if gap is not None:
        need = 1 / (1 + gap) - 1  # move in QQQ needed to reach the average
        if abs(gap) <= 0.03:
            add(70, "Nasdaq signal is close to flipping",
                f"QQQ is {gap:+.1%} from its 200-day average. A close {'below' if gap > 0 else 'above'} it switches the sleeve to "
                f"{cfg['nasdaq']['risk_off'] if gap > 0 else cfg['nasdaq']['risk_on']} the next trading day.", "info")
        elif gap < 0:
            add(20, "Nasdaq sleeve waiting in Treasuries",
                f"QQQ needs to rise about {need:.1%} to cross its 200-day average and switch back to QLD.", "info")
    hist = state.get("history", {}) or {}
    for s in cfg["crypto"]["symbols"]:
        h = [b[1] for b in hist.get(s, [])]
        p = (live.get("prices") or {}).get(s)
        if len(h) <= max(cfg["crypto"]["lookbacks"]) or not p:
            continue
        misses = [(lb, h[-lb] / p - 1) for lb in cfg["crypto"]["lookbacks"] if p <= h[-lb]]
        if misses:
            lb, need = min(misses, key=lambda x: x[1])
            if need <= 0.05:
                add(65, f"{s} close to a higher trend score",
                    f"A rise of about {need:.1%} puts {s} above its price {lb} days ago, which would increase its position at the next weekly check.", "info")
        elif cfg["crypto"]["lookbacks"]:
            pass

    # 4) costs
    fees = sum(t.get("fee", 0) for t in state.get("trades", []))
    if days >= 7 and fees > 0:
        yearly = fees / start * 365 / days
        if yearly > 0.01:
            add(60, "Fees are running above 1% a year",
                f"${fees:,.2f} so far (about {yearly:.1%}/yr). With real money use limit orders, and consider a wider crypto band (now {cfg['crypto']['band']:.0%}).")

    # 5) idle cash (real money only matters, but flag the size)
    cash = live.get("cash", 0)
    if cash / max(eq, 1) > 0.25:
        add(40, f"{cash / eq:.0%} of the account is idle cash",
            "That's the strategy waiting out downtrends. In the paper account it earns 0%; with real money, keep it in a "
            "high-interest account or a yield-bearing stablecoin (about 4%), which the backtest says adds about 1% a year to the whole account.")

    # 6) sleeve drift
    tot = live.get("crypto", 0) + live.get("nasdaq", 0)
    if tot > 0 and abs(live.get("crypto", 0) / tot - cfg["allocation"]["crypto"]) > 0.05:
        add(30, "Sleeves have drifted from 50/50",
            f"Crypto {live['crypto'] / tot:.0%} / Nasdaq {live['nasdaq'] / tot:.0%}. The monthly reset fixes this automatically.", "info")

    # 7) strategy changes: only tested ones, only after the trial
    if days < trial:
        add(10, f"No rule changes recommended (day {days} of {trial})",
            "Too early to judge the strategy: changing rules on a few days of results is how backtested systems get over-fitted. "
            "The paper month tests that the machinery works.", "info")
    else:
        bench = (state.get("equity") or [{}])[-1].get("bench_qqq")
        ret = eq / start - 1
        cmp = f" vs {bench / start - 1:+.1%} for holding QQQ" if bench else ""
        add(50, "Paper trial complete: decide on real money",
            f"Result {ret:+.1%}{cmp} over {days} days. Judge it on whether every trade followed the rules, not on the return.")
        for t, why in TESTED_UPGRADES:
            add(15, "Tested upgrade: " + t, why, "option")

    recs.sort(key=lambda r: -r["score"])
    for i, r in enumerate(recs, 1):
        r["rank"] = i
    return recs


def review_message(recs, limit=6):
    lines = [f"{r['rank']}. {r['title']}" + (f": {r['detail']}" if r["detail"] else "") for r in recs[:limit]]
    return ("Trend Bot: review & recommendations", "\n".join(lines), "low", "memo")
