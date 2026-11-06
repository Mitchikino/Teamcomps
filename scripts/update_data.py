#!/usr/bin/env python3
"""
Hämtar champ-statistik och skriver data.json som index.html läser.

Källor (varje källa misslyckas för sig – resten körs ändå):
  1. Data Dragon (Riot)   – patch + champ-lista (officiellt, stabilt)
  2. Leaguepedia          – pro-picks/bans/WR senaste X dagarna
  3. OP.GG MCP            – soloQ WR/pick/ban per roll + ALLA matchups (WR + games)
  4. Lolalytics           – matchups (WR + games) via sajtens JSON, HTML som reserv

Matchups sparas per champ som  "mu": {"Motståndare": {"o": [wr, games], "l": [wr, games]}}
(o = OP.GG, l = Lolalytics, wr = champens WR mot motståndaren). Bara par med > 20 games sparas.
index.html väger ihop källorna efter antal games.

Kör:  pip install -r requirements.txt && python scripts/update_data.py
Flaggor: --only ddragon,pro,opgg,lolalytics   --pro-days 60   --tier emerald_plus   --delay 1.0
"""
import argparse, datetime as dt, json, os, re, sys, time, unicodedata
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data.json"
UA = {"User-Agent": "teamcomps-personal/1.0 (private team tool)"}
S = requests.Session(); S.headers.update(UA)
LANES = ["top", "jungle", "middle", "bottom", "support"]
MIN_GAMES = 21  # matchup/synergi måste ha MER än 20 games för att sparas

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

def games_of(e):
    for k in ("play", "games", "count", "n"):
        v = e.get(k)
        if isinstance(v, (int, float)): return int(v)
    return None

