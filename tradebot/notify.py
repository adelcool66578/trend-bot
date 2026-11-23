"""Push alerts through ntfy (https://ntfy.sh): free, no account needed.

Set the NTFY_TOPIC secret to a long random topic name and subscribe to the same
topic in the ntfy app (iPhone, Android, or the ntfy web app on the Mac).
With no topic set, alerts are silently skipped.
"""
import datetime as dt
import os
import urllib.request

SENT = []  # kept for tests


def send(title, message, priority="default", tags="", click=None, topic=None, server=None):
    topic = topic or os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    server = (server or os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    headers = {"Title": title.encode("ascii", "ignore").decode(), "Priority": priority}
    if tags:
        headers["Tags"] = tags
    if click:
        headers["Click"] = click
    SENT.append({"title": title, "message": message, "priority": priority})
    try:
        req = urllib.request.Request(f"{server}/{topic}", data=message.encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception:
        return False  # an alert failing must never break the bot


def daily_messages(state, report, cfg):
    """Build the alerts for one daily cycle: trades, signals, problems, summary."""
    out = []
    fills = report.get("fills") or []
    if fills:
        lines = [f"{f['side'].upper()} {f['qty']:.6g} {f['symbol']} @ ${f['price']:,.2f} ({f['reason']})" for f in fills]
        out.append(("Trend Bot: trades placed", "\n".join(lines), "high", "chart_with_upwards_trend"))
    for e in state.get("events", []):
        if e["date"] == report["date"] and e["level"] == "signal":
            out.append(("Trend Bot: signal change", e["msg"], "high", "rotating_light"))
    if state.get("halted"):
        out.append(("Trend Bot: HALTED", state.get("halt_reason") or "", "urgent", "warning"))
    if report.get("data_failed"):
        out.append(("Trend Bot: no market data", "Every price source failed. Nothing traded; it retries in 10 minutes.", "high", "warning"))
    elif report.get("warnings"):
        out.append(("Trend Bot: warning", "\n".join(report["warnings"][:5]), "default", "warning"))
    return out  # the daily summary is sent separately each morning (morning_summary)


def morning_summary(state, live, cfg, now_local):
    """The 7am summary: account, holdings, what the bot is doing, and whether checks are running."""
    start = state.get("starting_cash") or cfg["account"]["starting_cash"]
    eq = live["equity"]
    pts = [p for p in live.get("intraday", [])]
    day_ago = (now_local.astimezone(dt.timezone.utc) - dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = [p for p in pts if p["t"] >= day_ago]
    ch24 = eq / recent[0]["equity"] - 1 if recent else 0.0
    vals = live.get("values", {})
    held = ", ".join(f"{s} ${v:,.0f}" for s, v in sorted(vals.items(), key=lambda x: -x[1]) if v > 1)
    cash = live.get("cash", 0)
    held = (held + ", " if held else "") + f"cash ${cash:,.0f}"
    sig = state.get("signals", {}) or {}
    nq = sig.get("nasdaq", {}) or {}
    ql = live.get("nasdaq_live") or {}
    lines = [f"${eq:,.2f}  ({ch24:+.2%} 24h, {eq / start - 1:+.2%} since start)",
             f"Holding: {held}"]
    if nq.get("holding"):
        gap = ql.get("gap", nq.get("gap"))
        lines.append(f"Nasdaq: QQQ {gap:+.1%} vs 200-day avg -> {nq['holding']}")
    cs = sig.get("crypto", {}) or {}
    if cs:
        lines.append("Crypto trend: " + ", ".join(
            f"{s} {round((d.get('score') or 0) * 3)}/3" for s, d in cs.items()))
    if state.get("pending_stock"):
        lines.append(f"Pending: switch to {state['pending_stock']['to']} next trading day")
    lines.append(f"Checks in last 24h: {len(recent)} (about 144 expected)")
    if state.get("halted"):
        lines.append("HALTED: " + (state.get("halt_reason") or ""))
    return ("Trend Bot morning summary", "\n".join(lines), "default", "sunrise")
