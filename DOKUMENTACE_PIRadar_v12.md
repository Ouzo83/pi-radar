# PIRadar — Technická dokumentace
> Verze: 1.2 | Datum: duben 2026 | Autor: Michal Janíček (@Ouzo83)

---

## 1. Co to je a k čemu to slouží

**PIRadar** je automatický dip screener a investiční dashboard postavený na portfoliích vybraných eToro Popular Investors (PI).

### Účel
Každý večer analyzuje akcie z portfolií 7 prověřených PI investorů – detekuje výrazné poklesy a fundamentálně podhodnocené tituly. Výstupem je:
- Interaktivní webový dashboard s Claude AI analýzou a vstupními/výstupními cenovými pásmy
- Denní souhrnný email ve 23:00 CEST s TOP pohyby a Claude analýzou
- Okamžité email alerty při pohybu >10% (pokles) nebo >15% (růst) přes den

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
        ├── Finnhub API    → cena, fundamenty, analyst rec, company profile, news
        ├── yfinance       → fallback pro mezinárodní tickery (.L, .DE, .HK...)
        ├── Claude API     → narativní analýza shortlistu + entry/exit ceny
        │
        ├── [--alert]      → Gmail SMTP alert při poklesu >10% nebo růstu >15%
        ├── [full report]  → Gmail SMTP denní souhrnný email ve 23:00 CEST
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
| Alert check | `--alert` | každých 30 min (Po–Pá, market hours) | Jen ceny, email při poklesu >10% nebo růstu >15% |
| Full report | *(bez přepínače)* | denně 21:00 UTC (23:00 CEST), Po–Pá | Plná analýza + Claude + news + data.json + denní email |

### Conviction systém
Každý ticker dostane skóre conviction podle toho, kolik PI investorů ho aktuálně drží (max 7):
- **7/7 PI** = conviction 100% – nejvyšší priorita
- **Score 0–100** kombinuje: conviction (max 40b) + margin of safety (max 25b) + denní pokles (max 20b) + analyst sentiment (max 15b)

---

## 3. Soubory a komponenty

