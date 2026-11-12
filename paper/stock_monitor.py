#!/usr/bin/env python3
"""Paper stock monitor: checks stop/target on 1-minute bars every 60 s and books the exit in
trader/portfolio.json via trader-broker/ticket.py. PAPER ONLY, no broker.
Registry: paper/open_positions.json = [{"symbol","qty","entry","stop","target","t"}]
Exit price: stop/target price (or the bar's open if it gapped through), flat at 15:55 ET at last price."""
import json, os, subprocess, sys, time, datetime, urllib.request, zoneinfo
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REG = os.path.join(ROOT, "paper", "open_positions.json")
LOG = os.path.join(ROOT, "paper", "monitor.log")
NY = zoneinfo.ZoneInfo("America/New_York")

def log(m):
    s = "%s %s" % (datetime.datetime.now(NY).strftime("%m-%d %H:%M:%S"), m)
    print(s, flush=True); open(LOG, "a").write(s + "\n")

def bars(sym):
    u = "https://query2.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=1m" % sym
    try:
        d = json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=20))["chart"]["result"][0]
    except Exception:
        return []
    q = d["indicators"]["quote"][0]
    return [(t, o, h, l, c) for t, o, h, l, c in zip(d["timestamp"], q["open"], q["high"], q["low"], q["close"]) if c is not None]

def sell(sym, qty, price, why):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "trader-broker", "ticket.py"), "fill", "--symbol", sym,
                        "--side", "sell", "--qty", str(qty), "--price", "%.2f" % price], cwd=ROOT, capture_output=True, text=True)
    log("SELL %s x%s @ %.2f (%s) -> %s" % (sym, qty, price, why, (r.stdout or r.stderr).strip()[:120]))
    return r.returncode == 0

def main():
    log("stock monitor start")
    while True:
        try:
            reg = json.load(open(REG)) if os.path.exists(REG) else []
            now = datetime.datetime.now(NY)
            keep = []
            for p in reg:
                bs = bars(p["symbol"])
                done = False
                since = p.get("checked", p.get("t", 0))
                new = [b for b in bs if b[0] > since]
                for t, o, h, l, c in new:
                    if l <= p["stop"]:
                        px = min(p["stop"], o) if o < p["stop"] else p["stop"]
                        done = sell(p["symbol"], p["qty"], px, "STOP"); break
                    if h >= p["target"]:
                        done = sell(p["symbol"], p["qty"], max(p["target"], p["target"]), "TARGET"); break
                if not done and bs and now.hour * 60 + now.minute >= 15 * 60 + 55:
                    done = sell(p["symbol"], p["qty"], bs[-1][4], "FLAT 15:55")
                if not done:
                    if bs: p["checked"] = bs[-1][0]
                    keep.append(p)
            json.dump(keep, open(REG, "w"), indent=1)
        except Exception as e:
            log("error %r" % (e,))
        time.sleep(60)

if __name__ == "__main__":
    main()
