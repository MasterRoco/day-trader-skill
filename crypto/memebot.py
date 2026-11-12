#!/usr/bin/env python3
"""Paper-trading bot for freshly migrated Solana memecoins (Pump.fun -> PumpSwap).

PAPER ONLY. No wallet, no keys, no real orders. Reads GeckoTerminal's free public API.

Rules (all in CONFIG below):
  - scan new pools; a "migration" is a pool on the `pumpswap` dex younger than MAX_AGE_MIN
  - paper-buy when liquidity, FDV and 5-minute buyer activity pass the filters
  - exit: take profit at +200% (price x3), stop at -40%, or after MAX_HOLD_H hours
  - fills are charged slippage; stops are charged more (dumps fill badly)
  - EVERY migration seen is logged with its entry-time price and re-priced later, so the
    result can be checked against ALL migrations, not only the ones the filters picked.
Limits of the simulation: prices come from a 60 s poll, so a spike between polls is missed
and a crash can gap past the stop; a real swap can fail, hit a honeypot or be front-run.
"""
import json, time, datetime, os, sys, urllib.request, urllib.error

BASE = "https://api.geckoterminal.com/api/v2/networks/solana"
HDR = {"Accept": "application/json"}
HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "ledger.json")
LOG = os.path.join(HERE, "bot.log")

CONFIG = dict(
    sleeve_usd=1000.0,      # paper money for memecoins
    position_usd=50.0,      # per trade (5% of the sleeve)
    max_open=8,
    max_age_min=20,         # pool must be this fresh to count as "just migrated"
    min_liq_usd=15000,
    fdv_min=30000, fdv_max=2000000,
    min_buyers_5m=8,
    take_profit_x=3.0,      # +200%
    stop_loss=0.40,         # -40%
    max_hold_h=2.0,
    slip_buy=0.03, slip_sell=0.05, slip_stop=0.12,
    poll_s=60,
    name_cooldown_h=24,
)
C = CONFIG


def log(msg):
    line = "%s %s" % (datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def get(url):
    for i in range(3):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=20))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(10 * (i + 1)); continue
            return None
        except Exception:
            time.sleep(3)
    return None


def load():
    if os.path.exists(LEDGER):
        return json.load(open(LEDGER))
    return dict(cash=C["sleeve_usd"], open=[], closed=[], seen={}, names={})


def save(L):
    tmp = LEDGER + ".tmp"
    json.dump(L, open(tmp, "w"), indent=1)
    os.replace(tmp, LEDGER)


def f(x, d=0.0):
    try:
        return float(x)
    except Exception:
        return d


def age_min(created):
    t = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
    return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 60


def scan_new(L, now):
    for page in (1, 2):
        d = get("%s/new_pools?page=%d" % (BASE, page))
        if not d:
            continue
        for p in d["data"]:
            a = p["attributes"]
            dex = p["relationships"]["dex"]["data"]["id"]
            if dex != "pumpswap":
                continue
            addr = a["address"]
            age = age_min(a["pool_created_at"])
            if age > C["max_age_min"] or addr in L["seen"]:
                continue
            price = f(a["base_token_price_usd"])
            liq = f(a["reserve_in_usd"]); fdv = f(a["fdv_usd"])
            tx5 = (a.get("transactions") or {}).get("m5") or {}
            buyers = tx5.get("buyers") or 0
            name = a["name"].split(" / ")[0]
            rec = dict(name=name, t=now, price0=price, liq=liq, fdv=fdv, buyers5=buyers, age_min=round(age, 1), picked=False)
            why = None
            if price <= 0: why = "no price"
            elif liq < C["min_liq_usd"]: why = "liq %.0f < %d" % (liq, C["min_liq_usd"])
            elif not (C["fdv_min"] <= fdv <= C["fdv_max"]): why = "fdv %.0f" % fdv
            elif buyers < C["min_buyers_5m"]: why = "buyers %s" % buyers
            elif name in L["names"] and now - L["names"][name] < C["name_cooldown_h"] * 3600: why = "clone name"
            elif len(L["open"]) >= C["max_open"]: why = "max open"
            elif L["cash"] < C["position_usd"]: why = "no cash"
            if why is None:
                fill = price * (1 + C["slip_buy"])
                qty = C["position_usd"] / fill
                L["cash"] -= C["position_usd"]
                L["names"][name] = now
                L["open"].append(dict(addr=addr, name=name, entry=fill, qty=qty, t=now, cost=C["position_usd"], high=price))
                rec["picked"] = True
                log("BUY  %-14s fill %.8g  fdv %.0f liq %.0f buyers5 %s  (%s)" % (name, fill, fdv, liq, buyers, addr[:6]))
            else:
                rec["skip"] = why
                log("skip %-14s %s" % (name, why))
            L["seen"][addr] = rec


