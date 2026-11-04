#!/usr/bin/env python3
"""
Hämtar champ-statistik och skriver data.json som index.html läser.

Källor (varje källa misslyckas för sig – resten körs ändå):
  1. Data Dragon (Riot)   – patch + champ-lista (officiellt, stabilt)
  2. Leaguepedia          – pro-picks/bans/WR senaste X dagarna
  3. OP.GG MCP            – soloQ WR/pick/ban per roll + counters
  4. Lolalytics           – soloQ tierlist + counters (scraping, kan gå sönder vid layoutändring)

Kör:  pip install -r requirements.txt && python scripts/update_data.py
Flaggor: --only ddragon,pro,opgg,lolalytics   --pro-days 60   --rank emerald_plus
"""
import argparse, datetime as dt, json, re, sys, time, unicodedata
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data.json"
UA = {"User-Agent": "teamcomps-personal/1.0 (private team tool)"}
S = requests.Session(); S.headers.update(UA)
LANES = ["top", "jungle", "middle", "bottom", "support"]

def log(*a): print("[data]", *a, flush=True)
def norm(s): return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFD", s.lower()))

# ---------- 1. Data Dragon ----------
def ddragon():
    ver = S.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=20).json()[0]
    champs = S.get(f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/en_US/champion.json", timeout=20).json()["data"]
    # name = visningsnamn ("Kog'Maw"), id = intern nyckel ("KogMaw")
    return ver, {c["name"]: {"id": c["id"], "key": c["key"]} for c in champs.values()}

def name_index(champs):
    idx = {}
    for name, c in champs.items():
        for k in (name, c["id"]): idx[norm(k)] = name
    idx.update({"monkeyking": "Wukong", "nunu": "Nunu & Willump", "renata": "Renata Glasc", "wukong": "Wukong"})
    return idx

# ---------- 2. Leaguepedia (pro) ----------
def leaguepedia(days, idx):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    stats, games, off = {}, 0, 0
    while True:
        r = S.get("https://lol.fandom.com/api.php", params={
            "action": "cargoquery", "format": "json", "tables": "ScoreboardGames",
            "fields": "Team1,Team2,WinTeam,Team1Picks,Team2Picks,Team1Bans,Team2Bans,Patch",
            "where": f'DateTime_UTC >= "{since} 00:00:00"', "limit": 500, "offset": off}, timeout=30)
        j = r.json() if r.headers.get("Content-Type", "").startswith("application/json") else {}
        if "error" in j or r.status_code != 200:
            log("Leaguepedia svar:", r.status_code, str(j.get("error") or r.text[:300]))
            if off == 0 and "ratelimit" in str(j).lower():
                time.sleep(30); continue
            break
        rows = j.get("cargoquery", [])
        if not rows: break
        for row in rows:
            g = row["title"]; games += 1
            for side in ("1", "2"):
                won = g.get("WinTeam") == g.get("Team" + side)
                for c in (g.get(f"Team{side}Picks") or "").split(","):
                    n = idx.get(norm(c))
                    if not n: continue
                    s = stats.setdefault(n, {"picks": 0, "bans": 0, "wins": 0})
                    s["picks"] += 1; s["wins"] += int(won)
                for c in (g.get(f"Team{side}Bans") or "").split(","):
                    n = idx.get(norm(c))
                    if n: stats.setdefault(n, {"picks": 0, "bans": 0, "wins": 0})["bans"] += 1
        off += len(rows)
        if len(rows) < 500: break
        time.sleep(2)
    for s in stats.values():
        s["presence"] = round((s["picks"] + s["bans"]) / max(games, 1), 4)
        s["wr"] = round(s["wins"] / s["picks"], 4) if s["picks"] else None
        s["games"] = games
    log(f"Leaguepedia: {games} pro-matcher sedan {since}")
    if games == 0: raise RuntimeError("0 matcher – se 'Leaguepedia svar' i loggen")
    return stats

# ---------- 3. OP.GG MCP ----------
class MCP:
    URL = "https://mcp-api.op.gg/mcp"
    def __init__(self):
        self.sid, self.i = None, 0
    def _post(self, payload):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.sid: h["Mcp-Session-Id"] = self.sid
        r = S.post(self.URL, json=payload, headers=h, timeout=60)
        self.sid = r.headers.get("Mcp-Session-Id", self.sid)
        if not r.text.strip(): return None
        if "text/event-stream" in r.headers.get("Content-Type", ""):
            data = [l[5:].strip() for l in r.text.splitlines() if l.startswith("data:")]
            return json.loads(data[-1]) if data else None
        return r.json()
    def req(self, method, params=None):
        self.i += 1
        res = self._post({"jsonrpc": "2.0", "id": self.i, "method": method, "params": params or {}})
        if res and "error" in res: raise RuntimeError(res["error"])
        return (res or {}).get("result")
    def start(self):
        self.req("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "teamcomps", "version": "1"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return {t["name"]: t for t in self.req("tools/list")["tools"]}
    def call(self, name, args):
        res = self.req("tools/call", {"name": name, "arguments": args})
        out = []
        for c in (res or {}).get("content", []):
            t = c.get("text", "")
            try: out.append(json.loads(t))
            except Exception: out.append(t)
        return out[0] if len(out) == 1 else out

def fill_args(schema, champ=None, lane=None):
    """Fyll verktygets argument utifrån dess schema (enum-värden matchas mot champ/roll)."""
    props, req = schema.get("properties", {}), schema.get("required", [])
    args = {}
    for k, p in props.items():
        enum, lk = p.get("enum") or [], k.lower()
        def pick(target):
            for e in enum:
                if norm(str(e)) == norm(target): return e
            return None
        if "champion" in lk and champ:
            args[k] = pick(champ) or champ.upper().replace(" ", "_").replace("'", "").replace(".", "")
        elif any(x in lk for x in ("position", "lane", "role")) and lane:
            alias = {"middle": ["mid", "middle"], "bottom": ["adc", "bottom", "bot"], "support": ["support", "sup"], "jungle": ["jungle"], "top": ["top"]}[lane]
            args[k] = next((x for a in alias if (x := pick(a))), alias[0].upper())
        elif "mode" in lk:
            args[k] = pick("ranked") or (enum[0] if enum else "RANKED")
        elif "region" in lk:
            args[k] = pick("global") or pick("euw") or (enum[0] if enum else "global")
        elif "tier" in lk:
            args[k] = pick("emerald_plus") or pick("platinum_plus") or (enum[0] if enum else "emerald_plus")
        elif "lang" in lk or "locale" in lk:
            args[k] = pick("en_US") or "en_US"
        elif k in req and enum:
            args[k] = enum[0]
    return args

def find_lists(obj, keys):
    """Leta rekursivt efter listor under nycklar som innehåller något av keys."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if any(x in k.lower() for x in keys) and isinstance(v, list): hits.append(v)
            hits += find_lists(v, keys)
    elif isinstance(obj, list):
        for v in obj: hits += find_lists(v, keys)
    return hits

def entry_name_wr(e, idx):
    if not isinstance(e, dict): return None
    name = None
    for k in ("champion_name", "championName", "name", "champion", "champion_id", "championId"):
        v = e.get(k)
        if isinstance(v, dict): v = v.get("name")
        if v is not None and idx.get(norm(str(v))): name = idx[norm(str(v))]; break
    wr = None
    for k in ("win_rate", "winRate", "winrate", "wr"):
        if e.get(k) is not None:
            wr = float(e[k]); wr = wr * 100 if wr <= 1 else wr; break
    return (name, wr) if name else None

def first_text(d, n=600):
    t = d if isinstance(d, str) else json.dumps(d, ensure_ascii=False)
    return t[:n].replace("\n", " ")

def opgg(champs, idx, delay):
    m = MCP(); tools = m.start()
    log("OP.GG verktyg:", ", ".join(tools))
    meta_tool = next((t for t in tools if t == "lol_list_lane_meta_champions"), None) or next((t for t in tools if "lane_meta" in t), None)
    ana_tool = next((t for t in tools if t == "lol_get_champion_analysis"), None) or next((t for t in tools if "champion_analysis" in t or "champion-analysis" in t), None)
    for t in (meta_tool, ana_tool):
        if t: log(f"OP.GG schema {t}:", json.dumps(tools[t].get("inputSchema", {}), ensure_ascii=False)[:800])
    soloq, counters = {}, {}
    if meta_tool:
        for k, lane in enumerate(LANES):
            try:
                d = m.call(meta_tool, fill_args(tools[meta_tool]["inputSchema"], lane=lane))
                if k == 0: log("OP.GG exempel-svar (meta):", first_text(d))
                found = 0
                for lst in find_lists(d, ["champion", "data", "position", "lane", "list"]):
                    for e in lst:
                        nw = entry_name_wr(e, idx)
                        if nw:
                            found += 1
                            soloq.setdefault(nw[0], {})[lane] = {"wr": nw[1],
                                "pick": e.get("pick_rate") or e.get("pickRate"), "ban": e.get("ban_rate") or e.get("banRate")}
                if found == 0 and isinstance(d, str): soloq_text(d, lane, soloq, idx)
            except Exception as ex: log("OP.GG meta", lane, "fel:", ex)
            time.sleep(delay)
    if ana_tool:
        for i, name in enumerate(champs):
            try:
                d = m.call(ana_tool, fill_args(tools[ana_tool]["inputSchema"], champ=champs[name]["id"], lane=None))
                if i == 0: log("OP.GG exempel-svar (analysis):", first_text(d))
                lst = [x for l in find_lists(d, ["weakcounter", "weak_counter", "counter"]) for x in l]
                cs = [nw for e in lst if (nw := entry_name_wr(e, idx))]
                if cs: counters[name] = [{"ch": c, "wr": w, "src": "opgg"} for c, w in cs if c != name and w is not None][:10]
            except Exception as ex:
                if i < 3: log("OP.GG analysis", name, "fel:", ex)
            time.sleep(delay)
    log(f"OP.GG: soloQ för {len(soloq)} champs, counters för {len(counters)}")
    if not soloq and not counters: raise RuntimeError("tomt svar – se 'exempel-svar' i loggen")
    return soloq, counters

def soloq_text(text, lane, soloq, idx):
    """Reservtolkning om OP.GG svarar med text: rad med champnamn + första procenttal = WR."""
    names = sorted(set(idx.values()), key=len, reverse=True)
    for line in text.splitlines():
        pct = re.search(r"(\d{1,2}\.\d+)\s*%", line)
        if not pct: continue
        low = line.lower()
        n = next((x for x in names if x.lower() in low), None)
        if n and lane not in soloq.get(n, {}): soloq.setdefault(n, {})[lane] = {"wr": float(pct.group(1))}

# ---------- 4. Lolalytics (scraping) ----------
def lol_slug(name, cid):
    return {"Wukong": "wukong", "Nunu & Willump": "nunu", "Renata Glasc": "renata"}.get(name, cid.lower())

def lolalytics(champs, idx, rank, delay):
    try:
        import lolalytics_api as la
    except ImportError:
        log("Lolalytics: paketet lolalytics-api saknas (pip install lolalytics-api)"); return {}, {}
    tiers, counters = {}, {}
    for lane in ["top", "jungle", "mid", "adc", "support"]:
        try:
            d = json.loads(la.get_tierlist(n=60, lane=lane, rank=rank))
            for e in d.values():
                n = idx.get(norm(e["champion"]))
                if n: tiers.setdefault(n, {})[lane] = {"tier": e["tier"], "wr": float(e["winrate"].strip("%") or 0)}
        except Exception as ex: log("Lolalytics tierlist", lane, "fel:", ex)
        time.sleep(delay)
    fails = 0
    for name, c in champs.items():
        try:
            d = json.loads(la.get_counters(n=8, champion=lol_slug(name, c["id"]), rank=rank))
            # winrate = CHAMPENS winrate mot countern (lågt = stark counter)
            cs = []
            for e in d.values():
                n = idx.get(norm(e["champion"]))
                if n: cs.append({"ch": n, "wr": float(e["winrate"]), "src": "lolalytics"})
            if cs: counters[name] = cs
        except Exception as ex:
            fails += 1
            if fails <= 3: log("Lolalytics counters", name, "fel:", ex)
        time.sleep(delay)
    log(f"Lolalytics: tierlist för {len(tiers)} champs, counters för {len(counters)} ({fails} fel)")
    if not tiers and not counters: raise RuntimeError(f"tomt svar ({fails} fel)")
    return tiers, counters

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="ddragon,pro,opgg,lolalytics")
    ap.add_argument("--pro-days", type=int, default=60)
    ap.add_argument("--rank", default="", help="lolalytics-rank, t.ex. diamond_plus. Tomt = sajtens standard")
    ap.add_argument("--delay", type=float, default=1.0, help="sekunder mellan anrop (var snäll mot sajterna)")
    a = ap.parse_args(); only = set(a.only.split(","))
    old = json.loads(OUT.read_text()) if OUT.exists() else {}
    out = {"updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "sources": {}, "champions": {}}
    ver, champs = ddragon()
    idx = name_index(champs)
    mj, mn = (ver.split(".") + ["0"])[:2]
    out["patch"] = f"{int(mj) + 10}.{mn}" if mj.isdigit() and int(mj) < 20 else ver
    out["ddragon"] = ver; out["sources"]["ddragon"] = "ok"
    C = out["champions"]
    for n in champs: C[n] = {"id": champs[n]["id"]}

    def run(key, fn):
        if key not in only:
            out["sources"][key] = old.get("sources", {}).get(key, "skipped"); return None
        try:
            r = fn(); out["sources"][key] = "ok"; return r
        except Exception as ex:
            log(f"{key} misslyckades: {ex}"); out["sources"][key] = f"fel: {ex}"[:200]; return None

    pro = run("pro", lambda: leaguepedia(a.pro_days, idx))
    og = run("opgg", lambda: opgg(champs, idx, a.delay))
    ll = run("lolalytics", lambda: lolalytics(champs, idx, a.rank, a.delay))

    # Slå ihop. Saknas en källa denna körning behålls förra körningens värden.
    for n in C:
        prev = old.get("champions", {}).get(n, {})
        C[n]["pro"] = (pro or {}).get(n) if pro is not None else prev.get("pro")
        sq = {}
        if og: sq.update({f"opgg_{k}": v for k, v in og[0].get(n, {}).items()})
        if ll: sq.update({f"lolalytics_{k}": v for k, v in ll[0].get(n, {}).items()})
        C[n]["soloq"] = sq or prev.get("soloq")
        cs = (og[1].get(n, []) if og else []) + (ll[1].get(n, []) if ll else [])
        C[n]["counters"] = cs or prev.get("counters", [])
        C[n] = {k: v for k, v in C[n].items() if v}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    log(f"Skrev {OUT} – patch {out['patch']}, källor: {out['sources']}")

if __name__ == "__main__":
    main()
