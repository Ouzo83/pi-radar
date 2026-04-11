# PIRadar — Technická dokumentace
> Verze: 1.0 | Datum: duben 2026 | Autor: Michal Janíček (@Ouzo83)

---

## 1. Co to je a k čemu to slouží

**PIRadar** je automatický dip screener a investiční dashboard postavený na portfoliích vybraných eToro Popular Investors (PI).

### Účel
Každý den ráno zobrazuje které akcie z portfolií 7 prověřených PI investorů výrazně klesly na ceně nebo jsou fundamentálně podhodnocené. Výstupem je interaktivní webový dashboard s Claude AI analýzou a konkrétními vstupními/výstupními cenovými pásmy.

### Kontext
Majitel (@Ouzo83) je sám Popular Investor na eToro. PIRadar mu slouží jako **první filtr** při hledání investičních příležitostí — ušetří čas oproti manuálnímu sledování desítek tickerů. Není to automatický trading systém, výstup je podklad pro vlastní rozhodnutí.

### Kdo to používá
Primárně @Ouzo83 osobně. Potenciálně sdílitelné s copiers jako ukázka analytické práce.

---

## 2. Architektura

### Datový tok

```
ticker_collector.py
        │
        │  BullAware API (7× portfolio endpoint)
        │
        ▼
   watchlist.json          ← unikátní tickery ze všech PI portfolií
        │
        ▼
  dip_screener.py
        │
        ├── Finnhub API    → cena, fundamenty, analyst rec, company profile
        ├── yfinance       → fallback pro mezinárodní tickery (.L, .DE, .HK...)
        ├── Claude API     → narativní analýza shortlistu + entry/exit ceny
        │
        ├── [--alert]      → Gmail SMTP alert při pohybu > ±10%
        │
        ▼
  dashboard/data.json      ← strukturovaná data pro frontend
        │
        ▼
  dashboard/index.html     ← interaktivní Netlify dashboard
```

### Dva provozní módy

| Mód | Přepínač | Kdy | Co dělá |
|---|---|---|---|
| Alert check | `--alert` | každých 30 min (market hours) | Jen ceny, email při >±10% |
| Full report | *(bez přepínače)* | denně 21:00 UTC | Plná analýza + Claude + data.json |

### Conviction systém
Každý ticker dostane skóre conviction podle toho, kolik PI investorů ho aktuálně drží:
- **7/7 PI** = conviction 100% – nejvyšší priorita
- **Score 0–100** kombinuje: conviction (max 40b) + margin of safety (max 25b) + denní pokles (max 20b) + analyst sentiment (max 15b)

---

## 3. Soubory a komponenty

### Adresářová struktura
```
C:\Users\Michal\Mike\eToro\eToro_report\pi_radar\
│
├── ticker_collector.py     ← krok 1: sběr tickerů z PI portfolií
├── dip_screener.py         ← krok 2: market data + analýza + alerty
├── watchlist.json          ← generovaný: seznam tickerů (vstup pro screener)
├── alert_state.json        ← generovaný: paměť odeslaných alertů (1× denně/ticker)
│
└── dashboard/
    ├── index.html          ← frontend dashboard (Netlify)
    └── data.json           ← generovaný: denní data pro dashboard
```

### `ticker_collector.py`
Stáhne portfolia 7 PI investorů přes BullAware API a vytvoří union watchlistu.

**Klíčové části:**
- `PI_USERNAMES` — seznam sledovaných PI usernames (editovat pro přidání/odebrání PI)
- `BLACKLIST` — set tickerů co se nikdy nezahrnují (ruské sankcionované akcie, delistované, duplikáty)
- `fetch_portfolio(username)` — stáhne pozice jednoho PI přes BullAware
- `collect()` — hlavní funkce, agreguje portfolia, počítá conviction, uloží `watchlist.json`

**Výstup `watchlist.json`:**
```json
{
  "generated_at": "2026-04-11T17:26:33",
  "pi_investors": ["Ouzo83", "smudliczek", ...],
  "total_tickers": 161,
  "watchlist": [
    {
      "symbol": "NVDA",
      "investor_count": 5,
      "investors": ["Ouzo83", "smudliczek", ...],
      "avg_weight_pct": 3.79,
      "direction": "long",
      "avg_profit_pct": 166.45,
      "conviction": 71
    }
  ]
}
```

---

### `dip_screener.py`
Hlavní skript. Stahuje tržní data, detekuje dipy, volá Claude, posílá alerty, ukládá dashboard data.

