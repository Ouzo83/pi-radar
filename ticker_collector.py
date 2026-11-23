#!/usr/bin/env python3
# ticker_collector.py — PIRadar: Sběr tickerů ze 4 PI portfolií
# Umístění: C:\Users\Michal\Mike\eToro\eToro_report\pi_radar\ticker_collector.py
# Config z:  C:\Users\Michal\Mike\eToro\eToro_report\config.py  (dvě úrovně výš)
#
# Použití:   python ticker_collector.py
# Výstup:    watchlist.json  (ve stejné složce)

import sys
import time
import json
import uuid
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
try:
    import config
    ETORO_API_KEY  = config.ETORO_API_KEY
    ETORO_USER_KEY = config.ETORO_USER_KEY
    if not ETORO_API_KEY or not ETORO_USER_KEY:
        raise AttributeError("ETORO_API_KEY / ETORO_USER_KEY jsou prázdné")
except (ImportError, AttributeError) as e:
    print(f"[CHYBA] Nelze načíst config.py: {e}")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("[CHYBA] Chybí 'requests'. Spusť: pip install requests")
    sys.exit(1)

# ── Konfigurace ───────────────────────────────────────────────────────────────
PI_USERNAMES = [
    "Ouzo83",
    "smudliczek",
    "Ctalbot44",
    "michalhla",
    "thomaspj",
    "JeppeKirkBonde",
    "triangulacapital",
]

ET_BASE    = "https://public-api.etoro.com"
SLEEP      = 1.2   # pauza mezi API voláními (limit 60 req/min)

# Tickery které nikdy nechceme ve watchlistu
# (ruské sankcionované akcie, delistované, duplikáty apod.)
BLACKLIST = {
    "OGZDL.L",    # Gazprom London GDR – sankcionováno
    "ROSNL.L",    # Rosneft London GDR – sankcionováno
    "SBER.MOEX",  # Sberbank Moskva – sankcionováno
    "SMSN.L",     # Samsung London GDR – delistováno
    "01211.HK",   # nedostupný
    "00285.HK",   # nedostupný
    "ADBE.RTH",   # Adobe after-hours token – ne reálný ticker
    "ASML.NV",    # ASML duplikát (ASML už máme)
    "TEM.US",     # špatný formát (správně je TEM)
    "FUR.NV",     # nedostupný
    "AUS.DE",     # nedostupný
    "TI5A.NV",    # nedostupný
}

SCRIPT_DIR = Path(__file__).parent
OUT_FILE   = SCRIPT_DIR / "watchlist.json"

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def et_get(path: str, params: dict | None = None, retries: int = 3) -> dict | None:
    """GET na oficiální eToro Public API (x-api-key + x-user-key)."""
    url = f"{ET_BASE}{path}"
    for attempt in range(1, retries + 1):
        headers = {
            "x-api-key":    ETORO_API_KEY,
            "x-user-key":   ETORO_USER_KEY,
            "x-request-id": str(uuid.uuid4()),
        }
        try:
            r = requests.get(url, headers=headers, params=params, timeout=20)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 60))
                log(f"  429 — čekám {wait}s ({path})")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError:
            log(f"  HTTP {r.status_code} — {path} (pokus {attempt}/{retries})")
        except Exception as e:
            log(f"  Chyba — {path}: {e} (pokus {attempt}/{retries})")
        if attempt < retries:
            time.sleep(15)
    return None


_SYMBOL_CACHE: dict[int, str] = {}


def resolve_symbols(instrument_ids: set[int]) -> dict[int, str]:
    """instrumentId → ticker (symbolFull), dávkově po 100."""
    missing = sorted(i for i in instrument_ids if i not in _SYMBOL_CACHE)
    for k in range(0, len(missing), 100):
        chunk = missing[k:k + 100]
        r = et_get("/api/v1/market-data/instruments",
                   {"instrumentIds": ",".join(map(str, chunk))})
        for d in (r or {}).get("instrumentDisplayDatas", []):
            _SYMBOL_CACHE[d["instrumentID"]] = (d.get("symbolFull") or "").upper().strip()
        time.sleep(0.5)
    return {i: _SYMBOL_CACHE.get(i, "") for i in instrument_ids}


