#!/usr/bin/env python3
"""
Bot Polymarket — paper trading 100% SIMULE (aucune transaction, aucune cle).
Un "tick" : lit state.json -> recupere les prix Polymarket -> ferme (TP/stop/resol)
puis ouvre des positions (momentum) -> reecrit state.json.
Adapte au tick HORAIRE du cloud : exclut le sport live, exige echeance >= minDaysLeft.
Lance dans le repo clone (state.json dans le cwd). Reseau via curl (urllib = 403).
"""
import json, subprocess, datetime, re, sys
from collections import Counter

GAMMA = "https://gamma-api.polymarket.com"
STATE = "state.json"

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def hours_since(iso):
    if not iso: return 1e9
    try:
        t = datetime.datetime.fromisoformat(iso.replace("Z","+00:00"))
        return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds()/3600
    except Exception:
        return 1e9

def curl_json(url):
    out = subprocess.run(["curl","-s",url,"-H","Accept: application/json"],
                         capture_output=True, text=True, timeout=40).stdout
    return json.loads(out)

def days_left(end):
    if not end: return None
    try:
        t = datetime.datetime.fromisoformat(end.replace("Z","+00:00"))
        return (t - datetime.datetime.now(datetime.timezone.utc)).days
    except Exception:
        return None

def side_price(m, side):
    try:
        p = json.loads(m.get("outcomePrices") or "[]")
        yes = float(p[0]); no = float(p[1]) if len(p) > 1 else 1 - yes
    except Exception:
        return float("nan")
    return yes if side == "YES" else no

def tag_of(q):
    q = (q or "").lower()
    if re.search(r"bitcoin|btc|ethereum| eth |solana|crypto|dogecoin|xrp", q): return "crypto"
    if re.search(r" vs | vs\.|atp|wta|nba|nfl|nhl|match|roland|stanley|padres|win the ", q): return "sport"
    return "autre"

def theme_key(q):
    """Cle de theme : meme question sans les chiffres -> regroupe les paris correles
       (ex: 'BTC dip to $65k/$60k/$55k' = un seul theme)."""
    q = (q or "").lower()
    q = re.sub(r"[0-9]", "", q)            # enleve les seuils ($65k, $60k...)
    q = re.sub(r"[^a-z ]", " ", q)         # enleve $ , % etc
    return re.sub(r"\s+", " ", q).strip()

def underlying_dir(question, side):
    """(sous-jacent, direction) pour plafonner l'EXPOSITION nette.
       Pour le crypto-prix: asset=BTC/ETH/SOL, direction=UP/DOWN/NEUTRAL selon le sens REEL du pari.
       Sinon: chaque theme est son propre sous-jacent (pas de netting cross-marche)."""
    q = (question or "").lower()
    asset = None
    if re.search(r"bitcoin|btc", q): asset = "BTC"
    elif re.search(r"ethereum|ether| eth ", q): asset = "ETH"
    elif re.search(r"solana| sol ", q): asset = "SOL"
    if asset:
        down = re.search(r"dip|below|under|drop|fall|lower|less than", q)
        up   = re.search(r"reach|above|over|exceed|higher|more than|\bhit\b", q)
        base = "DOWN" if (down and not up) else ("UP" if (up and not down) else None)
        if base is None: return (asset, "NEUTRAL")
        d = base if side == "YES" else ("UP" if base == "DOWN" else "DOWN")
        return (asset, d)
    return (theme_key(question), side)

def market_by_slug(slug):
    try:
        d = curl_json(f"{GAMMA}/markets?slug={slug}")
        return d[0] if d else None
    except Exception:
        return None

