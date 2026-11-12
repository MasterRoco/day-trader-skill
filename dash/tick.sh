#!/usr/bin/env bash
# One wake-up of the paper-trading loop: keep the bot and the stock monitor alive, export the dashboard data.
# Stop switch: create paper/STOP to end the 2-minute wake-up chain.
cd "$(dirname "$0")/.." || exit 1
if [ -f paper/STOP ]; then echo "STOP"; exit 0; fi
pgrep -f "python3 -I memebot.py" >/dev/null || (cd crypto && nohup python3 -I memebot.py >> bot.out 2>&1 &) 
pgrep -f "stock_monitor.py" >/dev/null || (nohup python3 -I paper/stock_monitor.py >> paper/monitor.out 2>&1 &)
# give a restarted or resumed bot one poll cycle to catch up before exporting
timeout 75 bash -c 'start=$(stat -c %Y crypto/ledger.json 2>/dev/null || echo 0); until [ "$(stat -c %Y crypto/ledger.json 2>/dev/null || echo 0)" -gt "$start" ]; do sleep 3; done' 
python3 -I dash/export.py
grep -E "BUY|SELL|PAUSE|RUG" crypto/bot.log | tail -3