**Klíčové konstanty (nahoře souboru):**
```python
ALERT_DIP_PCT    = -10.0   # pokles > 10% → email alert
ALERT_SURGE_PCT  =  10.0   # nárůst > 10% → email alert
SCREENER_DIP_PCT =  -5.0   # pokles > 5% → do denního reportu
CONVICTION_MIN   =   2     # min. počet PI pro doporučení (menší dipy)
CONVICTION_MIN_DIP = 1     # min. počet PI při poklesu > 10%
SLEEP_FINNHUB    =   0.5   # pauza mezi Finnhub voláními (max 60 req/min)
```

**Hlavní funkce:**

| Funkce | Co dělá |
|---|---|
| `get_quote(symbol)` | Finnhub quote → fallback yfinance |
| `get_quote_yfinance(symbol)` | Cena přes yfinance (mezinárodní tickery) |
| `get_fundamentals(symbol)` | P/E, P/B, ROE, marže z Finnhub → fallback yfinance |
| `get_fundamentals_yfinance(symbol)` | Fundamenty přes yfinance |
| `get_analyst_rec(symbol)` | Strong Buy/Buy/Hold/Sell konsensus z Finnhub |
| `get_company_profile(symbol)` | Název, sektor, market cap z Finnhub → fallback yfinance |
| `get_price_target(symbol)` | Analyst price target z Finnhub (premium → vrací `{}` na free) |
| `get_sector_pe(sector)` | Hardcoded průměrné P/E podle sektoru |
| `estimate_fair_value(...)` | Fair value ze 2 metod: P/E normalizace + analyst target |
| `compute_score(ticker_data)` | Score 0–100 pro řazení příležitostí |
| `run_alert_check(watchlist)` | Rychlý mód: jen ceny + email při >±10% |
| `run_full_report(watchlist)` | Plný mód: vše + Claude + data.json |
| `claude_analyze(shortlist)` | Pošle shortlist Claudovi, dostane analýzu v češtině |
| `send_alert_email(alerts)` | HTML email přes Gmail SMTP |

**Sektorové P/E (hardcoded, `get_sector_pe`):**
```python
"Technology": 28.0, "Financials": 13.0, "Health Care": 22.0 ...
```
→ Aktualizovat ručně dle vývoje trhu, přibližně 1× ročně.

---

### `dashboard/index.html`
Single-page aplikace v čistém HTML/CSS/JS. Čte `data.json` přes `fetch()`.

**Sekce dashboardu:**
1. **Stat strip** — celkový počet tickerů, dipy, růsty, conviction 100%
2. **AI analýza** — Claude text formátovaný z markdownu
3. **Dnešní dipy** — top 8 poklesů seřazené od největšího
4. **Top Conviction** — top 8 podle score
5. **Celý watchlist** — tabulka se search, filtry (Vše/Dipy/2+PI/Analyst Buy), řazení sloupců

**Filtry v tabulce:**
- `Vše` — všechny tickery
- `Dipy ↓5%+` — pokles ≥ 5% dnes
- `2+ PI` — drží alespoň 2 PI investoři
- `Analyst Buy` — konsensus Strong Buy nebo Buy

---

## 4. Kde to běží

| Komponenta | Kde |
|---|---|
| `ticker_collector.py` | Lokálně (Windows 11, HP EliteBook 840 G6) — manuálně nebo GitHub Actions |
| `dip_screener.py` | GitHub Actions (cron) |
| `dashboard/index.html` | Netlify — `piradar.netlify.app` |
| `data.json` | Generován GitHub Actions, commitován do repo → Netlify auto-redeploy |

### GitHub Actions — plánované workflow (TODO — viz sekce 10)
```
alert.yml  — cron: každých 30 min, Po–Pá 14:30–21:00 UTC → python dip_screener.py --alert
report.yml — cron: denně 21:00 UTC (23:00 CEST)          → python dip_screener.py + git commit data.json
```

---

## 5. Přihlašovací údaje a secrets

**Nikdy neukládej hodnoty přímo do kódu. Vše je v `config.py` (dvě úrovně výš od `pi_radar/`).**

### Lokální `config.py`
```
C:\Users\Michal\Mike\eToro\eToro_report\config.py
```

Proměnné které PIRadar čte:

| Proměnná | Služba | Poznámka |
|---|---|---|
| `BULLAWARE_API_KEY` | BullAware API | Bearer token, Personal plán (€18/měs) |
| `FINNHUB_API_KEY` | Finnhub | Free tier, 60 req/min |
| `FMP_API_KEY` | Financial Modeling Prep | Free tier (aktuálně nepoužíváno — premium endpointy) |
| `CLAUDE_API_KEY` | Anthropic Claude API | Klíč "etoro-report", limit $10/měs |
| `GMAIL_FROM` | Gmail SMTP | Odesílací adresa |
| `GMAIL_APP_PASSWORD` | Gmail SMTP | App password (ne heslo účtu) |
| `GMAIL_TO` | Gmail SMTP | Příjemce alertů |