def opgg(champs, idx, delay):
    m = MCP(); tools = m.start()
    log("OP.GG verktyg:", ", ".join(tools))
    meta_tool, ana_tool, syn_tool = "lol_list_lane_meta_champions", "lol_get_champion_analysis", "lol_get_champion_synergies"
    soloq, mu, synergies, main, roles = {}, {}, {}, {}, {}
    # 1) WR/pick/ban per roll + vilka roller varje champ spelas i
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
                        if rr >= 0.15: roles.setdefault(n, []).append((pos, rr))
                log(f"OP.GG meta {lane}: {n_found} champs")
            except Exception as ex: log("OP.GG meta", lane, "fel:", ex)
            time.sleep(delay)
    # 2) matchups per champ och roll (huvudroll + roller med >= 15 % role rate, max 2)
    ana_err, logged, flips = 0, 0, 0
    for name in champs:
        if name not in main: continue
        plist = [p for p, _ in sorted(roles.get(name, [main[name]]), key=lambda x: -x[1])][:2] or [main[name][0]]
        for pos in plist:
            d = None
            for cname in opgg_names(name, champs[name]["id"]):
                try:
                    d = m.call(ana_tool, {"champion": cname, "position": pos, "game_mode": "ranked", "lang": "en_US"}); break
                except Exception as ex:
                    err = str(ex)
                time.sleep(0.3)
            if d is None:
                ana_err += 1
                if ana_err <= 3: log("OP.GG analysis", name, pos, "fel:", err[:200])
                time.sleep(delay); continue
            def rows(lists):
                out = []
                for lst in lists:
                    for e in lst:
                        nw = entry_name_wr(e, idx)
                        g = games_of(e) if isinstance(e, dict) else None
                        if nw and nw[0] != name and nw[1] is not None and g and g >= MIN_GAMES:
                            out.append((nw[0], nw[1], g))
                return out
            # topp-listor (weak = champs som slår oss, strong = champs vi slår)
            weak, strong = rows(find_lists(d, ["weak_counter"])), rows(find_lists(d, ["strong_counter"]))
            if weak and sum(w for _, w, _ in weak) / len(weak) > 50: weak = [(c, 100 - w, g) for c, w, g in weak]
            if strong and sum(w for _, w, _ in strong) / len(strong) < 50: strong = [(c, 100 - w, g) for c, w, g in strong]
            top = {c: (w, g) for c, w, g in weak + strong}
            # hela listan under rätt roll i data.positions[].counters
            full = []
            for ps in find_lists(d, ["positions"]):
                for p_ in ps:
                    if isinstance(p_, dict) and str(p_.get("name", "")).lower() == pos.lower():
                        full += rows([p_.get("counters") or []])
            # vems WR? jämför mot topp-listorna och vänd hela listan om den är spegelvänd
            same = [abs(w - top[c][0]) for c, w, _ in full if c in top]
            flip = [abs((100 - w) - top[c][0]) for c, w, _ in full if c in top]
            if same and sum(flip) < sum(same):
                full = [(c, 100 - w, g) for c, w, g in full]; flips += 1
            if logged < 2:
                log(f"OP.GG matchups {name} ({pos}): topp {len(top)}, hela listan {len(full)}"); logged += 1
            M = mu.setdefault(name, {})
            for c, w, g in full + [(c, w, g) for c, (w, g) in top.items()]:
                if c not in M or g > M[c][1]: M[c] = [round(w, 2), g]
            time.sleep(delay)
        # synergier (huvudrollen)
        pos = main[name][0]
        if syn_tool in tools:
            try:
                sd = m.call(syn_tool, {"champion": opgg_names(name, champs[name]["id"])[0], "my_position": pos,
                                       "synergy_position": SYN_WITH[pos], "lang": "en_US"})
                lst = [x for l in all_dict_lists(sd) for x in l]
                cs = []
                for e in lst:
                    nw = entry_name_wr(e, idx, ("synergy_champion_name", "synergy_champion_id"))
                    g = games_of(e)
                    if nw and nw[0] != name and nw[1] is not None and g and g >= MIN_GAMES:
                        cs.append({"ch": nw[0], "wr": round(nw[1], 2), "games": g, "with": SYN_WITH[pos], "src": "opgg"})
                if cs: synergies[name] = cs[:12]
            except Exception as ex:
                if len(synergies) == 0: log("OP.GG synergies", name, "fel:", str(ex)[:200])
            time.sleep(delay)
    n_mu = sum(len(v) for v in mu.values())
    log(f"OP.GG: soloQ {len(soloq)} champs, matchups {n_mu} (för {len(mu)} champs), synergier {len(synergies)}, vända listor {flips}, analys-fel {ana_err}")
    if not soloq and not mu: raise RuntimeError("tomt svar – se exempel i loggen")
    return soloq, mu, synergies, {n: v[0] for n, v in main.items()}, {n: [p for p, _ in v] for n, v in roles.items()}

# ---------- 4. Lolalytics ----------
LL_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
         "Accept": "application/json, text/html;q=0.9", "Referer": "https://lolalytics.com/"}
LL_LANE = {"top": "top", "jungle": "jungle", "mid": "middle", "adc": "bottom", "support": "support"}

def lol_slug(name, cid):
    return {"Wukong": "wukong", "Nunu & Willump": "nunu", "Renata Glasc": "renata"}.get(name, cid.lower())

