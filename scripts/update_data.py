#!/usr/bin/env python3
"""
Hämtar champ-statistik och skriver data.json som index.html läser.

Källor (varje källa misslyckas för sig – resten körs ändå):
  1. Data Dragon (Riot)   – patch + champ-lista (officiellt, stabilt)
  2. Leaguepedia          – pro-picks/bans/WR senaste X dagarna
  3. OP.GG MCP            – soloQ WR/pick/ban per roll + counters
  4. Lolalytics           – soloQ tierlist + counters (scraping, kan gå sönder vid layoutändring)

Kör:  pip install -r requirements.txt && python scripts/update_data.py
Flaggor: --only ddragon,pro,opgg[,lolalytics]   --pro-days 60   --rank emerald_plus
"""
import argparse, datetime as dt, json, os, re, sys, time, unicodedata
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data.json"
UA = {"User-Agent": "teamcomps-personal/1.0 (private team tool)"}
S = requests.Session(); S.headers.update(UA)
LANES = ["top", "jungle", "middle", "bottom", "support"]
MIN_GAMES = 15  # minsta antal matcher för att en counter/synergi ska räknas

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
        for k in (name, c["id"], c["key"]): idx[norm(k)] = name
    idx.update({"monkeyking": "Wukong", "nunu": "Nunu & Willump", "renata": "Renata Glasc", "wukong": "Wukong"})
    return idx

# ---------- 2. Leaguepedia (pro) ----------
def lp_login():
    """Logga in med bot password (Special:BotPasswords) om LP_USER/LP_PASS finns – ger högre gränser."""
    user, pw = os.environ.get("LP_USER"), os.environ.get("LP_PASS")
    if not user or not pw:
        log("Leaguepedia: ingen inloggning (LP_USER/LP_PASS saknas) – kör anonymt"); return False
    api = "https://lol.fandom.com/api.php"
    tok = S.get(api, params={"action": "query", "meta": "tokens", "type": "login", "format": "json"}, timeout=30).json()["query"]["tokens"]["logintoken"]
    r = S.post(api, data={"action": "login", "lgname": user, "lgpassword": pw, "lgtoken": tok, "format": "json"}, timeout=30).json()
    ok = r.get("login", {}).get("result") == "Success"
    log("Leaguepedia: inloggning", "lyckades" if ok else f"misslyckades: {r.get('login')}")
    return ok

def leaguepedia(days, idx):
    lp_login()
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    stats, games, off, tries = {}, 0, 0, 0
    while True:
        r = S.get("https://lol.fandom.com/api.php", params={
            "action": "cargoquery", "format": "json", "tables": "ScoreboardGames",
            "fields": "Team1,Team2,WinTeam,Team1Picks,Team2Picks,Team1Bans,Team2Bans,Patch",
            "where": f'DateTime_UTC >= "{since} 00:00:00"', "limit": 500, "offset": off}, timeout=30)
        j = r.json() if r.headers.get("Content-Type", "").startswith("application/json") else {}
        if "error" in j or r.status_code != 200:
            err = str(j.get("error") or r.text[:300])
            if "ratelimit" in err.lower() and tries < 3:
                tries += 1; wait = 45 * tries
                log(f"Leaguepedia: rate limit, väntar {wait} s (försök {tries}/3)")
                time.sleep(wait); continue
            log("Leaguepedia ger upp:", r.status_code, err[:200])
            break
        tries = 0
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

# ---------- OP.GG:s textformat ----------
# Svaren ser ut som:
#   class Top: champion,is_rip,play,win,...
#   LolListLaneMetaChampions("en_US","top",Data(Positions([Top("Malphite",false,840,...), ...])))
# parse_opgg() gör om det till vanliga dicts/listor.
def parse_opgg(text):
    schemas, body = {}, []
    for line in text.split("\n"):
        m_ = re.match(r"\s*class\s+(\w+)\s*:\s*(.*)$", line)
        if m_:
            schemas.setdefault(m_.group(1), [f.strip() for f in m_.group(2).split(",") if f.strip()])
        else:
            body.append(line)
    src = "\n".join(body).strip()
    pos = [0]
    def ws():
        while pos[0] < len(src) and src[pos[0]] in " \t\r\n": pos[0] += 1
    def value():
        ws(); c = src[pos[0]]
        if c == '"':
            j = pos[0] + 1
            while src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            s_ = json.loads(src[pos[0]:j + 1]); pos[0] = j + 1; return s_
        if c == "[":
            pos[0] += 1; out = seq("]"); return out
        if c == "{":
            pos[0] += 1; out = {}
            ws()
            while src[pos[0]] != "}":
                k = value(); ws(); pos[0] += 1  # ':'
                out[str(k)] = value(); ws()
                if src[pos[0]] == ",": pos[0] += 1; ws()
            pos[0] += 1; return out
        m_ = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?").match(src, pos[0])
        if m_:
            pos[0] = m_.end(); t = m_.group(0)
            return float(t) if any(x in t for x in ".eE") else int(t)
        m_ = re.compile(r"[A-Za-z_]\w*").match(src, pos[0])
        if m_:
            name = m_.group(0); pos[0] = m_.end(); ws()
            if pos[0] < len(src) and src[pos[0]] == "(":
                pos[0] += 1; args = seq(")")
                fields = schemas.get(name, [])
                d = {f: args[i] for i, f in enumerate(fields) if i < len(args)}
                for i in range(len(fields), len(args)): d[f"_{i}"] = args[i]
                d["_type"] = name
                return d
            return {"true": True, "false": False, "null": None, "None": None, "True": True, "False": False}.get(name, name)
        raise ValueError(f"oväntat tecken {c!r} vid {pos[0]}")
    def seq(end):
        out = []; ws()
        while pos[0] < len(src) and src[pos[0]] != end:
            out.append(value()); ws()
            if pos[0] < len(src) and src[pos[0]] == ",": pos[0] += 1; ws()
        pos[0] += 1
        return out
    return value() if src else {}