def fetch_portfolio(username: str) -> list[dict]:
    """Vrátí agregované pozice investora ve formátu kompatibilním s původním
    BullAware výstupem: symbol, value (% portfolia), direction (1/-1), netProfit (%).
    Zdroj: eToro /user-info/people/{username}/portfolio/live (jen přímé pozice)."""
    r = et_get(f"/api/v1/user-info/people/{username}/portfolio/live")
    if not r:
        log(f"  [{username}] Portfolio nedostupné.")
        return []
    raw = r.get("positions", []) or []

    agg: dict[int, dict] = {}
    for p in raw:
        iid = p.get("instrumentId")
        w   = float(p.get("investmentPct") or 0.0)
        if iid is None:
            continue
        a = agg.setdefault(iid, {"w": 0.0, "long_w": 0.0, "short_w": 0.0, "pw": 0.0})
        a["w"]  += w
        a["pw"] += w * float(p.get("netProfit") or 0.0)
        if p.get("isBuy", True):
            a["long_w"] += w
        else:
            a["short_w"] += w

    symbols = resolve_symbols(set(agg))
    positions = []
    for iid, a in agg.items():
        positions.append({
            "symbol":    symbols.get(iid, ""),
            "value":     a["w"],
            "direction": 1 if a["long_w"] >= a["short_w"] else -1,
            "netProfit": (a["pw"] / a["w"]) if a["w"] else None,
        })
    log(f"  [{username}] {len(raw)} pozic → {len(positions)} instrumentů.")
    return positions

# ── Hlavní logika ─────────────────────────────────────────────────────────────

def collect() -> dict:
    log("═══ PIRadar: Ticker Collector ═══")
    log(f"Zpracovávám {len(PI_USERNAMES)} PI investorů...\n")

    per_investor = {}   # username → [symbol, ...]
    ticker_meta  = {}   # symbol → {investors: [...], avg_weight: float, directions: [...]}

    for username in PI_USERNAMES:
        log(f"→ {username}")
        positions = fetch_portfolio(username)
        symbols_this = []

        for pos in positions:
            symbol    = pos.get("symbol", "").upper().strip()
            weight    = pos.get("value", 0.0)       # % podíl v portfoliu
            direction = pos.get("direction", 1)      # 1 = long, -1 = short
            profit    = pos.get("netProfit")

            if not symbol:
                continue
            if symbol in BLACKLIST:
                continue

            symbols_this.append(symbol)

            if symbol not in ticker_meta:
                ticker_meta[symbol] = {
                    "symbol":     symbol,
                    "investors":  [],
                    "weights":    [],
                    "directions": [],
                    "profits":    [],
                }

            ticker_meta[symbol]["investors"].append(username)
            ticker_meta[symbol]["weights"].append(round(weight, 2))
            ticker_meta[symbol]["directions"].append(direction)
            if profit is not None:
                ticker_meta[symbol]["profits"].append(round(profit, 2))

        per_investor[username] = sorted(set(symbols_this))
        time.sleep(SLEEP)

    # ── Agregace ──────────────────────────────────────────────────────────────
    watchlist = []
    for symbol, meta in ticker_meta.items():
        investor_count = len(meta["investors"])
        avg_weight     = round(sum(meta["weights"]) / investor_count, 2)
        avg_profit     = (round(sum(meta["profits"]) / len(meta["profits"]), 2)
                          if meta["profits"] else None)
        # Směr: pokud alespoň jeden drží long → long (shorty jsou minoritní)
        direction      = "long" if meta["directions"].count(1) >= meta["directions"].count(-1) else "short"

        watchlist.append({
            "symbol":         symbol,
            "investor_count": investor_count,   # kolik PI ho drží (1–4)
            "investors":      meta["investors"],
            "avg_weight_pct": avg_weight,        # průměrný podíl v portfoliu
            "direction":      direction,
            "avg_profit_pct": avg_profit,        # průměrný P/L přes držitele
            "conviction":     round(investor_count / len(PI_USERNAMES) * 100), # % PI co ho drží
        })

    # Seřadit: nejprve počet investorů (conviction), pak průměrná váha
    watchlist.sort(key=lambda x: (-x["investor_count"], -x["avg_weight_pct"]))

    # ── Výstup ────────────────────────────────────────────────────────────────
    output = {
        "generated_at":   datetime.now().isoformat(),
        "pi_investors":   PI_USERNAMES,
        "total_tickers":  len(watchlist),
        "per_investor":   per_investor,
        "watchlist":      watchlist,
    }

    OUT_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── Shrnutí ───────────────────────────────────────────────────────────────
    print()
    log(f"═══ Hotovo ═══")
    log(f"Unikátních tickerů celkem: {len(watchlist)}")

    n = len(PI_USERNAMES)
    conviction_all  = [t for t in watchlist if t["investor_count"] == n]
    conviction_high = [t for t in watchlist if t["investor_count"] == n - 1]
    conviction_mid  = [t for t in watchlist if t["investor_count"] == n - 2]
    conviction_low  = [t for t in watchlist if t["investor_count"] < n - 2]

    log(f"  Drží všichni {n} PI:   {len(conviction_all)} tickerů → {[t['symbol'] for t in conviction_all]}")
    log(f"  Drží {n-1} z {n} PI:   {len(conviction_high)} tickerů → {[t['symbol'] for t in conviction_high]}")
    log(f"  Drží {n-2} z {n} PI:   {len(conviction_mid)} tickerů")
    log(f"  Drží méně než {n-2} PI: {len(conviction_low)} tickerů")
    log(f"Watchlist uložen: {OUT_FILE}")

    return output


if __name__ == "__main__":
    collect()
