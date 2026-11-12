#!/usr/bin/env python3
"""Paper-trading bot for Solana memecoins, two strategies side by side (PAPER ONLY, no wallet, no keys).

  migr  = buy right after migration: a fresh pool on the `pumpswap` dex
  curve = buy BEFORE migration: a Pump.fun bonding-curve pool (dex `pump-fun`), evaluated at
          age >= CURVE_EVAL_MIN minutes so there is real buyer activity to look at

Both use the same exits so they can be compared fairly:
  take profit at +200% (x3), stop at -40%, time exit after MAX_HOLD_H hours.
Each strategy has its own paper sleeve. Slippage is charged on every fill (stops cost most).

Honest record: every candidate that was EVALUATED (picked or not) is re-priced after 30 min and
2 h and its peak is kept, so the result can be checked against all candidates, not only the
ones the filter picked.
Limits: prices come from a 60 s poll (spikes between polls are missed, crashes can gap past a
stop); a real swap can fail, hit a honeypot or be front-run; liquidity/FDV figures are the API's.
"""
import json, time, datetime, os, urllib.request, urllib.error

BASE = "https://api.geckoterminal.com/api/v2/networks/solana"
HDR = {"Accept": "application/json"}
HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "ledger.json")
LOG = os.path.join(HERE, "bot.log")

COMMON = dict(sleeve_usd=1000.0, position_usd=50.0, max_open=8, take_profit_x=3.0, stop_loss=0.40,
              max_hold_h=2.0, slip_buy=0.03, slip_sell=0.05, slip_stop=0.12, poll_s=60, name_cooldown_h=24,
              max_drawdown=0.50)   # no new entries once a strategy is down 50% (open positions still managed)
STRAT = {
    "migr":  dict(dex="pumpswap", max_age_min=20, min_liq_usd=3000, fdv_min=8000, fdv_max=2000000, min_buyers_5m=8),
    "curve": dict(dex="pump-fun", eval_min=5, eval_max=20, min_liq_usd=0, fdv_min=8000, fdv_max=50000, min_buyers_5m=10),
}
RUG = dict(min_pool_age_min=30,      # migr positions: rule is active from this pool age on ...
           only_in_profit=True,       # ... and only while the position is in profit (user rule)
           liq_drop=0.40,             # pool liquidity down 40% from entry/peak = liquidity pulled
           price_dump=0.25,           # price 25% under the peak since entry
           sell_x=2.0, min_sells=15)  # 5-min sells >= 2x buys and at least 15 sells
PENDING_MAX = 120   # curve pools waiting for their evaluation age


