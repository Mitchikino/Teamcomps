# Teamcomps – draft-verktyg

`index.html` är draft-sidan. Den läser `data.json` om filen finns och använder då riktig statistik (pro-presence, soloQ-winrate, counters). Saknas filen kör sidan på sin inbyggda data.

## Kom igång (GitHub Pages, gratis)

1. Skapa ett nytt repo på GitHub och ladda upp allt i den här mappen (även den dolda mappen `.github`).
2. **Settings → Pages:** välj *Deploy from a branch*, branch `main`, mapp `/ (root)`.
3. **Settings → Actions → General → Workflow permissions:** välj *Read and write permissions*.
4. **Actions-fliken → "Uppdatera champ-data" → Run workflow.** Första körningen tar en stund (ett anrop per champ och källa, med 1 s paus).
5. Sidan finns sedan på `https://<ditt-namn>.github.io/<repo>/`.

Datan uppdateras automatiskt varje dag 05:00 UTC. För en gång i veckan, ändra raden `cron` i `.github/workflows/update-data.yml` till `"0 5 * * 3"`.

> Ett publikt repo gör sidan publik för alla som har länken. Vill ni hålla den inom laget: kör lokalt (nedan) och dela filerna, eller lägg den bakom lösenord på t.ex. Netlify.

## Köra lokalt

```bash
pip install -r requirements.txt
python scripts/update_data.py                 # alla källor
python scripts/update_data.py --only ddragon,pro   # bara snabba källor
python -m http.server 8000                    # öppna http://localhost:8000
```

Sidan måste öppnas via en server (inte genom att dubbelklicka på filen), annars får den inte läsa `data.json`.

## Källor

| Källa | Vad | Stabilitet |
| --- | --- | --- |
| Data Dragon (Riot) | Patch, champ-namn | Stabil |
| Leaguepedia | Pro-picks/bans/WR senaste 60 dagarna (`--pro-days`) | Stabil |
| OP.GG MCP | SoloQ WR/pick/ban per roll, counters | Skriptet läser verktygens schema själv; kan behöva justeras om OP.GG ändrar format |
| Lolalytics | SoloQ tierlist, counters | Scraping – går sönder när sajten ändrar layout |

Varje källa körs för sig. Om en misslyckas behålls förra körningens data för den källan, och statusen syns längst ner på sidan (✓ eller felmeddelande).

## Om något går sönder

- **Lolalytics ger fel:** paketet `lolalytics-api` läser sidans HTML med fasta sökvägar. Uppdatera paketet (`pip install -U lolalytics-api`) eller kör utan: `--only ddragon,pro,opgg`.
- **OP.GG ger tomma counters:** kör skriptet lokalt och titta på raden `OP.GG verktyg: …` i loggen. Verktygs- eller fältnamn kan ha ändrats; justera `pos_tool`/`ana_tool` och `find_lists` i `scripts/update_data.py`.
- **Ändra rank-filter:** `--rank diamond_plus` (gäller lolalytics).

## Hur sidan använder datan

- **Tier/presence:** pro-presence ≥ 45 % = S, ≥ 20 % = A, ≥ 7 % = B. Ersätter den inbyggda tier-listan.
- **Counters:** om er champ har bättre än 50 % mot en fiendepick visas ”Statistisk counter: X 54,5 %” och förslaget går upp. Om fiendens pick har bättre WR mot er champ visas ”Svag mot X”.
- **Ban-tips:** champs som statistiskt slår era picks (eller er bästa comp) föreslås som bans, t.ex. ”Kontrar er Orianna (54,5 %)”.