def ll_parse_json(j, idx, me):
    """Generisk tolkning: hitta poster med champ-id + WR + games, var som helst i svaret."""
    out = {}
    def num(v):
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    def walk(o, key_hint=None):
        if isinstance(o, dict):
            name = None
            for k in ("cid", "championId", "champion_id", "champion", "c", "id", "name"):
                if k in o and idx.get(norm(str(o[k]))): name = idx[norm(str(o[k]))]; break
            if not name and key_hint is not None and idx.get(norm(str(key_hint))): name = idx[norm(str(key_hint))]
            wr = None
            for k in ("vsWr", "vs_wr"):  # Lolalytics: vsWr = MOTSTÅNDARENS WR mot champen -> vänd
                if k in o and num(o[k]) is not None: wr = 100 - (o[k] * 100 if o[k] <= 1 else o[k]); break
            if wr is None:
                wr = next((num(o[k]) for k in ("wr", "winRate", "win_rate", "winrate") if k in o and num(o[k]) is not None), None)
            g = next((num(o[k]) for k in ("n", "games", "gamesPlayed", "play", "count") if k in o and num(o[k]) is not None), None)
            if wr is None and g and num(o.get("wins")) is not None: wr = 100 * o["wins"] / g
            if name and name != me and wr is not None and g:
                wr = wr * 100 if wr <= 1 else wr
                if 0 < wr < 100 and g >= MIN_GAMES and (name not in out or g > out[name][1]): out[name] = [round(wr, 2), int(g)]
            for k, v in o.items(): walk(v, k)
        elif isinstance(o, list):
            # [wr, games] eller [games, wins] under en champ-nyckel
            if key_hint is not None and idx.get(norm(str(key_hint))) and 2 <= len(o) <= 8 and all(num(x) is not None for x in o[:2]):
                name = idx[norm(str(key_hint))]; a, b = o[0], o[1]
                wr, g = (a, b) if a <= 100 and b > 100 else ((100 * b / a, a) if a > b and a > 100 else (None, None))
                if name != me and wr and g and g >= MIN_GAMES and 0 < wr < 100: out.setdefault(name, [round(wr, 2), int(g)])
            for v in o: walk(v, key_hint)
    walk(j)
    # Kontroll: "strong"-counters ska slå champen (champens WR < 50). Är det tvärtom är listan spegelvänd.
    strong = []
    for lst in find_lists(j, ["strong"]): strong += [x for x in lst if not isinstance(x, (dict, list))]
    sw = [out[idx[norm(str(c))]][0] for c in strong if idx.get(norm(str(c))) in out]
    if sw and sum(sw) / len(sw) > 50:
        for v in out.values(): v[0] = round(100 - v[0], 2)
    return out

def ll_parse_html(text, idx, me, slug):
    """Reserv: läs rader runt länkar /lol/<slug>/vs/<motståndare>/ – första % = WR, största heltal = games."""
    out = {}
    for mm in re.finditer(rf'/lol/{re.escape(slug)}/vs/([a-z0-9]+)/', text):
        opp = idx.get(norm(mm.group(1)))
        if not opp or opp == me or opp in out: continue
        chunk = re.sub(r"<[^>]+>", " ", text[mm.end():mm.end() + 1200])
        pct = re.search(r"(\d{1,2}(?:\.\d+)?)\s*%", chunk)
        ints = [int(x.replace(",", "").replace("\u00a0", "")) for x in re.findall(r"\b\d{1,3}(?:[,\u00a0]\d{3})+\b|\b\d{2,6}\b", chunk)]
        g = max(ints) if ints else 0
        if pct and g >= MIN_GAMES: out[opp] = [round(float(pct.group(1)), 2), g]
    return out