def log(msg):
    line = "%s %s" % (datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    with open(LOG, "a") as fh:
        fh.write(line + "\n")


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
        L = json.load(open(LEDGER))
    else:
        L = {}
    cash = L.get("cash", COMMON["sleeve_usd"])
    if not isinstance(cash, dict):          # migrate the v1 ledger
        cash = {"migr": cash, "curve": COMMON["sleeve_usd"]}
    L["cash"] = cash
    for k in ("open", "closed"):
        L.setdefault(k, [])
    for rec in L["open"] + L["closed"]:
        rec.setdefault("strat", "migr")
    L.setdefault("seen", {}); L.setdefault("names", {}); L.setdefault("pending", {})
    for r in L["seen"].values():
        r.setdefault("strat", "migr")
    return L


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


def strat_equity(L, strat):
    """Cash plus open positions at their last known price."""
    return L["cash"][strat] + sum(p["qty"] * p.get("last", p["entry"]) for p in L["open"] if p["strat"] == strat)


def evaluate(L, now, strat, a, addr):
    """Apply the strategy's filter to pool attributes `a`; record the candidate; paper-buy if it passes."""
    S = STRAT[strat]
    price = f(a.get("base_token_price_usd"))
    liq = f(a.get("reserve_in_usd")); fdv = f(a.get("fdv_usd"))
    tx5 = (a.get("transactions") or {}).get("m5") or {}
    buyers = tx5.get("buyers") or 0
    buys, sells = tx5.get("buys") or 0, tx5.get("sells") or 0
    name = a["name"].split(" / ")[0]
    rec = dict(strat=strat, name=name, t=now, price0=price, liq=liq, fdv=fdv, buyers5=buyers,
               age_min=round(age_min(a["pool_created_at"]), 1), picked=False)
    why = None
    n_open = sum(1 for p in L["open"] if p["strat"] == strat)
    if price <= 0: why = "no price"
    elif liq < S["min_liq_usd"]: why = "liq %.0f" % liq
    elif not (S["fdv_min"] <= fdv <= S["fdv_max"]): why = "fdv %.0f" % fdv
    elif buyers < S["min_buyers_5m"]: why = "buyers %s" % buyers
    elif strat == "curve" and buys <= sells: why = "sells>=buys"
    elif name in L["names"] and now - L["names"][name] < COMMON["name_cooldown_h"] * 3600: why = "clone name"
    elif n_open >= COMMON["max_open"]: why = "max open"
    elif L["cash"][strat] < COMMON["position_usd"]: why = "no cash"
    elif strat_equity(L, strat) < COMMON["sleeve_usd"] * (1 - COMMON["max_drawdown"]): why = "drawdown pause"
    if why is None:
        fill = price * (1 + COMMON["slip_buy"])
        L["cash"][strat] -= COMMON["position_usd"]
        L["names"][name] = now
        L["open"].append(dict(strat=strat, addr=addr, name=name, entry=fill, qty=COMMON["position_usd"] / fill, t=now,
                              cost=COMMON["position_usd"], high=price, fdv0=fdv,
                              liq0=liq, liq_peak=liq, created_ts=now - rec["age_min"] * 60))
        rec["picked"] = True
        log("BUY  [%s] %-14s fill %.8g fdv %.0f liq %.0f buyers5 %s (%s)" % (strat, name, fill, fdv, liq, buyers, addr[:6]))
    else:
        rec["skip"] = why
    # keep a record only of candidates that were real enough to trade at all (fdv in range) -> bounded size
    if S["fdv_min"] <= fdv:
        L["seen"][addr] = rec


def scan(L, now, deep=False):
    for page in (range(1, 11) if deep else (1, 2, 3)):
        d = get("%s/new_pools?page=%d" % (BASE, page))
        if not d:
            continue
        for p in d["data"]:
            a = p["attributes"]; addr = a["address"]
            dex = p["relationships"]["dex"]["data"]["id"]
            if addr in L["seen"] or addr in L["pending"]:
                continue
            if dex == STRAT["migr"]["dex"]:
                if age_min(a["pool_created_at"]) <= STRAT["migr"]["max_age_min"]:
                    evaluate(L, now, "migr", a, addr)
            elif dex == STRAT["curve"]["dex"] and len(L["pending"]) < PENDING_MAX:
                L["pending"][addr] = dict(t=now, created=a["pool_created_at"])


def eval_pending(L, now):
    S = STRAT["curve"]
    due, drop = [], []
    for addr, pr in L["pending"].items():
        a = age_min(pr["created"])
        if a > S["eval_max"]: drop.append(addr)
        elif a >= S["eval_min"]: due.append(addr)
    for addr in drop:
        L["pending"].pop(addr, None)
    for i in range(0, len(due), 30):
        batch = due[i:i + 30]
        d = get("%s/pools/multi/%s" % (BASE, ",".join(batch)))
        if not d:
            continue
        got = set()
        for p in d["data"]:
            a = p["attributes"]; got.add(a["address"])
            evaluate(L, now, "curve", a, a["address"])
            L["pending"].pop(a["address"], None)
        for addr in batch:
            if addr not in got:
                L["pending"].pop(addr, None)   # pool gone: nothing to evaluate


def prices(addrs):
    """-> (price by pool, attributes by pool)"""
    out, attrs = {}, {}
    for i in range(0, len(addrs), 30):
        d = get("%s/pools/multi/%s" % (BASE, ",".join(addrs[i:i + 30])))
        if not d:
            continue
        for p in d["data"]:
            a = p["attributes"]
            out[a["address"]] = f(a["base_token_price_usd"])
            attrs[a["address"]] = a
    return out, attrs


def rug_signals(pos, a, now, ratio):
    """Return the list of rug-pull warning signs for a migrated position, or [] when the rule does not apply."""
    if pos["strat"] != "migr" or a is None:
        return []
    age = (now - pos.get("created_ts", pos["t"])) / 60
    if age < RUG["min_pool_age_min"] or (RUG["only_in_profit"] and ratio <= 1.0):
        return []
    sig = []
    liq = f(a.get("reserve_in_usd"))
    base = max(pos.get("liq0", 0), pos.get("liq_peak", 0))
    pos["liq_peak"] = max(pos.get("liq_peak", 0), liq)
    tx_all = (((a.get("transactions") or {}).get("m5") or {}).get("buys") or 0) + \
             (((a.get("transactions") or {}).get("m5") or {}).get("sells") or 0)
    if base > 0 and liq > 0 and liq <= base * (1 - RUG["liq_drop"]) and tx_all < 20:
        sig.append("liquidity -%.0f%%" % ((1 - liq / base) * 100))
    if pos["high"] > 0 and f(a.get("base_token_price_usd")) <= pos["high"] * (1 - RUG["price_dump"]):
        sig.append("price %.0f%% under peak" % ((1 - f(a.get("base_token_price_usd")) / pos["high"]) * 100))
    tx5 = (a.get("transactions") or {}).get("m5") or {}
    buys, sells = tx5.get("buys") or 0, tx5.get("sells") or 0
    if sells >= RUG["min_sells"] and sells >= RUG["sell_x"] * max(buys, 1):
        sig.append("sell pressure %d sells/%d buys" % (sells, buys))
    return sig


def candles(addr):
    """1-minute candles, oldest first: [(ts, o, h, l, c, v)] (GeckoTerminal keeps ~16 h at 1 000 candles)."""
    d = get("%s/pools/%s/ohlcv/minute?aggregate=1&limit=1000&currency=usd" % (BASE, addr))
    time.sleep(2.2)
    try:
        return sorted(tuple(float(x) for x in c) for c in d["data"]["attributes"]["ohlcv_list"])
    except Exception:
        return []


def replay(pos, now):
    """Catch-up after a pause: walk the 1-minute candles since entry and apply take profit, stop and
    time exit in order. Within one candle the stop is assumed to hit first (conservative). The rug rule
    needs live liquidity data and is NOT replayed. -> (reason, fill_price, exit_ts) or None."""
    cs = [c for c in candles(pos["addr"]) if c[0] >= pos["t"] - 60]
    if not cs:
        return None
    entry = pos["entry"]
    for ts, o, h, l, c, v in cs:
        pos["high"] = max(pos["high"], h)
        if ts - pos["t"] >= COMMON["max_hold_h"] * 3600:
            return "TIME", c * (1 - COMMON["slip_sell"]), ts
        if l <= entry * (1 - COMMON["stop_loss"]):
            return "SL", entry * (1 - COMMON["stop_loss"]) * (1 - COMMON["slip_stop"]), ts
        if h >= entry * COMMON["take_profit_x"]:
            return "TP", entry * COMMON["take_profit_x"] * (1 - COMMON["slip_sell"]), ts
    pos["last"] = cs[-1][4]
    return None


def replay_seen(r, addr):
    """Fill p30 / p120 / max for an unpicked candidate from candles, so pauses do not distort the record."""
    cs = [c for c in candles(addr) if c[0] >= r["t"] - 60]
    if not cs:
        return
    r["max"] = max([r.get("max", 0)] + [c[2] for c in cs if c[0] - r["t"] <= 7200])
    for ts, o, h, l, c, v in cs:
        if ts - r["t"] >= 1800 and "p30" not in r: r["p30"] = c
        if ts - r["t"] >= 7200 and "p120" not in r: r["p120"] = c; break


def close_pos(L, pos, reason, fillp, when, note=""):
    proceeds = pos["qty"] * fillp
    L["cash"][pos["strat"]] += proceeds
    pnl = proceeds - pos["cost"]
    L["closed"].append(dict(strat=pos["strat"], name=pos["name"], reason=reason, pnl=round(pnl, 2),
                            pct=round(pnl / pos["cost"] * 100, 1), held_min=round((when - pos["t"]) / 60),
                            peak_x=round(pos["high"] / pos["entry"], 2), fdv0=pos.get("fdv0"), replay=bool(note)))
    log("SELL [%s] %-14s %-4s pnl %+.2f (%+.0f%%) peak x%.2f %s" % (pos["strat"], pos["name"], reason, pnl,
                                                                  pnl / pos["cost"] * 100, pos["high"] / pos["entry"], note))


def catch_up(L, now):
    """If the bot was paused (the sandbox sleeps between Claude's turns), settle what happened meanwhile."""
    gap = now - L.get("last_poll", now)
    if gap < 90:
        return
    log("PAUSE detected: %.0f min without polling -> replaying candles" % (gap / 60))
    keep = []
    for pos in L["open"]:
        res = replay(pos, now)
        if res:
            reason, fillp, ts = res
            close_pos(L, pos, reason, fillp, ts, note="(replayed)")
        else:
            keep.append(pos)
    L["open"] = keep
    n = 0
    for a, r in list(L["seen"].items()):
        if not r["picked"] and "p120" not in r and now - r["t"] >= 1800 and n < 10:
            replay_seen(r, a); n += 1
    log("catch-up done: %d candidates re-priced" % n)


def manage(L, now):
    addrs = [p["addr"] for p in L["open"]]
    watch = [a for a, r in L["seen"].items() if not r["picked"] and "p120" not in r and now - r["t"] < 9000]
    px, attrs = prices(list(dict.fromkeys(addrs + watch)))
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
            if now - pos["t"] > COMMON["max_hold_h"] * 3600:
                p = 0.0   # pool vanished / untradable: total loss
            else:
                still.append(pos); continue
        pos["high"] = max(pos["high"], p)
        pos["last"] = p
        ratio = p / pos["entry"] if pos["entry"] else 0
        reason = None
        a_ = attrs.get(pos["addr"]) or {}
        t5 = (a_.get("transactions") or {}).get("m5") or {}
        liq_now = f(a_.get("reserve_in_usd"), -1)
        if pos["strat"] == "migr" and a_ and 0 <= liq_now < max(100.0, 0.02 * pos.get("liq0", 0)) and (t5.get("buys") or 0) + (t5.get("sells") or 0) == 0 \
                and now - pos["t"] >= 300:
            close_pos(L, pos, "DEAD", 0.0, now, note="(no liquidity, no trades)")
            continue
        sig = rug_signals(pos, attrs.get(pos["addr"]), now, ratio)
        rug = bool(sig) and (any(x.startswith("liquidity") for x in sig) or len(sig) >= 2)
        if ratio >= COMMON["take_profit_x"]:
            reason, fillp = "TP", p * (1 - COMMON["slip_sell"])
        elif rug:
            reason, fillp = "RUG", p * (1 - COMMON["slip_stop"])
            if any(x.startswith("liquidity -") and float(x.split("-")[1].rstrip("%")) >= 90 for x in sig):
                fillp = 0.0   # liquidity pulled: in reality nothing can be sold any more -> total loss
            log("RUG? [%s] %-14s %s" % (pos["strat"], pos["name"], "; ".join(sig)))
        elif ratio <= 1 - COMMON["stop_loss"]:
            reason, fillp = "SL", p * (1 - COMMON["slip_stop"])
        elif now - pos["t"] >= COMMON["max_hold_h"] * 3600:
            reason, fillp = "TIME", p * (1 - COMMON["slip_sell"])
        if reason:
            proceeds = pos["qty"] * fillp
            L["cash"][pos["strat"]] += proceeds
            pnl = proceeds - pos["cost"]
            L["closed"].append(dict(strat=pos["strat"], name=pos["name"], reason=reason, pnl=round(pnl, 2),
                                    pct=round(pnl / pos["cost"] * 100, 1), held_min=round((now - pos["t"]) / 60),
                                    peak_x=round(pos["high"] / pos["entry"], 2), fdv0=pos.get("fdv0")))
            log("SELL [%s] %-14s %-4s pnl %+.2f (%+.0f%%) peak x%.2f" % (pos["strat"], pos["name"], reason, pnl,
                                                                      pnl / pos["cost"] * 100, pos["high"] / pos["entry"]))
        else:
            still.append(pos)
    L["open"] = still


def summary(L):
    for s in STRAT:
        cl = [c for c in L["closed"] if c["strat"] == s]
        op = [p for p in L["open"] if p["strat"] == s]
        tp = sum(c["reason"] == "TP" for c in cl)
        log("STATUS [%s] cash %.2f open %d closed %d (TP %d) realized %+.2f | evaluated %d picked %d pending %d" % (
            s, L["cash"][s], len(op), len(cl), tp, sum(c["pnl"] for c in cl),
            sum(1 for r in L["seen"].values() if r["strat"] == s),
            sum(1 for r in L["seen"].values() if r["strat"] == s and r["picked"]), len(L["pending"])))


def main():
    L = load()
    log("memebot v2 start (PAPER): strategies %s, sleeve %.0f each, %.0f per trade" % (
        ",".join(STRAT), COMMON["sleeve_usd"], COMMON["position_usd"]))
    tick = 0
    while True:
        now = time.time()
        try:
            deep = now - L.get("last_poll", now) >= 90
            catch_up(L, now)
            scan(L, now, deep)
            eval_pending(L, now)
            manage(L, now)
            L["last_poll"] = time.time()
            save(L)
            tick += 1
            if tick % 10 == 0:
                summary(L)
        except Exception as e:
            log("error: %r" % (e,))
        time.sleep(COMMON["poll_s"])


if __name__ == "__main__":
    main()