def all_dict_lists(obj):
    """Alla listor av dicts, var som helst i strukturen."""
    out = []
    if isinstance(obj, dict):
        for v in obj.values(): out += all_dict_lists(v)
    elif isinstance(obj, list):
        if obj and isinstance(obj[0], dict): out.append(obj)
        for v in obj: out += all_dict_lists(v)
    return out

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
        if (res or {}).get("isError"):
            raise RuntimeError(" ".join(c.get("text", "") for c in res.get("content", []))[:300])
        for c in (res or {}).get("content", []):
            t = c.get("text", "")
            try: out.append(json.loads(t)); continue
            except Exception: pass
            try: out.append(parse_opgg(t))
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

def entry_name_wr(e, idx, keys=None):
    if not isinstance(e, dict): return None
    name = None
    for k in (keys or ("champion_name", "championName", "name", "champion", "champion_id", "championId", "key")):
        v = e.get(k)
        if isinstance(v, dict): v = v.get("name")
        if v is not None and idx.get(norm(str(v))): name = idx[norm(str(v))]; break
    wr = None
    for k in ("win_rate", "winRate", "winrate", "wr"):
        if e.get(k) is not None:
            wr = float(e[k]); wr = wr * 100 if wr <= 1 else wr; break
    if wr is None and isinstance(e.get("win"), (int, float)) and isinstance(e.get("play"), (int, float)) and e["play"] > 0:
        if e["play"] < MIN_GAMES: return None  # för få matcher = brus
        wr = 100.0 * e["win"] / e["play"]
    return (name, wr) if name else None

def first_text(d, n=600):
    t = d if isinstance(d, str) else json.dumps(d, ensure_ascii=False)
    return t[:n].replace("\n", " ")

OP_LANE = {"top": "top", "jungle": "jungle", "middle": "mid", "bottom": "adc", "support": "support"}
# vilken lagkamrat vi vill ha synergier för, per roll
SYN_WITH = {"adc": "support", "support": "adc", "jungle": "mid", "mid": "jungle", "top": "jungle"}

def opgg_names(name, cid):
    """OP.GG vill ha UPPER_SNAKE_CASE – prova några varianter."""
    a = re.sub(r"[^A-Za-z0-9 ]", "", name).strip().upper().replace(" ", "_")
    b = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", cid).upper()
    c = cid.upper()
    special = {"Nunu & Willump": ["NUNU", "NUNU_WILLUMP"], "Wukong": ["WUKONG", "MONKEY_KING"], "Renata Glasc": ["RENATA", "RENATA_GLASC"]}
    out = special.get(name, []) + [a, b, c]
    return list(dict.fromkeys(out))