def lolalytics(champs, idx, plan, tier, delay):
    """plan = {champ: [lanes]} i OP.GG-format (top/jungle/mid/adc/support)."""
    mu, fails, how, shown = {}, 0, {"json": 0, "html": 0}, 0
    for name, lanes in plan.items():
        slug = lol_slug(name, champs[name]["id"])
        for lane in lanes:
            ll = LL_LANE.get(lane, lane); got, err = {}, ""
            for host in ("https://a1.lolalytics.com/mega/", "https://ax.lolalytics.com/mega/"):
                try:
                    r = S.get(host, params={"ep": "counter", "v": "1", "c": slug, "lane": ll, "tier": tier,
                                            "queue": "ranked", "region": "all"}, headers=LL_UA, timeout=30)
                    if r.status_code == 200 and r.text.strip().startswith(("{", "[")):
                        j = r.json()
                        if shown < 1: log("Lolalytics exempel (json):", first_text(j, 500)); shown += 1
                        got = ll_parse_json(j, idx, name)
                        if got: how["json"] += 1; break
                    else: err = f"{host} {r.status_code}"
                except Exception as ex: err = str(ex)[:150]
            if not got:
                try:
                    r = S.get(f"https://lolalytics.com/lol/{slug}/counters/", params={"lane": ll, "tier": tier}, headers=LL_UA, timeout=30)
                    if r.status_code == 200:
                        got = ll_parse_html(r.text, idx, name, slug)
                        if got: how["html"] += 1
                    else: err = f"html {r.status_code}"
                except Exception as ex: err = str(ex)[:150]
            if got:
                M = mu.setdefault(name, {})
                for c, v in got.items():
                    if c not in M or v[1] > M[c][1]: M[c] = v
            else:
                fails += 1
                if fails <= 3: log("Lolalytics", name, lane, "inga matchups:", err)
            time.sleep(delay)
    n_mu = sum(len(v) for v in mu.values())
    log(f"Lolalytics: matchups {n_mu} (för {len(mu)} champs), via json {how['json']}, html {how['html']}, {fails} misslyckade")
    if not mu: raise RuntimeError(f"inga matchups ({fails} fel) – se exempel i loggen")
    return mu

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="ddragon,pro,opgg,lolalytics")
    ap.add_argument("--pro-days", type=int, default=60)
    ap.add_argument("--tier", default="emerald_plus", help="lolalytics-tier, t.ex. emerald_plus, diamond_plus")
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
    # lolalytics hämtas för samma roller som OP.GG hittade (annars förra körningens main_role)
    plan = {}
    for n in champs:
        lanes = (og[4].get(n) if og else None) or ([og[3][n]] if og and n in og[3] else None) or \
                ([old.get("champions", {}).get(n, {}).get("main_role")] if old.get("champions", {}).get(n, {}).get("main_role") else [])
        if lanes: plan[n] = lanes[:2]
    ll = run("lolalytics", lambda: lolalytics(champs, idx, plan, a.tier, a.delay))
    pro = run("pro", lambda: leaguepedia(a.pro_days, idx))

    # Slå ihop. Saknas en källa denna körning behålls förra körningens värden för den källan.
    for n in C:
        prev = old.get("champions", {}).get(n, {})
        C[n]["pro"] = (pro or {}).get(n) if pro is not None else prev.get("pro")
        sq = {f"opgg_{k}": v for k, v in og[0].get(n, {}).items()} if og else \
             {k: v for k, v in (prev.get("soloq") or {}).items() if k.startswith("opgg_")}
        C[n]["soloq"] = sq
        prev_mu = prev.get("mu") or {}
        M = {}
        for key, src in (("o", og[1] if og else None), ("l", ll)):
            if src is not None:
                for c, v in (src.get(n) or {}).items(): M.setdefault(c, {})[key] = v
            else:
                for c, v in prev_mu.items():
                    if key in v: M.setdefault(c, {})[key] = v[key]
        C[n]["mu"] = M
        # counters/beats (äldre format) = sammanvägd WR, viktad efter games
        comb = []
        for c, v in M.items():
            g = sum(x[1] for x in v.values())
            if g: comb.append({"ch": c, "wr": round(sum(x[0] * x[1] for x in v.values()) / g, 2), "games": g, "src": "+".join(sorted(v))})
        C[n]["counters"] = sorted([x for x in comb if x["wr"] < 50], key=lambda x: x["wr"])[:10]
        C[n]["beats"] = sorted([x for x in comb if x["wr"] >= 50], key=lambda x: -x["wr"])[:10]
        C[n]["synergies"] = (og[2].get(n, []) if og else []) or prev.get("synergies", [])
        C[n]["main_role"] = (og[3].get(n) if og else None) or prev.get("main_role")
        C[n] = {k: v for k, v in C[n].items() if v}
    tot = sum(len(c.get("mu", {})) for c in C.values())
    both = sum(1 for c in C.values() for v in c.get("mu", {}).values() if len(v) == 2)
    log(f"Matchups totalt: {tot}, varav {both} med båda källorna")
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    log(f"Skrev {OUT} – patch {out['patch']}, källor: {out['sources']}")

if __name__ == "__main__":
    main()
