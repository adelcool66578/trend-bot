"""End-to-end check through the real CLI (`python -m tradebot refresh`), with replayed
prices, simulated clock, a skipped day, and a fake ntfy server recording every alert.
Needs the research data files (not run in GitHub Actions)."""
import datetime as dt
import json
import os
import shutil
import sys
import threading
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
from replay_check import build_series
from tradebot import __main__ as M
from tradebot.data import ReplayData

SERIES = build_series()
SENT = []


class Ntfy(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        SENT.append({"t": NOW[0], "title": self.headers["Title"], "body": body, "prio": self.headers.get("Priority")})
        self.send_response(200); self.end_headers()


NOW = [None]


class FakeLive(ReplayData):
    def __init__(self, now=None):
        super().__init__(SERIES)
        self.now = NOW[0]
        self.set_date(self.now.date().isoformat())


class FrozenDT(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW[0]


srv = HTTPServer(("127.0.0.1", 0), Ntfy)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ.update(NTFY_TOPIC="e2e", NTFY_SERVER=f"http://127.0.0.1:{srv.server_port}", GITHUB_EVENT_NAME="schedule")
M.LiveData = FakeLive
M.dt.datetime = FrozenDT

work = Path("/tmp/e2e"); shutil.rmtree(work, ignore_errors=True); work.mkdir()
args = ["--config", str(ROOT / "config.toml"), "--state", str(work / "state.json"),
        "--live", str(work / "live.json"), "--docs", str(work / "docs")]
times = ["17:40", "18:05", "18:20", "21:35", "21:45", "23:00"]
skip = "2026-03-31"          # GitHub skips a whole day
d = dt.date(2026, 3, 10)
runs = 0
while d <= dt.date(2026, 4, 20):
    if d.isoformat() != skip:
        for t in times:
            NOW[0] = dt.datetime.fromisoformat(f"{d}T{t}").replace(tzinfo=dt.timezone.utc)
            rc = M.main(["refresh"] + args)
            assert rc == 0, (d, t, rc)
            runs += 1
    d += dt.timedelta(days=1)
srv.shutdown()

st = json.loads((work / "state.json").read_text())
live = json.loads((work / "live.json").read_text())
nz = ZoneInfo("Pacific/Auckland")
fails = []

# 1) daily cycle once per day, including the catch-up after the skipped day
run_days = [e["date"] for e in st["events"] if e["level"] == "run"]
dupes = [k for k, v in Counter(run_days).items() if v > 1]
if dupes: fails.append(f"daily cycle ran twice on {dupes}")
if "2026-03-31" in run_days: fails.append("ran on the skipped day")
catch = [e for e in SENT if e["t"].date().isoformat() == "2026-04-01" and e["t"].strftime("%H:%M") == "17:40"]
print(f"{runs} refreshes, {len(set(run_days))} daily cycles, catch-up after skipped day: "
      f"{'yes' if '2026-04-01' in run_days else 'NO'}")

# 2) one summary + one review per NZ morning
by_day = defaultdict(list)
for m in SENT:
    by_day[m["t"].astimezone(nz).date().isoformat()].append(m["title"])
for day, titles in by_day.items():
    c = Counter(titles)
    if c["Trend Bot morning summary"] > 1 or c["Trend Bot: review & recommendations"] > 1:
        fails.append(f"duplicate summary/review on {day}")
summaries = sum(1 for m in SENT if m["title"] == "Trend Bot morning summary")
reviews = sum(1 for m in SENT if m["title"] == "Trend Bot: review & recommendations")
early = [m for m in SENT if m["title"] == "Trend Bot morning summary" and m["t"].astimezone(nz).hour < 7]
if early: fails.append("summary sent before 7am NZ")
print(f"morning summaries {summaries}, reviews {reviews}")

# 3) every trade notified, with the same lines as the trade log
trade_msgs = [m for m in SENT if m["title"] == "Trend Bot: trades placed"]
notified_lines = sum(len(m["body"].splitlines()) for m in trade_msgs)
if notified_lines != len(st["trades"]):
    fails.append(f"{len(st['trades'])} trades but {notified_lines} trade alert lines")
if any(m["prio"] != "high" for m in trade_msgs): fails.append("trade alert not high priority")
signals = [m for m in SENT if m["title"] == "Trend Bot: signal change"]
print(f"trades {len(st['trades'])}, trade alerts {len(trade_msgs)} ({notified_lines} lines), signal alerts {len(signals)}")
for m in trade_msgs[:3]:
    print("   e.g.", m["t"].strftime("%Y-%m-%d %H:%M"), "|", m["body"].splitlines()[0])

# 4) the one-time 'checks running' notice
n_conf = sum(1 for m in SENT if m["title"] == "Trend Bot: live checks running")
if n_conf != 1: fails.append(f"'checks running' notice sent {n_conf} times")

# 5) accounting still balances
pos, cash = st["paper"]["positions"], st["paper"]["cash"]
tot = cash + sum(q * st["prices"][s] for s, q in pos.items())
if abs(tot - st["equity"][-1]["total"]) > 1e-6: fails.append("equity does not balance")
if abs(sum(st["sleeve_cash"].values()) - cash) > 1e-6: fails.append("sleeve cash does not balance")

# 6) app files valid
for f in ("data.js", "live.js"):
    s = (work / "docs" / f).read_text()
    json.loads(s[s.index("=") + 1:].strip().rstrip(";"))
print("live review items:", [r["title"] for r in live["review"]["items"]])
print("equity", round(st["equity"][-1]["total"], 2), "| intraday points kept", len(live["intraday"]))
print("ERRORS:", fails or "none")