def prices(addrs):
    out = {}
    for i in range(0, len(addrs), 30):
        d = get("%s/pools/multi/%s" % (BASE, ",".join(addrs[i:i + 30])))
        if not d:
            continue
        for p in d["data"]:
            out[p["attributes"]["address"]] = f(p["attributes"]["base_token_price_usd"])
    return out


def manage(L, now):
    addrs = [p["addr"] for p in L["open"]]
    # also re-price recent unpicked migrations (30 min, 2 h) for the unbiased record
    watch = [a for a, r in L["seen"].items() if not r["picked"] and ("p30" not in r or "p120" not in r) and now - r["t"] < 9000]
    px = prices(list(dict.fromkeys(addrs + watch)))
    for a in watch:
        r = L["seen"][a]; p = px.get(a)
        if p is None: continue
        el = now - r["t"]
        if el >= 1800 and "p30" not in r: r["p30"] = p
        if el >= 7200 and "p120" not in r: r["p120"] = p
        r["max"] = max(r.get("max", 0), p)
    still = []
    for pos in L["open"]:
        p = px.get(pos["addr"])
        if p is None:
            if now - pos["t"] > C["max_hold_h"] * 3600:
                p = 0.0   # pool vanished or untradable: mark as total loss
            else:
                still.append(pos); continue
        pos["high"] = max(pos["high"], p)
        ratio = p / pos["entry"] if pos["entry"] else 0
        reason = None
        if ratio >= C["take_profit_x"]:
            reason, fillp = "TP", p * (1 - C["slip_sell"])
        elif ratio <= 1 - C["stop_loss"]:
            reason, fillp = "SL", p * (1 - C["slip_stop"])
        elif now - pos["t"] >= C["max_hold_h"] * 3600:
            reason, fillp = "TIME", p * (1 - C["slip_sell"])
        if reason:
            proceeds = pos["qty"] * fillp
            L["cash"] += proceeds
            pnl = proceeds - pos["cost"]
            L["closed"].append(dict(name=pos["name"], reason=reason, pnl=round(pnl, 2), pct=round(pnl / pos["cost"] * 100, 1),
                                    held_min=round((now - pos["t"]) / 60), peak_x=round(pos["high"] / pos["entry"], 2)))
            log("SELL %-14s %-4s pnl %+.2f (%+.0f%%) peak x%.2f" % (pos["name"], reason, pnl, pnl / pos["cost"] * 100, pos["high"] / pos["entry"]))
        else:
            still.append(pos)
    L["open"] = still


def summary(L):
    n = len(L["closed"]); pnl = sum(c["pnl"] for c in L["closed"])
    log("STATUS cash %.2f open %d closed %d realized %+.2f seen %d picked %d" % (
        L["cash"], len(L["open"]), n, pnl, len(L["seen"]), sum(1 for r in L["seen"].values() if r["picked"])))


def main():
    L = load()
    log("memebot start (PAPER). sleeve %.0f, %.0f per trade" % (C["sleeve_usd"], C["position_usd"]))
    tick = 0
    while True:
        now = time.time()
        try:
            scan_new(L, now)
            manage(L, now)
            save(L)
            tick += 1
            if tick % 10 == 0:
                summary(L)
        except Exception as e:
            log("error: %r" % (e,))
        time.sleep(C["poll_s"])


if __name__ == "__main__":
    main()
