#!/usr/bin/env bash
# One wake-up of the paper-trading loop: keep the bot and the stock monitor alive, export the dashboard data.
# Stop switch: create paper/STOP to end the 2-minute wake-up chain.
cd "$(dirname "$0")/.." || exit 1
if [ -f paper/STOP ]; then echo "STOP"; exit 0; fi
pgrep -f "python3 -I memebot.py" >/dev/null || (cd crypto && nohup python3 -I memebot.py >> bot.out 2>&1 &) 
pgrep -f "stock_monitor.py" >/dev/null || (nohup python3 -I paper/stock_monitor.py >> paper/monitor.out 2>&1 &)
# give a restarted or resumed bot one poll cycle to catch up before exporting
timeout 75 bash -c 'start=$(stat -c %Y crypto/ledger.json 2>/dev/null || echo 0); until [ "$(stat -c %Y crypto/ledger.json 2>/dev/null || echo 0)" -gt "$start" ]; do sleep 3; done' 
python3 -I - <<'PY'
# turn new BUY/SELL/RUG/PAUSE lines of the bot log into dashboard events (German), once each
import json, os, datetime, zoneinfo
off_p, log_p = "paper/.events_offset", "crypto/bot.log"
lines = open(log_p).read().splitlines() if os.path.exists(log_p) else []
off = int(open(off_p).read()) if os.path.exists(off_p) else len(lines)
ny = datetime.datetime.now(zoneinfo.ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
out = []
for l in lines[off:]:
    w = l.split()
    if len(w) < 4: continue
    if w[1] == "BUY": out.append("Memecoin gekauft %s %s, FDV %s" % (w[2], w[3], w[7] if len(w) > 7 else "?"))
    elif w[1] == "SELL": out.append("Memecoin verkauft %s %s: %s %s %s" % (w[2], w[3], w[4], w[6], w[7] if len(w) > 7 else ""))
    elif w[1].startswith("RUG"): out.append("Rug-Verdacht: " + " ".join(w[2:]))
    elif w[1] == "PAUSE": out.append("Pause nachgeholt: " + " ".join(w[2:]))
with open("paper/events.jsonl", "a") as f:
    for t in out: f.write(json.dumps({"t": ny, "text": t}, ensure_ascii=False) + "\n")
open(off_p, "w").write(str(len(lines)))
PY
python3 -I dash/export.py
grep -E "BUY|SELL|PAUSE|RUG" crypto/bot.log | tail -3
