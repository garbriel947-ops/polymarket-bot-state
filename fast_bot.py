#!/usr/bin/env python3
"""
fast_bot.py — Bot Polymarket RAPIDE, paper-trading 100% SIMULE (aucune cle, aucune transaction).

Architecture :
  - SCAN momentum toutes les SCAN_SEC (gamma + volatilite) -> ouvre des positions.
  - WebSocket temps reel sur les positions ouvertes -> ferme au TP/stop en ~1s.
  - Persiste state.json en local + (option) push GitHub periodique pour le dashboard.

Lancer en local :   python3 fast_bot.py
Sur un VPS 24/7  :   voir DEPLOY (systemd).
Dependances      :   pip install websocket-client   (requests non requis : on utilise urllib+curl-like)
"""
import json, time, threading, subprocess, datetime, re, statistics, os
import urllib.request
import websocket  # websocket-client

# ---------------- CONFIG ----------------
GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
STATE = os.environ.get("PMBOT_STATE", "state.json")
SCAN_SEC = 300          # re-scan des entrees toutes les 5 min
PUSH_SEC = 300          # push GitHub toutes les 5 min (0 = desactive)
GIT_PUSH = os.environ.get("PMBOT_GIT_PUSH", "0") == "1"

CFG = {"capital0":1000,"stakePct":0.10,"minScore":0.12,"maxOpen":8,
       "entryMin":0.08,"entryMax":0.88,"cooldownH":12,"minDaysLeft":2,
       "excludeSport":True,"kStop":2.0,"rTP":1.5,"distMin":0.04,"distMax":0.15}

# ---------------- ETAT (thread-safe) ----------------
LOCK = threading.Lock()
def load_state():
    try:
        with open(STATE) as f: return json.load(f)
    except Exception:
        return {"cfg":CFG,"cash":CFG["capital0"],"open":[],"closed":[],
                "lastClosed":{},"running":True,"lastTick":None}
ST = load_state()
ST.setdefault("cfg", CFG)
for k,v in CFG.items(): ST["cfg"].setdefault(k,v)
PRICES = {}   # token_id -> dernier mid temps reel

def save_state():
    with LOCK:
        ST["lastTick"] = now_iso()
        with open(STATE,"w") as f: json.dump(ST,f,ensure_ascii=False,indent=2)

# ---------------- UTILS ----------------
def now_iso(): return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def hours_since(iso):
    if not iso: return 1e9
    try: return (datetime.datetime.now(datetime.timezone.utc)-datetime.datetime.fromisoformat(iso.replace("Z","+00:00"))).total_seconds()/3600
    except Exception: return 1e9
def log(*a): print(f"[{now_iso()}]", *a, flush=True)

def http_json(url):
    req = urllib.request.Request(url, headers={"Accept":"application/json","User-Agent":"pmbot/1.0"})
    with urllib.request.urlopen(req, timeout=40) as r: return json.loads(r.read())

def days_left(end):
    if not end: return None
    try: return (datetime.datetime.fromisoformat(end.replace("Z","+00:00"))-datetime.datetime.now(datetime.timezone.utc)).days
    except Exception: return None
def side_price(m, side):
    try:
        p=json.loads(m.get("outcomePrices") or "[]"); yes=float(p[0]); no=float(p[1]) if len(p)>1 else 1-yes
    except Exception: return float("nan")
    return yes if side=="YES" else no
def tokens_of(m):
    try: return json.loads(m.get("clobTokenIds") or "[]")
    except Exception: return []
def tag_of(q):
    q=(q or "").lower()
    if re.search(r"bitcoin|btc|ethereum| eth |solana|crypto|dogecoin|xrp",q): return "crypto"
    if re.search(r" vs | vs\.|atp|wta|nba|nfl|nhl|match|roland|stanley|padres|win the ",q): return "sport"
    return "autre"