def opgg(champs, idx, delay):
    m = MCP(); tools = m.start()
    log("OP.GG verktyg:", ", ".join(tools))
    meta_tool, ana_tool, syn_tool = "lol_list_lane_meta_champions", "lol_get_champion_analysis", "lol_get_champion_synergies"
    soloq, counters, beats, synergies, main = {}, {}, {}, {}, {}
    # 1) WR/pick/ban per roll + huvudroll per champ
    if meta_tool in tools:
        for lane in LANES:
            pos = OP_LANE[lane]
            try:
                d = m.call(meta_tool, {"position": pos, "lang": "en_US"})
                n_found = 0
                for lst in all_dict_lists(d):
                    for e in lst:
                        n = idx.get(norm(str(e.get("champion", ""))))
                        if not n: continue
                        n_found += 1
                        wr = e.get("win_rate"); wr = wr * 100 if isinstance(wr, (int, float)) and wr <= 1 else wr
                        rr = e.get("role_rate") or 0
                        soloq.setdefault(n, {})[lane] = {"wr": wr, "pick": e.get("pick_rate"), "ban": e.get("ban_rate"),
                                                         "games": e.get("play"), "role_rate": rr, "tier": e.get("tier")}
                        if rr > main.get(n, ("", -1))[1]: main[n] = (pos, rr)
                log(f"OP.GG meta {lane}: {n_found} champs")
            except Exception as ex: log("OP.GG meta", lane, "fel:", ex)
            time.sleep(delay)
    # 2) counters + synergier per champ i dess huvudroll
    first, ana_err, logged_c = True, 0, False
    for name in champs:
        if name not in main: continue
        pos = main[name][0]
        d = None
        for cname in opgg_names(name, champs[name]["id"]):
            try:
                d = m.call(ana_tool, {"champion": cname, "position": pos, "game_mode": "ranked", "lang": "en_US"}); break
            except Exception as ex:
                err = str(ex)
            time.sleep(0.3)
        if d is None:
            ana_err += 1
            if ana_err <= 3: log("OP.GG analysis", name, "fel:", err[:200])
            time.sleep(delay); continue
        if first:
            log("OP.GG exempel (analysis):", json.dumps(d, ensure_ascii=False)[:700]); first = False
        def grab(keys):
            out = []
            for lst in find_lists(d, keys):
                for e in lst:
                    nw = entry_name_wr(e, idx)
                    if nw and nw[0] != name and nw[1] is not None:
                        out.append({"ch": nw[0], "wr": round(nw[1], 2), "games": e.get("play"), "src": "opgg"})
            return out
        weak, strong = grab(["weak_counter"]), grab(["strong_counter"])
        if not weak and not strong:
            # reserv: counters-listan under rätt roll i data.positions
            allc = []
            for ps in find_lists(d, ["positions"]):
                for p_ in ps:
                    if isinstance(p_, dict) and str(p_.get("name", "")).lower() in (pos, pos.upper().lower()):
                        for e in (p_.get("counters") or []):
                            nw = entry_name_wr(e, idx)
                            if nw and nw[0] != name and nw[1] is not None:
                                allc.append({"ch": nw[0], "wr": round(nw[1], 2), "games": e.get("play"), "src": "opgg"})
            weak = sorted([x for x in allc if x["wr"] < 50], key=lambda x: x["wr"])
            strong = sorted([x for x in allc if x["wr"] > 50], key=lambda x: -x["wr"])
        if not logged_c:
            for lst in find_lists(d, ["counter"]):
                if lst: log("OP.GG exempel (counter-rad):", json.dumps(lst[0], ensure_ascii=False)[:300]); logged_c = True; break
        # "counters" = champs som slår denna champ, wr = denna champs WR mot dem (lågt = stark counter)
        if weak:
            if sum(x["wr"] for x in weak) / len(weak) > 50:  # svaret anger motståndarens WR – vänd
                for x in weak: x["wr"] = round(100 - x["wr"], 2)
            counters[name] = weak[:10]
        if strong: beats[name] = strong[:10]
        time.sleep(delay)
        # synergier
        if syn_tool in tools:
            try:
                sd = m.call(syn_tool, {"champion": opgg_names(name, champs[name]["id"])[0], "my_position": pos,
                                       "synergy_position": SYN_WITH[pos], "lang": "en_US"})
                lst = [x for l in all_dict_lists(sd) for x in l]
                cs = [(nw, e.get("play")) for e in lst if (nw := entry_name_wr(e, idx, ("synergy_champion_name", "synergy_champion_id")))]
                cs = [{"ch": c, "wr": round(w, 2), "games": g, "with": SYN_WITH[pos], "src": "opgg"} for (c, w), g in cs if c != name and w is not None]
                if cs: synergies[name] = cs[:12]
                if name == next(iter(main)): log("OP.GG exempel (synergies):", json.dumps(sd, ensure_ascii=False)[:500])
            except Exception as ex:
                if len(synergies) == 0: log("OP.GG synergies", name, "fel:", str(ex)[:200])
            time.sleep(delay)
    log(f"OP.GG: soloQ {len(soloq)} champs, counters {len(counters)}, slår {len(beats)}, synergier {len(synergies)}, analys-fel {ana_err}")
    if not soloq and not counters: raise RuntimeError("tomt svar – se exempel i loggen")
    return soloq, counters, synergies, beats, {n: v[0] for n, v in main.items()}

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
    ap.add_argument("--only", default="ddragon,pro,opgg", help="lägg till ,lolalytics för att även skrapa lolalytics")
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
            out["sources"][key] = "av"; return None
        try:
            r = fn(); out["sources"][key] = "ok"; return r
        except Exception as ex:
            log(f"{key} misslyckades: {ex}"); out["sources"][key] = f"fel: {ex}"[:200]; return None

    og = run("opgg", lambda: opgg(champs, idx, a.delay))
    ll = run("lolalytics", lambda: lolalytics(champs, idx, a.rank, a.delay))
    pro = run("pro", lambda: leaguepedia(a.pro_days, idx))

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
        C[n]["synergies"] = (og[2].get(n, []) if og else []) or prev.get("synergies", [])
        C[n]["beats"] = (og[3].get(n, []) if og else []) or prev.get("beats", [])
        C[n]["main_role"] = (og[4].get(n) if og else None) or prev.get("main_role")
        C[n] = {k: v for k, v in C[n].items() if v}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    log(f"Skrev {OUT} – patch {out['patch']}, källor: {out['sources']}")

if __name__ == "__main__":
    main()
