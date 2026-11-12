#!/usr/bin/env python3
"""Builds the JSON documents for the Trading Live dashboard from the local paper-trading state.
Writes dash/out/{stocks,crypto,activity,history,meta}.json. Pure read of local files plus one Yahoo
quote per open stock position. PAPER ONLY."""
import json, os, subprocess, datetime, statistics, urllib.request, zoneinfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "dash", "out")
os.makedirs(OUT, exist_ok=True)
NY = zoneinfo.ZoneInfo("America/New_York")
now = datetime.datetime.now(datetime.timezone.utc)
ISO = now.strftime("%Y-%m-%dT%H:%M:%SZ")


def rj(p, d):
    try:
        return json.load(open(os.path.join(ROOT, p)))
    except Exception:
        return d


def tail(p, n):
    try:
        return open(os.path.join(ROOT, p)).read().splitlines()[-n:]
    except Exception:
        return []


def alive(pat):
    return subprocess.run(["pgrep", "-f", pat], capture_output=True).returncode == 0


def quote(sym):
    try:
        u = "https://query2.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=1m" % sym
        d = json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=15))
        return float(d["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception:
        return None


def market_state():
    t = now.astimezone(NY)
    if t.weekday() >= 5:
        return "closed"
    m = t.hour * 60 + t.minute
    return "open" if 9 * 60 + 30 <= m < 16 * 60 else "closed"


# ---------------------------------------------------------------- stocks
cfg = rj("trader/config.json", {})
pf = rj("trader/portfolio.json", {"cash": cfg.get("budget", 5000), "positions": []})
reg = {p["symbol"]: p for p in rj("paper/open_positions.json", [])}
trades = rj("paper/trades.json", [])
start = float(cfg.get("budget", 5000))
openp, mv = [], 0.0
for pos in pf.get("positions", []):
    sym = pos["ticker"]; qty = pos["shares"]
    cost = sum(l["cost"] for l in pos.get("lots", []))
    last = quote(sym) or (cost / qty if qty else 0)
    r = reg.get(sym, {})
    mv += qty * last
    openp.append(dict(symbol=sym, qty=qty, entry=round(cost / qty, 2) if qty else 0, last=round(last, 2),
                      stop=r.get("stop"), target=r.get("target"), pnl=round(qty * last - cost, 2)))
equity = float(pf.get("cash", start)) + mv
today = now.astimezone(NY).strftime("%Y-%m-%d")
realized_today = round(sum(t["pnl"] for t in trades if t["date"] == today), 2)
stocks = dict(updated=ISO, market=market_state(), start=start, cash=round(float(pf.get("cash", start)), 2),
              equity=round(equity, 2), total_pnl=round(equity - start, 2), realized_today=realized_today,
              open=openp, trades=trades[-30:],
              limits=dict(max_loss_trade=cfg.get("max_loss_per_trade_usd"), max_daily_loss=cfg.get("max_daily_loss_usd"),
                          max_trades=cfg.get("max_trades_per_day"),
                          trades_today=sum(1 for t in trades if t["date"] == today) + len(openp)),
              rounds=[dict(t="10:15", l="Scan + Trades"), dict(t="11:30", l="Runde 2"), dict(t="13:30", l="Runde 3"),
                      dict(t="15:50", l="Tagesbilanz"), dict(t="15:55", l="Monitor schließt alles")])

# ---------------------------------------------------------------- crypto
L = rj("crypto/ledger.json", {"cash": {}, "open": [], "closed": [], "seen": {}, "pending": {}})
cash = L.get("cash", {})
if not isinstance(cash, dict):
    cash = {"migr": cash, "curve": 1000.0}
LABEL = {"migr": "Nach der Migration (PumpSwap)", "curve": "Vor der Migration (Bonding Curve)"}
RULE = {"migr": "FDV ≥ $8k · Liq. ≥ $3k · ≥ 8 Käufer/5 Min · Rug-Schutz ab 30 Min im Plus",
        "curve": "FDV $8k–50k · ≥ 10 Käufer/5 Min · mehr Käufe als Verkäufe · Prüfung ab 5 Min Alter"}
strategies, unbiased = {}, {}
for s in ("migr", "curve"):
    op = []
    unreal = 0.0
    for p in L["open"]:
        if p.get("strat", "migr") != s:
            continue
        last = p.get("last", p["entry"])
        val = p["qty"] * last
        unreal += val - p["cost"]
        op.append(dict(name=p["name"], x=round(last / p["entry"], 3) if p["entry"] else 0, value=round(val, 2),
                       pnl=round(val - p["cost"], 2), held_min=round((now.timestamp() - p["t"]) / 60),
                       peak_x=round(p["high"] / p["entry"], 2) if p["entry"] else 0, fdv0=round(p.get("fdv0") or 0)))
    op.sort(key=lambda r: -r["x"])
    cl = [c for c in L["closed"] if c.get("strat", "migr") == s]
    counts = {k: sum(1 for c in cl if c["reason"] == k) for k in ("TP", "SL", "TIME", "RUG", "DEAD")}
    counts["RUG"] += counts.pop("DEAD")  # dead pools (liquidity gone, no trades) count with the rugs
    realized = round(sum(c["pnl"] for c in cl), 2)
    start_s = 1000.0
    invested = sum(p["cost"] for p in L["open"] if p.get("strat", "migr") == s)
    eq = float(cash.get(s, start_s)) + invested + unreal
    seen = [r for r in L["seen"].values() if r.get("strat", "migr") == s]
    strategies[s] = dict(label=LABEL[s], rule=RULE[s], cash=round(float(cash.get(s, start_s)), 2), start=start_s,
                         equity=round(eq, 2), realized=realized, unrealized=round(unreal, 2), open=op,
                         closed=list(reversed(cl[-25:])), counts=counts,
                         avg_pct=round(sum(c["pct"] for c in cl) / len(cl), 1) if cl else None,
                         evaluated=len(seen), picked=sum(1 for r in seen if r.get("picked")))
    # curve pools without liquidity had already completed their curve: their price is frozen, not a real outcome
    done = [r for r in seen if "p120" in r and r.get("price0") and (s != "curve" or r.get("liq", 0) > 0)]
    xs = [r["p120"] / r["price0"] for r in done]
    unbiased[s] = dict(tracked=len(seen), finished=len(done),
                       hit3x=sum(1 for r in done if r.get("max", 0) >= 3 * r["price0"]),
                       median_x120=round(statistics.median(xs), 2) if xs else None,
                       below_half=sum(1 for x in xs if x < 0.5))
crypto = dict(updated=ISO, strategies=strategies, unbiased=unbiased, pending=len(L.get("pending", {})))

# ---------------------------------------------------------------- activity / meta / history
cl_lines = [l for l in tail("crypto/bot.log", 400) if (" BUY " in l or " SELL " in l or "RUG" in l or "STATUS" in l or "start" in l)][-40:]
events = []
for l in tail("paper/events.jsonl", 40):
    try:
        events.append(json.loads(l))
    except Exception:
        pass
activity = dict(updated=ISO, crypto=cl_lines, stocks=tail("paper/monitor.log", 15), claude=list(reversed(events))[:25])
def hb(path):
    try:
        return float(open(os.path.join(ROOT, path)).read())
    except Exception:
        return None
lp = L.get("last_poll")
mh = hb("paper/heartbeat")
meta = dict(updated=ISO, bot_alive=alive("python3 -I memebot.py"), monitor_alive=alive("stock_monitor.py"),
            bot_age_min=round((now.timestamp() - lp) / 60, 1) if lp else None,
            monitor_age_min=round((now.timestamp() - mh) / 60, 1) if mh else None,
            refresh_min=2, mode="PAPER")

hp = os.path.join(ROOT, "dash", "history.json")
hist = rj("dash/history.json", [])
hist.append(dict(t=ISO, stocks=stocks["equity"], migr=strategies["migr"]["equity"], curve=strategies["curve"]["equity"]))
hist = hist[-240:]
json.dump(hist, open(hp, "w"))
history = dict(updated=ISO, points=hist, start=dict(stocks=start, migr=1000.0, curve=1000.0))

for name, doc in dict(stocks=stocks, crypto=crypto, activity=activity, meta=meta, history=history).items():
    json.dump(doc, open(os.path.join(OUT, name + ".json"), "w"), ensure_ascii=False)
print("exported", ISO, "stocks equity", stocks["equity"], "migr", strategies["migr"]["equity"], "curve", strategies["curve"]["equity"])
