"""Backtest 'enter after PumpSwap migration, exit at +200%' on recent pools (GeckoTerminal 5m candles).
Conservative: 5-minute entry delay, 3% slippage each way, stop checked before target inside a candle.
Sample is pools from the PumpSwap list sorted by 24h volume, so it is biased TOWARD survivors."""
import json, time, datetime, urllib.request, sys
H={"Accept":"application/json"}
def get(u):
    for i in range(4):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(u,headers=H),timeout=20))
        except Exception as e:
            if "429" in str(e): time.sleep(8); continue
            return None
now=datetime.datetime.now(datetime.timezone.utc)
pools=[]
for page in (1,2,3):
    d=get(f"https://api.geckoterminal.com/api/v2/networks/solana/dexes/pumpswap/pools?page={page}&sort=h24_volume_usd_desc")
    if not d: continue
    for p in d["data"]:
        a=p["attributes"]
        age=(now-datetime.datetime.fromisoformat(a["pool_created_at"].replace("Z","+00:00"))).total_seconds()/3600
        if 3<=age<=48: pools.append((a["address"],a["name"],age))
    time.sleep(2.2)
print("pools in sample:",len(pools)); sys.stdout.flush()
TP,SL,SLIP,MAXH=3.0,0.5,0.03,2
res=[]
for addr,name,age in pools[:40]:
    d=get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{addr}/ohlcv/minute?aggregate=5&limit=1000&currency=usd")
    time.sleep(2.2)
    if not d: continue
    c=sorted(d["data"]["attributes"]["ohlcv_list"])           # oldest first
    if len(c)<4: continue
    entry_c=c[1]                                              # enter at close of 2nd 5m candle (delay)
    entry=entry_c[4]*(1+SLIP)
    if entry<=0: continue
    out=None
    for ts,o,h,l,cl,v in c[2:2+MAXH*12]:
        if l<=entry*(1-SL): out=("SL",-SL-SLIP); break
        if h>=entry*TP:     out=("TP",TP-1-SLIP);  break
    if out is None:
        last=c[min(1+MAXH*12,len(c)-1)][4]
        out=("TIME",last*(1-SLIP)/entry-1)
    res.append((name,out[0],round(out[1]*100,1)))
    print(f"{name[:18]:18} {out[0]:4} {out[1]*100:7.1f}%"); sys.stdout.flush()
n=len(res)
if n:
    tp=sum(r[1]=="TP" for r in res); sl=sum(r[1]=="SL" for r in res); tm=n-tp-sl
    avg=sum(r[2] for r in res)/n
    print(f"\nN={n}  TP(+200%) {tp}  SL(-50%) {sl}  time-exit {tm}  avg return/trade {avg:.1f}%")
json.dump(res,open("study_result.json","w"))