def volatility(tokens):
    try:
        if not tokens: return None
        out = http_json(f"{CLOB}/prices-history?market={tokens[0]}&interval=1w&fidelity=60")
        ps=[float(x["p"]) for x in out.get("history",[])]
        if len(ps)<8: return None
        diffs=[abs(ps[i]-ps[i-1]) for i in range(1,len(ps))]
        return statistics.pstdev(diffs)
    except Exception: return None

def cur_value(p):
    """Prix actuel du cote detenu : temps reel (WS) si dispo, sinon dernier connu."""
    rt = PRICES.get(p.get("token"))
    return rt if rt is not None else p.get("lastPrice", p["entry"])

# ---------------- SORTIE (appelee en temps reel) ----------------
def check_exit(p, cur, reason_resol=False):
    reason=None
    if cur>=p["tp"]: reason="TP"
    elif cur<=p["stop"]: reason="STOP"
    elif reason_resol: reason="RESOL"
    if not reason: return False
    with LOCK:
        if p not in ST["open"]: return False
        pnl_pct=(cur-p["entry"])/p["entry"]; pnl_amt=p["stake"]*pnl_pct
        ST["cash"]+=p["stake"]+pnl_amt
        ST["closed"].insert(0,{"slug":p["slug"],"question":p["question"],"tag":p.get("tag"),
            "side":p["side"],"entry":p["entry"],"exit":cur,"pnlPct":pnl_pct,"pnlAmt":pnl_amt,
            "openedAt":p["openedAt"],"closedAt":now_iso(),"reason":reason})
        ST["lastClosed"][p["slug"]]=now_iso()
        ST["open"].remove(p)
    log(f"CLOTURE {reason} {p['side']} {p['question'][:40]} entry={p['entry']:.3f} exit={cur:.3f} pnl={pnl_amt:+.1f}")
    return True

# ---------------- SCAN / ENTREES (toutes les SCAN_SEC) ----------------
def scan_loop():
    while True:
        try: do_scan()
        except Exception as e: log("scan ERREUR:", e)
        save_state()
        time.sleep(SCAN_SEC)

def do_scan():
    cfg=ST["cfg"]
    allm=[]
    for off in (0,100,200):
        try: allm+=http_json(f"{GAMMA}/markets?closed=false&active=true&order=volume24hr&ascending=false&limit=100&offset={off}")
        except Exception: pass
    mmap={m.get("slug"):m for m in allm}
    # MAJ prix des positions ouvertes + detection resolution
    for p in list(ST["open"]):
        m=mmap.get(p["slug"])
        if m:
            cur=side_price(m,p["side"])
            if cur==cur: p["lastPrice"]=cur
            if m.get("closed") is True: check_exit(p, cur if cur==cur else p["entry"], reason_resol=True)
    # candidats momentum
    cand=[]
    for m in allm:
        try: yes=float(json.loads(m.get("outcomePrices") or "[]")[0])
        except Exception: continue
        day=float(m.get("oneDayPriceChange") or 0); hour=float(m.get("oneHourPriceChange") or 0)
        week=float(m.get("oneWeekPriceChange") or 0); vol=float(m.get("volume24hr") or 0); liq=float(m.get("liquidity") or 0)
        dl=days_left(m.get("endDate")); tag=tag_of(m.get("question"))
        if not(0.06<=yes<=0.94): continue
        if vol<20000 or liq<20000: continue
        if abs(day)<0.03: continue
        if dl is None or dl<cfg["minDaysLeft"]: continue
        if cfg["excludeSport"] and tag=="sport": continue
        fresh=abs(day)/(abs(week)+0.03); accel=1 if(hour*day>0 and abs(hour)>0.003) else 0
        score=abs(day)*min(fresh,3)*(1+accel)
        cand.append((score,m,day,tag))
    cand.sort(key=lambda x:-x[0])
    for score,m,day,tag in cand:
        if len(ST["open"])>=cfg["maxOpen"]: break
        if score<cfg["minScore"]: break
        slug=m.get("slug")
        if any(p["slug"]==slug for p in ST["open"]): continue
        if hours_since(ST["lastClosed"].get(slug))<cfg["cooldownH"]: continue
        side="YES" if day>0 else "NO"; entry=side_price(m,side)
        if entry!=entry or entry<cfg["entryMin"] or entry>cfg["entryMax"]: continue
        stake=cfg["capital0"]*cfg["stakePct"]
        if ST["cash"]<stake: continue
        toks=tokens_of(m); sigma=volatility(toks)
        dist=cfg["kStop"]*sigma if sigma is not None else entry*0.08
        dist=max(cfg["distMin"], min(dist, cfg["distMax"]))
        token = toks[0] if side=="YES" else (toks[1] if len(toks)>1 else None)
        with LOCK:
            ST["open"].append({"slug":slug,"question":m.get("question"),"tag":tag,"side":side,
                "entry":entry,"tp":min(entry+cfg["rTP"]*dist,0.97),"stop":max(entry-dist,0.02),
                "stake":stake,"sigma":round(sigma,4) if sigma else None,"dist":round(dist,4),
                "token":token,"lastPrice":entry,"openedAt":now_iso(),"score":round(score,3)})
            ST["cash"]-=stake
        log(f"OUVERTURE {side} {m.get('question')[:40]} @ {entry:.3f} stop={max(entry-dist,0.02):.3f} tp={min(entry+cfg['rTP']*dist,0.97):.3f}")
    log(f"scan: {len(cand)} candidats | open={len(ST['open'])} cash={ST['cash']:.0f} closed={len(ST['closed'])}")
    resubscribe()