### Adresářová struktura
```
C:\Users\Michal\Mike\eToro\eToro_report\pi_radar\
│
├── ticker_collector.py     ← krok 1: sběr tickerů z PI portfolií
├── dip_screener.py         ← krok 2: market data + analýza + alerty + email
├── watchlist.json          ← generovaný: seznam tickerů (vstup pro screener)
├── alert_state.json        ← generovaný: paměť odeslaných alertů (1× denně/ticker)
├── DOKUMENTACE_PIRadar.md  ← tato dokumentace
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

**Aktuální PI investoři (7):**
```python
PI_USERNAMES = [
    "Ouzo83",
    "smudliczek",
    "Ctalbot44",
    "michalhla",
    "thomaspj",
    "JeppeKirkBonde",
    "triangulacapital",
]
```

**Výstup `watchlist.json`:**
```json
{
  "generated_at": "2026-04-13T21:00:00",
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
Hlavní skript. Stahuje tržní data, detekuje dipy, volá Claude, posílá alerty i denní email, ukládá dashboard data.

**Klíčové konstanty (nahoře souboru):**
```python
ALERT_DIP_PCT      = -10.0   # pokles > 10% → okamžitý email alert
ALERT_SURGE_PCT    =  15.0   # nárůst > 15% → okamžitý email alert
SCREENER_DIP_PCT   =  -5.0   # pokles > 5% → do denního reportu
CONVICTION_MIN     =   2     # min. počet PI pro doporučení (menší dipy)
CONVICTION_MIN_DIP =   1     # min. počet PI při poklesu > 10%
SLEEP_FINNHUB      =   0.5   # pauza mezi Finnhub voláními (max 60 req/min)
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
| `get_company_news(symbol, days=3)` | Posledních 3 dny headlines z Finnhub (max 5) |
| `get_sector_pe(sector)` | Hardcoded průměrné P/E podle sektoru |
| `estimate_fair_value(...)` | Fair value ze 2 metod: P/E normalizace + analyst target |
| `compute_score(ticker_data)` | Score 0–100 pro řazení příležitostí |
| `run_alert_check(watchlist)` | Rychlý mód: jen ceny + email při >10%/-15% |
| `run_full_report(watchlist)` | Plný mód: vše + Claude + data.json + denní email |
| `claude_analyze(shortlist)` | Pošle shortlist + news Claudovi, analýza v češtině |
| `send_alert_email(alerts)` | Okamžitý HTML email při velkém pohybu, Prague timezone |
| `send_report_email(data, analysis, count)` | Denní souhrnný HTML email ve 23:00 CEST |

**Company news (`get_company_news`):**
- Volá Finnhub `/company-news` endpoint (free tier ✅)
- Stáhne headlines za posledních 3 dny, max 5 článků na ticker
- Headlines jsou předány Claudovi jako součást dat pro analýzu
- Claude má instrukci news vždy použít k vysvětlení proč akcie klesla/rostla
- Příklad výstupu: *"FICO kleslo -14% po slabším výhledu pro Q2 – fundamenty s ROE 146% zůstávají silné"*

**Sektorové P/E (hardcoded, `get_sector_pe`):**
```python
"Technology": 28.0, "Financials": 13.0, "Health Care": 22.0 ...
```
→ Aktualizovat ručně dle vývoje trhu, přibližně 1× ročně.

---

### `dashboard/index.html`
Single-page aplikace v čistém HTML/CSS/JS. Čte `data.json` přes `fetch()`. Conviction tečky jsou dynamické – zobrazují kolik ze 7 PI daný ticker drží. Čas generování zobrazuje Prague timezone.

**Sekce dashboardu:**
1. **Stat strip** — celkový počet tickerů, dipy ↓5%+, růsty ↑5%+, Claude analýza, conviction 100%
2. **AI analýza** — Claude text formátovaný z markdownu (včetně news kontextu)
3. **Dnešní dipy** — top 8 poklesů seřazené od největšího
4. **Top Conviction** — top 8 podle score
5. **Celý watchlist** — tabulka se search, filtry, řazení sloupců

**Filtry v tabulce:**
- `Vše` — všechny tickery
- `Dipy ↓5%+` — pokles ≥ 5% dnes
- `2+ PI` — drží alespoň 2 PI investoři
- `Analyst Buy` — konsensus Strong Buy nebo Buy

⚠️ Dashboard nelze otevřít přes `file://` — blokuje CORS. Lokálně vždy přes:
```bash
cd dashboard
python -m http.server 8080
# Otevři: http://localhost:8080
```

---

## 4. Kde to běží

| Komponenta | Kde |
|---|---|
| `ticker_collector.py` | GitHub Actions (automaticky před každým reportem) |
| `dip_screener.py` | GitHub Actions (cron) |
| `dashboard/index.html` | Netlify — `pi-radar.netlify.app` |
| `data.json` | Generován GitHub Actions, commitován do repo → Netlify auto-redeploy |

### GitHub Actions — aktivní workflows

**`alert.yml`** — cron každých 30 min, Po–Pá 14:00–21:00 UTC:
```
python dip_screener.py --alert
→ git pull --rebase + commit alert_state.json
```

**`report.yml`** — denně 21:00 UTC (23:00 CEST), Po–Pá:
```
python ticker_collector.py    ← aktualizace watchlistu
python dip_screener.py        ← plný report + Claude + news + denní email
→ git pull --rebase + commit dashboard/data.json + watchlist.json → Netlify auto-redeploy
```

---

## 5. Přihlašovací údaje a secrets

**Nikdy neukládej hodnoty přímo do kódu. Vše je v `config.py` (dvě úrovně výš od `pi_radar/`).**

### Lokální `config.py`
```
C:\Users\Michal\Mike\eToro\eToro_report\config.py
```

Proměnné které PIRadar čte — používej **PŘESNĚ tyto názvy**:

| Proměnná | Služba | Poznámka |
|---|---|---|
| `BULLAWARE_API_KEY` | BullAware API | Bearer token, Personal plán (€18/měs) |
| `FINNHUB_API_KEY` | Finnhub | Free tier, 60 req/min |
| `CLAUDE_API_KEY` | Anthropic Claude API | Klíč "etoro-report", limit $10/měs |
| `GMAIL_FROM` | Gmail SMTP | Odesílací adresa |
| `GMAIL_APP_PASSWORD` | Gmail SMTP | App password (ne heslo účtu) |
| `GMAIL_TO` | Gmail SMTP | Příjemce alertů i denního reportu |
| `ETORO_API_KEY` | eToro Official API | PIRadar nepoužívá, jen pro kompatibilitu |
| `ETORO_USER_KEY` | eToro Official API | PIRadar nepoužívá, jen pro kompatibilitu |

⚠️ `FMP_API_KEY` PIRadar nepoužívá — FMP free tier nepodporuje potřebné endpointy.

### GitHub Actions secrets
Nastavit v: `github.com/Ouzo83/pi-radar → Settings → Secrets and variables → Actions`

```
BULLAWARE_API_KEY
FINNHUB_API_KEY
CLAUDE_API_KEY
GMAIL_FROM
GMAIL_APP_PASSWORD
GMAIL_TO
```

### GitHub Actions permissions
Nastavit v: `Settings → Actions → General → Workflow permissions`
→ **Read and write permissions** (nutné pro commit data.json zpět do repo)

---

## 6. Závislosti

### Python knihovny
```bash
pip install requests yfinance rich
```

| Knihovna | Použití |
|---|---|
| `requests` | HTTP volání (Finnhub, BullAware, Claude API) |
| `yfinance` | Fallback data pro mezinárodní tickery |
| `rich` | Barevný terminálový výstup (volitelné) |
| `zoneinfo` | Prague timezone v emailech (součást Python 3.9+) |
| `re` | Regex pro formátování Claude analýzy v emailu |

### Externí API a služby

| Služba | Plán | Limit | Poznámka |
|---|---|---|---|
| BullAware API | Personal (€18/měs) | — | api.bullaware.com/v1 |
| Finnhub | Free | 60 req/min | finnhub.io/api/v1 – včetně company-news |
| Anthropic Claude | Pay-as-you-go ($10/měs limit) | — | model: claude-sonnet-4-20250514 |
| yfinance | Zdarma (neoficiální) | — | Scrape z Yahoo Finance, ~2× ročně se rozbije |
| Gmail SMTP | Zdarma | — | smtp.gmail.com:465, vyžaduje App Password + 2FA |
| Netlify | Free tier | — | pi-radar.netlify.app |
| GitHub Actions | Free (public repo) | Neomezené minuty | Cron workflows |

---

## 7. Jak spustit / otestovat

### Automatický provoz (standardní stav)
Vše běží samo přes GitHub Actions Po–Pá. Není potřeba nic spouštět manuálně.

### Manuální spuštění (debug / test)
```bash
cd C:\Users\Michal\Mike\eToro\eToro_report\pi_radar

# Plný report + denní email (trvá ~18-20 minut)
python dip_screener.py

# Rychlý alert check (trvá ~5-6 minut)
python dip_screener.py --alert

# Jen aktualizace watchlistu (~15 sekund)
python ticker_collector.py
```

### Lokální preview dashboardu
```bash
cd C:\Users\Michal\Mike\eToro\eToro_report\pi_radar\dashboard
python -m http.server 8080
# Otevři v prohlížeči: http://localhost:8080
```

### Ruční spuštění Actions workflow
`github.com/Ouzo83/pi-radar → Actions → [workflow] → Run workflow`

### Git workflow při lokálních úpravách
Pokud Actions právě běží a commituje, může dojít ke konfliktu:
```bash
git pull          # nejdřív stáhni co Actions commitoval
git push          # pak pushni své změny
```

### Typické časy běhu
| Skript | Tickerů | Čas |
|---|---|---|
| `ticker_collector.py` | 7 PI | ~15 sekund |
| `dip_screener.py` (full) | 161 tickerů | ~18–20 minut |
| `dip_screener.py --alert` | 161 tickerů | ~5–6 minut |
| GitHub Actions full report | 161 tickerů | ~8–10 minut (rychlejší servery) |

---

## 8. Jak upravit kód

### Přidat / odebrat PI investora
```python
# ticker_collector.py, řádek ~31
PI_USERNAMES = [
    "Ouzo83",
    # přidej username sem
]
```
Watchlist se automaticky aktualizuje každý večer přes GitHub Actions.

### Přidat ticker do blacklistu
```python
# ticker_collector.py, řádek ~43
BLACKLIST = {
    "OGZDL.L",   # Gazprom — sankcionováno
    # přidej sem
}
```

### Změnit alert prahy
```python
# dip_screener.py, řádek ~47
ALERT_DIP_PCT    = -10.0   # pokles → okamžitý alert
ALERT_SURGE_PCT  =  15.0   # růst → okamžitý alert
SCREENER_DIP_PCT =  -5.0   # pokles → do denního reportu
```

### Změnit počet dní pro news
```python
# dip_screener.py, funkce get_company_news()
news = get_company_news(symbol, days=3)   # změň na 1, 3, nebo 7
```

### Změnit sektorové P/E průměry
```python
# dip_screener.py, funkce get_sector_pe()
SECTOR_PE = {
    "Technology": 28.0,   # aktualizuj dle trhu ~1× ročně
    ...
}
```

### Upravit Claude prompt
```python
# dip_screener.py, funkce claude_analyze()
# "Odpovídej česky." → "Respond in English." pro anglický výstup
# max_tokens=2000 → více pro delší analýzu
```

### Vypnout denní email (ponechat jen dashboard)
```python
# dip_screener.py, funkce run_full_report()
# Zakomentuj nebo odstraň:
# send_report_email(data, claude_analysis, len(shortlist))
```

---

## 9. Diagnostika problémů

### `[CHYBA] Nelze načíst config.py`
→ Skript nenašel `config.py`. Musí být dvě úrovně výš:
```
eToro_report/config.py                    ← zde
eToro_report/pi_radar/dip_screener.py     ← spouštíš odsud
```

### `HTTP 403 — Finnhub endpoint`
→ Endpoint je premium. `finnhub_get()` 403 tiše ignoruje – ticker dostane prázdná data, vše funguje dál.

### `No module named 'yfinance'`
```bash
python -m pip install yfinance
```

### `possibly delisted` zprávy od yfinance
→ Ticker není na Yahoo Finance. Přidej do `BLACKLIST` v `ticker_collector.py`.

### `[CHYBA] Email` nebo `[CHYBA] Report email`
→ Zkontroluj `GMAIL_FROM` a `GMAIL_APP_PASSWORD` v `config.py`.
→ App password: myaccount.google.com → Zabezpečení → Hesla aplikací. Gmail musí mít 2FA.

### `Shortlist pro Claude analýzu: 0 tickerů`
→ Normální stav v klidném trhu. Claude analýza se neprovede, denní email přijde bez AI sekce.

### `NetworkError` v dashboardu
→ Otevíráš `index.html` přes `file://`. Spusť `python -m http.server 8080`.

### BullAware timeout
→ API občas timeoutuje. Skript má 3 retry s 15s pauzou. Spusť znovu.

### yfinance se rozbilo (~2× ročně)
```bash
pip install --upgrade yfinance
```
→ Mezitím mezinárodní tickery budou přeskočeny, US tickery fungují dál.

### GitHub Actions `! [rejected] main -> main`
→ Actions commitoval do repo zatímco ty jsi lokálně pushoval.
```bash
git pull
git push
```

### Dashboard ukazuje starý čas (o 2h méně)
→ Byl bug s UTC vs Prague timezone – opraven v dashboard/index.html v1.2.
→ Pokud se znovu objeví, zkontroluj `toLocaleString('cs-CZ', {timeZone: 'Europe/Prague'})`.

---

## 10. TODO / nápady na rozšíření

### Střední priorita
- [ ] **MoS filtr oprava** — předražené akcie (P/E >> sektor) se dostávají do shortlistu chybně. Přidat podmínku: MoS záporné = cena POD fair value
- [ ] **5denní % změna** — Finnhub `/stock/candle` pro 5D performance (lepší než jen intraday)
- [ ] **Historické dipy** — ukládání `data.json` s datem do archivu, trend view v dashboardu
- [ ] **"Odeslat na eToro" button** — prefill Social Poster (stejně jako v `analyza-akcii`)

### Nízká priorita / nápady
- [ ] **Sektorový breakdown** — kolik % portfolia PI investorů je v tech vs defense vs energy
- [ ] **Korelační analýza** — které tickery se pohybují společně (redundance v portfoliu)
- [ ] **FMP premium** ($15/měs) — přidá DCF fair value pro přesnější MoS výpočet
- [ ] **Telegram bot** — alternativa k Gmail alertům (real-time push na mobil)
- [ ] **Freemium verze** — zkrácený výstup na eToro feed, plný dashboard pro copiers
- [ ] **Node.js 24** — aktualizovat actions/checkout@v4 a actions/setup-python@v5 (aktuálně Node.js 20 warning)

### Dokončeno (přesunuto z TODO)
- [x] **Company news** — Finnhub `/company-news` přidán, Claude analýza vidí headlines
- [x] **Denní souhrnný email** — `send_report_email()` odesílá report každý večer ve 23:00 CEST
- [x] **Timezone fix** — Praha timezone v emailech i dashboardu
- [x] **Conviction tečky dynamické** — zobrazují kolik ze 7 PI ticker drží
- [x] **Blacklist** — ruské/delistované/junk tickery automaticky vyřazeny

---

## 11. Repozitář a deploy

### GitHub
```
Repo:   github.com/Ouzo83/pi-radar (public)
Větev:  main
```

### Netlify
```
Site:              pi-radar.netlify.app
Publish directory: dashboard
Build command:     (žádný — statický HTML)
Auto-deploy:       při každém commitu do main
```

### Lokální umístění
```
C:\Users\Michal\Mike\eToro\eToro_report\pi_radar\
```

---

*Dokumentace vytvořena: duben 2026 | Verze 1.2 — přidány company news, denní email, timezone fix*
*Kontakt: @Ouzo83 na eToro / X*