CLOB = "https://clob.polymarket.com"
def volatility(m):
    """Volatilite du marche = ecart-type des variations horaires, ramene a l'echelle
       JOURNALIERE (x sqrt(24)). En points de prix absolus. None si pas assez de donnees."""
    try:
        toks = json.loads(m.get("clobTokenIds") or "[]")
        if not toks: return None
        url = f"{CLOB}/prices-history?market={toks[0]}&interval=1w&fidelity=60"  # horaire ~ dernier(s) jour(s)
        out = subprocess.run(["curl","-s",url,"-H","Accept: application/json"],
                             capture_output=True, text=True, timeout=40).stdout
        h = json.loads(out).get("history", [])
        ps = [float(x["p"]) for x in h]
        if len(ps) < 8: return None
        diffs = [abs(ps[i]-ps[i-1]) for i in range(1, len(ps))]
        import statistics as stx
        return stx.pstdev(diffs)                      # sigma a l'echelle HORAIRE (= frequence du tick)
    except Exception:
        return None

def main():
    st = json.load(open(STATE))
    cfg = st["cfg"]
    cash = st["cash"]; opens = st["open"]; closed = st["closed"]
    last_closed = st.get("lastClosed", {}); running = st.get("running", True)
    stats = st.get("stats", {"realizedPnl": 0.0, "nClosed": 0, "nWins": 0})  # cumul (survit au bornage)

    # marche-map depuis le top volume24h
    allm = []
    for off in (0, 100, 200):
        try: allm += curl_json(f"{GAMMA}/markets?closed=false&active=true&order=volume24hr&ascending=false&limit=100&offset={off}")
        except Exception: pass
    mmap = {m.get("slug"): m for m in allm}
    for p in opens:
        if p["slug"] not in mmap:
            mk = market_by_slug(p["slug"])
            if mk: mmap[p["slug"]] = mk

    # ---- 1. EXITS ----
    still = []
    for p in opens:
        m = mmap.get(p["slug"])
        if not m: still.append(p); continue
        cur = side_price(m, p["side"])
        if cur != cur: still.append(p); continue  # NaN
        reason = None
        if cur >= p["tp"]: reason = "TP"
        elif cur <= p["stop"]: reason = "STOP"
        elif m.get("closed") is True: reason = "RESOL"
        elif cfg.get("maxHoldDays", 0) and hours_since(p["openedAt"]) > cfg["maxHoldDays"]*24: reason = "TIME"
        if reason:
            pnl_pct = (cur - p["entry"]) / p["entry"]
            pnl_amt = p["stake"] * pnl_pct
            cash += p["stake"] + pnl_amt
            closed.insert(0, {"slug": p["slug"], "question": p["question"], "tag": p.get("tag"),
                "side": p["side"], "entry": p["entry"], "exit": cur, "pnlPct": pnl_pct,
                "pnlAmt": pnl_amt, "openedAt": p["openedAt"], "closedAt": now_iso(), "reason": reason})
            last_closed[p["slug"]] = now_iso()
            stats["realizedPnl"] += pnl_amt; stats["nClosed"] += 1
            if pnl_amt > 0: stats["nWins"] += 1
        else:
            still.append(p)
    opens = still

    # ---- 2. ENTRIES (momentum, adapte tick horaire) ----
    if running:
        cand = []
        for m in allm:
            try: pr = json.loads(m.get("outcomePrices") or "[]")
            except Exception: continue
            try: yes = float(pr[0])
            except Exception: continue
            day = float(m.get("oneDayPriceChange") or 0)
            hour = float(m.get("oneHourPriceChange") or 0)
            week = float(m.get("oneWeekPriceChange") or 0)
            vol = float(m.get("volume24hr") or 0); liq = float(m.get("liquidity") or 0)
            dl = days_left(m.get("endDate"))
            tag = tag_of(m.get("question"))
            if not (0.06 <= yes <= 0.94): continue
            if vol < 20000 or liq < 20000: continue
            if abs(day) < 0.03: continue
            if dl is None or dl < cfg.get("minDaysLeft", 2): continue   # horaire: pas de marche ultra-court
            if cfg.get("excludeSport", True) and tag == "sport": continue  # horaire: pas de sport live
            fresh = abs(day) / (abs(week) + 0.03)
            accel = 1 if (hour * day > 0 and abs(hour) > 0.003) else 0
            score = abs(day) * min(fresh, 3) * (1 + accel)
            cand.append((score, m, day, tag))
        cand.sort(key=lambda x: -x[0])
        # plafonds anti-correlation : par theme, par categorie, et par EXPOSITION directionnelle nette
        theme_ct = Counter(theme_key(p.get("question")) for p in opens)
        cat_ct = Counter(p.get("tag") for p in opens)
        expo = Counter()
        for p in opens:
            expo[underlying_dir(p.get("question"), p.get("side"))] += p.get("stake", 0)
        max_expo = cfg.get("maxExpoPerDir", 0.10) * cfg["capital0"]
        for score, m, day, tag in cand:
            if len(opens) >= cfg["maxOpen"]: break
            if score < cfg["minScore"]: break
            slug = m.get("slug")
            if any(p["slug"] == slug for p in opens): continue
            if hours_since(last_closed.get(slug)) < cfg["cooldownH"]: continue
            th = theme_key(m.get("question"))
            if theme_ct[th] >= cfg.get("maxPerTheme", 1): continue       # 1 pari max par theme
            if cat_ct[tag] >= cfg.get("maxPerCat", 3): continue          # diversite des categories
            # mode "fade" (mean-reversion, validé en backtest) : on parie sur la CORRECTION
            # du mouvement -> on prend le cote qui vient de baisser. "momentum" = suivre (perdant).
            if cfg.get("mode", "fade") == "fade":
                side = "NO" if day > 0 else "YES"
            else:
                side = "YES" if day > 0 else "NO"
            entry = side_price(m, side)
            if entry != entry or entry < cfg["entryMin"] or entry > cfg["entryMax"]: continue
            stake = cfg["capital0"] * cfg["stakePct"]
            if cash < stake: continue
            ud = underlying_dir(m.get("question"), side)            # (sous-jacent, direction)
            if expo[ud] + stake > max_expo: continue               # plafond d'exposition nette
            # --- stop/TP adaptes a la volatilite du marche ---
            k = cfg.get("kStop", 2.0); R = cfg.get("rTP", 1.5)
            sigma = volatility(m)
            if sigma is not None:
                dist = k * sigma
            else:                                   # fallback si pas d'historique
                dist = entry * cfg["stopPct"]
            dist = max(cfg.get("distMin", 0.03), min(dist, cfg.get("distMax", 0.30)))
            stop = max(entry - dist, 0.02)
            tp = min(entry + R * dist, 0.97)
            opens.append({"slug": slug, "question": m.get("question"), "tag": tag, "side": side,
                "entry": entry, "tp": tp, "stop": stop, "stake": stake,
                "sigma": round(sigma, 4) if sigma is not None else None,
                "dist": round(dist, 4), "theme": th, "und": ud[0], "dir": ud[1],
                "openedAt": now_iso(), "score": score})
            cash -= stake
            theme_ct[th] += 1; cat_ct[tag] += 1; expo[ud] += stake

    stats["realizedPnl"] = round(stats["realizedPnl"], 2)
    stats["winRate"] = round(stats["nWins"]/stats["nClosed"], 3) if stats["nClosed"] else None
    # bornage : on garde les 300 derniers trades dans le fichier (stats cumulees = exactes)
    st.update({"cash": cash, "open": opens, "closed": closed[:300],
               "lastClosed": last_closed, "running": running, "lastTick": now_iso(), "stats": stats})
    json.dump(st, open(STATE, "w"), ensure_ascii=False, indent=2)
    print(f"tick OK @ {st['lastTick']} | cash={cash:.0f} | open={len(opens)} | "
          f"closed_cumul={stats['nClosed']} | realized={stats['realizedPnl']:+.1f}")

if __name__ == "__main__":
    main()