# ---------------- WEBSOCKET TEMPS REEL (sorties) ----------------
WS_APP=None
def open_tokens():
    return [p["token"] for p in ST["open"] if p.get("token")]
def resubscribe():
    global WS_APP
    if WS_APP:
        try: WS_APP.close()
        except Exception: pass

def ws_loop():
    global WS_APP
    while True:
        toks=open_tokens()
        if not toks:
            time.sleep(5); continue
        def on_open(ws): ws.send(json.dumps({"assets_ids":toks,"type":"market"})); log(f"WS abonne a {len(toks)} tokens")
        def on_message(ws,message):
            try: d=json.loads(message)
            except Exception: return
            for e in (d if isinstance(d,list) else [d]):
                if not isinstance(e,dict): continue
                if e.get("event_type")=="book":
                    aid=e.get("asset_id")
                    bb=max((float(b["price"]) for b in e.get("bids",[])),default=None)
                    ba=min((float(a["price"]) for a in e.get("asks",[])),default=None)
                    if bb is not None and ba is not None:
                        mid=(bb+ba)/2; PRICES[aid]=mid
                        for p in list(ST["open"]):
                            if p.get("token")==aid: check_exit(p, mid)
        def on_error(ws,err): log("WS erreur:",err)
        WS_APP=websocket.WebSocketApp(WS_URL,on_open=on_open,on_message=on_message,on_error=on_error)
        WS_APP.run_forever(ping_interval=20,ping_timeout=10)
        time.sleep(2)   # reconnexion

# ---------------- PUSH GITHUB (dashboard) ----------------
def push_loop():
    while True:
        time.sleep(PUSH_SEC)
        try:
            subprocess.run(["git","add","state.json"],check=False)
            subprocess.run(["git","commit","-m",f"bot tick {now_iso()}"],check=False,
                           capture_output=True)
            subprocess.run(["git","push","origin","HEAD:main"],check=False,capture_output=True)
        except Exception as e: log("push ERREUR:",e)

# ---------------- MAIN ----------------
if __name__=="__main__":
    log("fast_bot demarre (paper trading simule). cfg:", ST["cfg"])
    threading.Thread(target=ws_loop,daemon=True).start()
    threading.Thread(target=scan_loop,daemon=True).start()
    if GIT_PUSH: threading.Thread(target=push_loop,daemon=True).start()
    try:
        while True: time.sleep(60); save_state()
    except KeyboardInterrupt:
        save_state(); log("arret propre.")
