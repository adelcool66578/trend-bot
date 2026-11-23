"""Push alerts through ntfy (https://ntfy.sh): free, no account needed.

Set the NTFY_TOPIC secret to a long random topic name and subscribe to the same
topic in the ntfy app (iPhone, Android, or the ntfy web app on the Mac).
With no topic set, alerts are silently skipped.
"""
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
    eq = state.get("equity") or []
    if eq and not report.get("data_failed"):
        cur = eq[-1]["total"]
        prev = eq[-2]["total"] if len(eq) > 1 else state.get("starting_cash", cur)
        start = state.get("starting_cash") or cfg["account"]["starting_cash"]
        held = ", ".join(f"{p['symbol']} ${p['value']:,.0f}" for p in state.get("positions_view", [])) or "all cash"
        out.append(("Trend Bot daily summary",
                    f"${cur:,.2f} ({cur / prev - 1:+.2%} today, {cur / start - 1:+.2%} since start)\nHolding: {held}",
                    "low", "bar_chart"))
    return out