### GitHub Actions secrets (pro automatický provoz)
Stejné proměnné musí být uloženy jako GitHub Repository Secrets:
`BULLAWARE_API_KEY`, `FINNHUB_API_KEY`, `CLAUDE_API_KEY`, `GMAIL_FROM`, `GMAIL_APP_PASSWORD`, `GMAIL_TO`

---

## 6. Závislosti

### Python knihovny
```
requests      — HTTP volání (Finnhub, BullAware, Claude API)
yfinance      — fallback data pro mezinárodní tickery
rich          — barevný terminálový výstup (volitelné)
```

Instalace:
```bash
pip install requests yfinance rich
```

### Externí API a služby

| Služba | Plán | Limit | URL |
|---|---|---|---|
| BullAware API | Personal (€18/měs) | — | api.bullaware.com/v1 |
| Finnhub | Free | 60 req/min | finnhub.io/api/v1 |
| Anthropic Claude | Pay-as-you-go ($10/měs limit) | — | api.anthropic.com/v1 |
| yfinance | Zdarma (neoficiální) | — | scrape z Yahoo Finance |
| Gmail SMTP | Zdarma | — | smtp.gmail.com:465 |
| Netlify | Free tier | ~15 deployů/deploy | netlify.com |
| GitHub Actions | Free (public repo) | 2000 min/měs | github.com |

### Claude model
```python
"claude-sonnet-4-20250514"   # používá dip_screener.py
```

---

## 7. Jak spustit / otestovat

### Manuální spuštění (plný workflow)
```bash
cd C:\Users\Michal\Mike\eToro\eToro_report\pi_radar

# Krok 1: Aktualizuj watchlist (1× týdně nebo po změně PI portfolií)
python ticker_collector.py

# Krok 2: Spusť plný report
python dip_screener.py

# Krok 2b: Rychlý alert check (bez Claude, jen ceny)
python dip_screener.py --alert
```

### Lokální preview dashboardu
```bash
cd C:\Users\Michal\Mike\eToro\eToro_report\pi_radar\dashboard
python -m http.server 8080
# Otevři: http://localhost:8080
```
⚠️ Dashboard nelze otevřít přes `file://` — blokuje to CORS. Vždy přes HTTP server nebo Netlify.

### Typické časy běhu
| Skript | Tickerů | Čas |
|---|---|---|
| `ticker_collector.py` | 7 PI | ~15 sekund |
| `dip_screener.py` (full) | 161 tickerů | ~18–20 minut |
| `dip_screener.py --alert` | 161 tickerů | ~5–6 minut |

---

## 8. Jak upravit kód

### Přidat / odebrat PI investora
```python
# ticker_collector.py, řádek ~31
PI_USERNAMES = [
    "Ouzo83",
    "smudliczek",
    "Ctalbot44",
    "michalhla",
    "thomaspj",
    "JeppeKirkBonde",
    "triangulacapital",
    # sem přidej username (eToro handle)
]
```
Po úpravě spusť `ticker_collector.py` → nový `watchlist.json`.

### Přidat ticker do blacklistu
```python
# ticker_collector.py, řádek ~43
BLACKLIST = {
    "OGZDL.L",   # Gazprom — sankcionováno
    # přidej sem
}
```

### Změnit alert práh (default ±10%)
```python
# dip_screener.py, řádek ~47
ALERT_DIP_PCT   = -10.0   # změň na např. -8.0
ALERT_SURGE_PCT =  10.0
```

### Změnit sektorové P/E průměry
```python
# dip_screener.py, funkce get_sector_pe()
SECTOR_PE = {
    "Technology": 28.0,   # aktualizuj dle trhu
    ...
}
```

### Upravit Claude prompt (jazyk, styl, délka analýzy)
```python
# dip_screener.py, funkce claude_analyze()
# Prompt je na řádku ~cca 280
# Změn "Odpovídej česky." na "Respond in English." pro anglický výstup
# Změn max_tokens pro delší/kratší analýzu
```

---

## 9. Diagnostika problémů

### `[CHYBA] Nelze načíst config.py`
→ Skript nenašel `config.py`. Zkontroluj cestu — musí být dvě úrovně výš:
```
eToro_report/config.py
eToro_report/pi_radar/dip_screener.py  ← tady jsi
```

### `HTTP 403 — Finnhub endpoint`
→ Endpoint je premium. Finnhub free tier nemá `stock/price-target`.
→ Řešení: Funkce `finnhub_get()` 403 tiše ignoruje, ticker dostane prázdná data (OK).

### `No module named 'yfinance'`
```bash
pip install yfinance
# nebo pokud máš více Python verzí:
python -m pip install yfinance
```

### `possibly delisted` spam od yfinance
→ Ticker není dostupný na Yahoo Finance (delistovaný, ruský, špatný formát).
→ Řešení: Přidej symbol do `BLACKLIST` v `ticker_collector.py` a přegeneruj watchlist.

### `[CHYBA] Email: ...`
→ Zkontroluj `GMAIL_FROM` a `GMAIL_APP_PASSWORD` v `config.py`.
→ App password generuješ na: myaccount.google.com → Zabezpečení → Hesla aplikací.
→ Gmail účet musí mít zapnuté 2FA.

### `Shortlist pro Claude analýzu: 0 tickerů`
→ Žádný ticker nesplnil podmínky (pokles > 5% nebo MoS < -10% + conviction ≥ 2 PI).
→ Normální stav v klidném trhu — Claude analýza se neprovede, `claude_analysis: ""`.

### `NetworkError when attempting to fetch resource` (dashboard)
→ Otevíráš `index.html` přes `file://`. Spusť lokální server:
```bash
python -m http.server 8080
```

### BullAware timeout
→ API občas timeoutuje (zejména `smudliczek`). Skript má 3 retry s 15s pauzou.
→ Pokud vypadne celé portfolio jednoho PI, watchlist bude neúplný — spusť znovu.

### yfinance se rozbilo (stává se ~2× ročně)
→ Yahoo Finance změnil strukturu, yfinance potřebuje update.
```bash
pip install --upgrade yfinance
```
→ Mezitím mezinárodní tickery budou přeskočeny, US tickery přes Finnhub fungují dál.

---

## 10. TODO / nápady na rozšíření

### Vysoká priorita (plánováno)
- [ ] **GitHub Actions `alert.yml`** — cron každých 30 min, Po–Pá 14:30–21:00 UTC
- [ ] **GitHub Actions `report.yml`** — denně 21:00 UTC, commit `data.json`, Netlify redeploy
- [ ] **Netlify deploy** — `piradar.netlify.app`, publish directory: `dashboard/`

### Střední priorita (zmíněno během vývoje)
- [ ] **MoS filtr oprava** — RHM.DE se dostalo do shortlistu přes MoS podmínku chybně (předražená akcie s P/E 65x se dostala do "podhodnocených"). Přidat podmínku: MoS záporné = cena POD fair value
- [ ] **Sekce "Dnešní dipy" v dashboardu** — samostatné řazení podle % poklesu (FICO -14% jako #1)
- [ ] **"Odeslat na eToro" button** — prefill Social Poster (stejně jako v `analyza-akcii`)
- [ ] **5denní % změna** — doplnit Finnhub `/stock/candle` pro 5D performance (lepší než jen intraday)
- [ ] **Historické dipy** — ukládání `data.json` s datem do archivu, trend view v dashboardu

### Nízká priorita / nápady
- [ ] **Web search / news** — přidat Finnhub `/company-news` pro kontext "proč to padlo"
- [ ] **Sektorový breakdown** — kolik % portfolia PI investorů je v tech vs defense vs energy
- [ ] **Korelační analýza** — které tickery se pohybují společně (redundance v portfoliu)
- [ ] **FMP premium** ($15/měs) — přidá DCF fair value pro přesnější MoS výpočet
- [ ] **Telegram bot** — alternativa k Gmail alertům (real-time push na mobil)
- [ ] **Freemium verze** — zkrácený výstup na eToro feed, plný dashboard pro copiers

---

## 11. Repozitář a deploy

### GitHub
```
Repo: Ouzo83/pi-radar (public)
Větev: main
```

### Netlify
```
Site: piradar.netlify.app
Publish directory: dashboard
Build command: (žádný — statický HTML)
Auto-deploy: při každém commitu do main (kvůli data.json update)
```

### GitHub Actions secrets (nastavit v Settings → Secrets → Actions)
```
BULLAWARE_API_KEY
FINNHUB_API_KEY
CLAUDE_API_KEY
GMAIL_FROM
GMAIL_APP_PASSWORD
GMAIL_TO
```

---

*Dokumentace vytvořena: duben 2026*
*Kontakt: @Ouzo83 na eToro / X*
