#!/usr/bin/env python3
# dip_screener.py — PIRadar: Dip detektor + alert engine + Claude analýza
# Umístění: C:\Users\Michal\Mike\eToro\eToro_report\pi_radar\dip_screener.py
# Config z:  C:\Users\Michal\Mike\eToro\eToro_report\config.py  (dvě úrovně výš)
#
# Použití:
#   python dip_screener.py            → plný běh (data + analýza + dashboard)
#   python dip_screener.py --alert    → jen alert check (rychlý, pro 30min cron)
#   python dip_screener.py --report   → plný report (pro 22:00 cron)

import sys
import time
import json
import smtplib
import argparse
from datetime import datetime, timezone
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ── Config ────────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
try:
    import config
    BULLAWARE_API_KEY = config.BULLAWARE_API_KEY
    FINNHUB_API_KEY   = config.FINNHUB_API_KEY
    CLAUDE_API_KEY    = config.CLAUDE_API_KEY
    GMAIL_USER        = config.GMAIL_FROM
    GMAIL_PASS        = config.GMAIL_APP_PASSWORD
    GMAIL_TO          = config.GMAIL_TO
except (ImportError, AttributeError) as e:
    print(f"[CHYBA] Nelze načíst config.py: {e}")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("[CHYBA] Chybí 'requests'. Spusť: pip install requests")
    sys.exit(1)

# ── Konstanty ─────────────────────────────────────────────────────────────────
SCRIPT_DIR      = Path(__file__).parent
WATCHLIST_FILE  = SCRIPT_DIR / "watchlist.json"
DASHBOARD_FILE  = SCRIPT_DIR / "dashboard" / "data.json"
STATE_FILE      = SCRIPT_DIR / "alert_state.json"   # pamatuje si co jsme už alertovali

FINNHUB_BASE    = "https://finnhub.io/api/v1"

CLAUDE_BASE     = "https://api.anthropic.com/v1"

# Alert prahy
ALERT_DIP_PCT       = -10.0   # pokles o víc než 10 % → alert
ALERT_SURGE_PCT     =  15.0   # nárůst o víc než 15 % → alert (value investor = surge méně důležitý)
SCREENER_DIP_PCT    =  -5.0   # pro denní report: zajímavý pokles
CONVICTION_MIN      =  2      # minimální počet PI investorů pro doporučení
CONVICTION_MIN_DIP  =  1      # stačí 1 PI investor pokud pokles > ALERT_DIP_PCT

SLEEP_FINNHUB = 0.5   # 60 req/min → max 2/s, jedeme 0.5s pro jistotu

# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")

# ── Načtení watchlistu ────────────────────────────────────────────────────────
def load_watchlist() -> list[dict]:
    if not WATCHLIST_FILE.exists():
        log("[CHYBA] watchlist.json nenalezen. Spusť nejdřív ticker_collector.py")
        sys.exit(1)
    data = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    return data.get("watchlist", [])

# ── Finnhub API ───────────────────────────────────────────────────────────────
def finnhub_get(endpoint: str, params: dict = None) -> dict | None:
    url    = f"{FINNHUB_BASE}/{endpoint}"
    params = params or {}
    params["token"] = FINNHUB_API_KEY
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 403:
            return None   # premium endpoint – tiše přeskočíme
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError:
        return None
    except Exception as e:
        log(f"  [Finnhub] {endpoint}: {e}")
        return None

def get_quote_yfinance(symbol: str) -> dict | None:
    """Fallback: cena přes yfinance (pro mezinárodní tickery)."""
    try:
        import yfinance as yf
        import logging
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        t = yf.Ticker(symbol)
        info = t.fast_info
        price     = getattr(info, "last_price",      None)
        prev      = getattr(info, "previous_close",  None)
        high_52w  = getattr(info, "year_high",       None)
        low_52w   = getattr(info, "year_low",        None)
        if not price:
            return None
        change_abs = round(price - prev, 4) if prev else None
        change_pct = round((price - prev) / prev * 100, 2) if prev and prev != 0 else None
        return {
            "symbol":     symbol,
            "price":      price,
            "change_pct": change_pct,
            "change_abs": change_abs,
            "high_today": None,
            "low_today":  None,
            "prev_close": prev,
            "high_52w":   high_52w,
            "low_52w":    low_52w,
            "source":     "yfinance",
        }
    except Exception as e:
        log(f"  [yfinance] {symbol}: {e}")
        return None


def get_fundamentals_yfinance(symbol: str) -> dict:
    """Fallback: fundamenty přes yfinance (pro mezinárodní tickery)."""
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        dy = info.get("dividendYield")
        # yfinance vrací dividendYield jako decimal (0.03 = 3%) – cap na 15%
        if dy and dy > 0.15:
            dy = None
        return {
            "pe_ttm":         info.get("trailingPE"),
            "pb":             info.get("priceToBook"),
            "ps":             info.get("priceToSalesTrailing12Months"),
            "eps_ttm":        info.get("trailingEps"),
            "revenue_growth": info.get("revenueGrowth"),
            "gross_margin":   info.get("grossMargins"),
            "net_margin":     info.get("profitMargins"),
            "roe":            info.get("returnOnEquity"),
            "debt_equity":    info.get("debtToEquity"),
            "current_ratio":  info.get("currentRatio"),
            "beta":           info.get("beta"),
        }
    except Exception as e:
        log(f"  [yfinance] fundamenty {symbol}: {e}")
        return {}


def get_profile_yfinance(symbol: str) -> dict:
    """Fallback: profil firmy přes yfinance."""
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        return {
            "name":       info.get("longName") or info.get("shortName", symbol),
            "sector":     info.get("sector", ""),
            "industry":   info.get("industry", ""),
            "market_cap": info.get("marketCap"),
            "exchange":   info.get("exchange", ""),
            "currency":   info.get("currency", ""),
        }
    except Exception:
        return {}


def get_quote(symbol: str) -> dict | None:
    """Vrátí aktuální cenu a denní změnu. Finnhub primárně, yfinance jako fallback."""
    data = finnhub_get("quote", {"symbol": symbol})
    if data and data.get("c", 0) != 0:
        return {
            "symbol":     symbol,
            "price":      data.get("c"),
            "change_pct": data.get("dp"),
            "change_abs": data.get("d"),
            "high_today": data.get("h"),
            "low_today":  data.get("l"),
            "prev_close": data.get("pc"),
            "high_52w":   data.get("h52"),
            "low_52w":    data.get("l52"),
            "source":     "finnhub",
        }
    # Finnhub nepodporuje tento symbol (mezinárodní burza) → zkusíme yfinance
    return get_quote_yfinance(symbol)

def get_fundamentals(symbol: str) -> dict:
    """Vrátí základní fundamenty z Finnhub, fallback yfinance."""
    data = finnhub_get("stock/metric", {"symbol": symbol, "metric": "all"})
    if not data:
        return get_fundamentals_yfinance(symbol)
    m = data.get("metric", {})
    return {
        "pe_ttm":          m.get("peBasicExclExtraTTM"),
        "pb":              m.get("pbAnnual"),
        "ps":              m.get("psTTM"),
        "eps_ttm":         m.get("epsBasicExclExtraAnnual"),
        "revenue_growth":  m.get("revenueGrowthTTMYoy"),
        "gross_margin":    m.get("grossMarginTTM"),
        "net_margin":      m.get("netProfitMarginTTM"),
        "roe":             m.get("roeTTM"),
        "debt_equity":     m.get("totalDebt/totalEquityAnnual"),
        "current_ratio":   m.get("currentRatioAnnual"),
        "beta":            m.get("beta"),
    }

def get_analyst_rec(symbol: str) -> dict:
    """Analyst recommendations z Finnhub."""
    data = finnhub_get("stock/recommendation", {"symbol": symbol})
    if not data or not isinstance(data, list) or len(data) == 0:
        return {}
    latest = data[0]
    total = (latest.get("strongBuy", 0) + latest.get("buy", 0) +
             latest.get("hold", 0) + latest.get("sell", 0) + latest.get("strongSell", 0))
    buy_score = latest.get("strongBuy", 0) + latest.get("buy", 0)
    consensus = (
        "Strong Buy" if buy_score / total > 0.6 else
        "Buy"        if buy_score / total > 0.4 else
        "Hold"       if latest.get("hold", 0) / total > 0.4 else
        "Sell"
    ) if total > 0 else "N/A"
    return {
        "consensus":   consensus,
        "strong_buy":  latest.get("strongBuy", 0),
        "buy":         latest.get("buy", 0),
        "hold":        latest.get("hold", 0),
        "sell":        latest.get("sell", 0),
        "strong_sell": latest.get("strongSell", 0),
        "total":       total,
        "period":      latest.get("period", ""),
    }

def get_company_profile(symbol: str) -> dict:
    """Profil firmy z Finnhub, fallback yfinance."""
    data = finnhub_get("stock/profile2", {"symbol": symbol})
    if not data:
        return get_profile_yfinance(symbol)
    return {
        "name":       data.get("name", symbol),
        "sector":     data.get("finnhubIndustry", ""),
        "industry":   data.get("finnhubIndustry", ""),
        "market_cap": data.get("marketCapitalization"),
        "exchange":   data.get("exchange", ""),
        "currency":   data.get("currency", "USD"),
    }

def get_price_target(symbol: str) -> dict:
    """Analyst price target z Finnhub."""
    data = finnhub_get("stock/price-target", {"symbol": symbol})
    if not data:
        return {}
    return {
        "target_high":   data.get("targetHigh"),
        "target_low":    data.get("targetLow"),
        "target_mean":   data.get("targetMean"),
        "target_median": data.get("targetMedian"),
    }

def get_company_news(symbol: str, days: int = 3) -> list[str]:
    """Posledních N dní novinek z Finnhub – vrátí max 5 headlines."""
    from datetime import timedelta
    date_to   = datetime.now().strftime("%Y-%m-%d")
    date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    data = finnhub_get("company-news", {
        "symbol": symbol,
        "from":   date_from,
        "to":     date_to,
    })
    if not data or not isinstance(data, list):
        return []
    headlines = []
    for item in data[:5]:
        headline = item.get("headline", "").strip()
        source   = item.get("source", "")
        if headline:
            headlines.append(f"{headline} [{source}]")
    return headlines


def get_sector_pe(sector: str) -> float | None:
    """Hardcoded sektorové průměry P/E."""
    SECTOR_PE = {
        "Technology":             28.0,
        "Communication Services": 20.0,
        "Consumer Discretionary": 22.0,
        "Consumer Staples":       20.0,
        "Health Care":            22.0,
        "Financials":             13.0,
        "Industrials":            20.0,
        "Energy":                 12.0,
        "Materials":              16.0,
        "Real Estate":            30.0,
        "Utilities":              18.0,
    }
    return SECTOR_PE.get(sector)

# ── Výpočet fair value (bez DCF z FMP premium) ────────────────────────────────
def estimate_fair_value(price: float, fundamentals: dict,
                        price_target: dict, sector: str) -> dict:
    """
    Odhadne fair value třemi metodami:
    1. P/E normalizace vůči sektorovému průměru
    2. Analyst consensus target (průměr)
    3. Vzdálenost od 52W high
    Výsledek: průměr dostupných metod.
    """
    estimates = []
    details   = {}

    # Metoda 1: P/E vs sektor
    pe  = fundamentals.get("pe_ttm")
    eps = fundamentals.get("eps_ttm")
    sector_pe = get_sector_pe(sector)
    if pe and eps and sector_pe and pe > 0 and eps > 0:
        fair_pe = sector_pe * eps
        estimates.append(fair_pe)
        details["fair_pe_method"] = round(fair_pe, 2)
        details["discount_to_sector_pe"] = round((price - fair_pe) / fair_pe * 100, 1)

    # Metoda 2: Analyst consensus target
    target_mean = price_target.get("target_mean")
    if target_mean and target_mean > 0:
        estimates.append(target_mean)
        details["fair_analyst_target"] = round(target_mean, 2)
        details["discount_to_target"] = round((price - target_mean) / target_mean * 100, 1)

    if not estimates:
        return {"fair_value": None, "margin_of_safety_pct": None, **details}

    fair_avg = sum(estimates) / len(estimates)
    mos      = round((price - fair_avg) / fair_avg * 100, 1)   # záporné = podhodnocení

    return {
        "fair_value":          round(fair_avg, 2),
        "margin_of_safety_pct": mos,   # záporné = cena pod fair value (příležitost)
        **details,
    }

# ── Scoring ───────────────────────────────────────────────────────────────────
def compute_score(ticker_data: dict) -> int:
    """
    Skóre 0–100 určuje prioritu tickeru v reportu.
    Vyšší = zajímavější příležitost.
    """
    score = 0
    w = ticker_data.get("watchlist_meta", {})
    q = ticker_data.get("quote", {}) or {}
    f = ticker_data.get("fundamentals", {}) or {}
    v = ticker_data.get("valuation", {}) or {}
    a = ticker_data.get("analyst", {}) or {}

    # Conviction (kolik PI investorů drží)
    score += w.get("investor_count", 0) * 10   # max 40

    # Pokles od fair value (margin of safety)
    mos = v.get("margin_of_safety_pct")
    if mos is not None:
        if mos < -30:   score += 25
        elif mos < -20: score += 18
        elif mos < -10: score += 10
        elif mos < 0:   score += 5

    # Denní pokles (dip)
    chg = q.get("change_pct")
    if chg is not None:
        if chg < -10:   score += 20
        elif chg < -5:  score += 12
        elif chg < -2:  score += 5

    # Analyst sentiment
    consensus = a.get("consensus", "")
    if consensus == "Strong Buy":   score += 15
    elif consensus == "Buy":        score += 8

    return min(score, 100)

# ── Alert state (aby jsme neposílali stejný alert 10×) ────────────────────────
def load_alert_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_alert_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def should_alert(symbol: str, change_pct: float, state: dict) -> bool:
    """Vrátí True jen pokud jsme tento alert dnes ještě neposlali."""
    today = datetime.now().strftime("%Y-%m-%d")
    key   = f"{symbol}_{today}"
    # Alert posíláme max 1× za den na ticker
    if key in state:
        return False
    return True

def mark_alerted(symbol: str, state: dict):
    today = datetime.now().strftime("%Y-%m-%d")
    key   = f"{symbol}_{today}"
    state[key] = datetime.now().isoformat()
    # Pročistíme záznamy starší než 2 dny
    cutoff = datetime.now().strftime("%Y-%m-%")
    state = {k: v for k, v in state.items() if k.split("_")[1][:7] >= cutoff[:-1]}
    return state

# ── Email ─────────────────────────────────────────────────────────────────────
def send_alert_email(alerts: list[dict]):
    if not alerts:
        return

    rows = ""
    for a in alerts:
        color  = "#f85149" if a["change_pct"] < 0 else "#3fb950"
        sign   = "+" if a["change_pct"] > 0 else ""
        inv    = ", ".join(a.get("investors", []))
        rows += f"""
        <tr>
          <td style="font-weight:600;padding:8px 12px;color:#e6edf3">{a['symbol']}</td>
          <td style="color:{color};font-weight:600;padding:8px 12px">{sign}{a['change_pct']:.1f}%</td>
          <td style="padding:8px 12px;color:#e6edf3">${a['price']:.2f}</td>
          <td style="color:#8b949e;padding:8px 12px">{inv}</td>
          <td style="padding:8px 12px;color:#e6edf3">{a.get('conviction', 0)}% conviction</td>
        </tr>"""

    from zoneinfo import ZoneInfo
    prague_time = datetime.now(ZoneInfo("Europe/Prague")).strftime('%d.%m.%Y %H:%M')
    html = f"""
    <html><body style="background:#0d1117;color:#e6edf3;font-family:sans-serif;padding:24px">
    <h2 style="color:#f0883e">⚡ PIRadar Alert — {prague_time}</h2>
    <p style="color:#8b949e">Detekován pohyb &gt; ±10% u sledovaných PI akcií:</p>
    <table style="border-collapse:collapse;background:#161b22;border-radius:8px;width:100%;color:#e6edf3">
      <thead>
        <tr style="color:#8b949e;font-size:12px;text-transform:uppercase">
          <th style="padding:8px 12px;text-align:left;color:#8b949e">Symbol</th>
          <th style="padding:8px 12px;text-align:left;color:#8b949e">Změna</th>
          <th style="padding:8px 12px;text-align:left;color:#8b949e">Cena</th>
          <th style="padding:8px 12px;text-align:left;color:#8b949e">PI investoři</th>
          <th style="padding:8px 12px;text-align:left;color:#8b949e">Conviction</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    <p style="color:#8b949e;margin-top:20px;font-size:12px">
      PIRadar — automatický alert | <a href="https://piradar.netlify.app" style="color:#58a6ff">Otevřít dashboard ↗</a>
    </p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    from zoneinfo import ZoneInfo
    prague_time = datetime.now(ZoneInfo("Europe/Prague")).strftime('%d.%m.%Y %H:%M')
    msg["Subject"] = f"⚡ PIRadar Alert: {', '.join(a['symbol'] for a in alerts[:3])} pohyb >{abs(ALERT_DIP_PCT):.0f}%"
    msg["From"]    = GMAIL_USER
    msg["To"]      = GMAIL_TO
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.send_message(msg)
        log(f"  ✓ Alert email odeslán: {[a['symbol'] for a in alerts]}")
    except Exception as e:
        log(f"  [CHYBA] Email: {e}")

# ── Notifikační email po full reportu ─────────────────────────────────────────
def send_report_ready_email(dashboard_data: dict):
    """Odešle teasing email po úspěšném vygenerování denního reportu.
    Subject obsahuje rychlé shrnutí, body stat strip + top 3 dipy + CTA."""
    DASHBOARD_URL = "https://pi-radar.netlify.app"

    tickers = dashboard_data.get("tickers", []) or []
    if not tickers:
        log("  [Notifikace] Přeskočeno — žádná ticker data.")
        return

    # Dipy ≥ 5 % pokles, seřazené od největšího
    dips = [
        t for t in tickers
        if ((t.get("quote", {}) or {}).get("change_pct") is not None
            and (t.get("quote", {}) or {}).get("change_pct") <= SCREENER_DIP_PCT)
    ]
    dips.sort(key=lambda x: (x.get("quote", {}) or {}).get("change_pct", 0))
    top_dips = dips[:3]

    # Nejvyšší conviction ticker
    def _conv(t):
        return (t.get("watchlist_meta", {}) or {}).get("conviction", 0) or 0
    top_conv_ticker = max(tickers, key=_conv) if tickers else None
    top_conv_symbol = top_conv_ticker["symbol"] if top_conv_ticker else "-"
    top_conv_value  = _conv(top_conv_ticker) if top_conv_ticker else 0

    # Prague time
    from zoneinfo import ZoneInfo
    now_prague = datetime.now(ZoneInfo("Europe/Prague"))
    date_str   = f"{now_prague.day}.{now_prague.month}."

    # Subject - teasing s hlavní metrikou
    if top_dips:
        top = top_dips[0]
        top_sym = top["symbol"]
        top_pct = (top.get("quote", {}) or {}).get("change_pct", 0)
        subject = f"🎯 PI Radar [{date_str}] · {len(dips)} dipů · top: {top_sym} {top_pct:+.1f}%"
    else:
        subject = f"🎯 PI Radar [{date_str}] · klidný den (žádné dipy ≥5 %)"

    # Tabulka top 3 dipů
    if top_dips:
        dip_rows = ""
        for t in top_dips:
            q    = t.get("quote", {}) or {}
            w    = t.get("watchlist_meta", {}) or {}
            chg  = q.get("change_pct", 0)
            inv  = w.get("investor_count", 0)
            dots = "●" * min(inv, 7) + "○" * max(7 - inv, 0)
            dip_rows += f"""
            <tr>
              <td style="font-weight:600;padding:10px 14px;color:#e6edf3;font-size:15px">{t['symbol']}</td>
              <td style="color:#f85149;font-weight:700;padding:10px 14px;text-align:right;font-size:15px">{chg:+.1f}%</td>
              <td style="color:#8b949e;padding:10px 14px;text-align:right;font-size:13px">{dots} ({inv}/7)</td>
            </tr>"""
        dips_section = f"""
        <h3 style="color:#e6edf3;margin:28px 0 12px 0;font-size:16px">🔻 Top dipy dnes</h3>
        <table style="border-collapse:collapse;background:#161b22;border-radius:8px;width:100%;overflow:hidden">
          <tbody>{dip_rows}</tbody>
        </table>"""
    else:
        dips_section = """
        <div style="padding:20px;background:#161b22;border-radius:8px;text-align:center;color:#8b949e;margin:24px 0">
          Žádné výrazné poklesy (≥5 %). Klidný trh.
        </div>"""

    # HTML email
    html = f"""<html><body style="background:#0d1117;color:#e6edf3;font-family:sans-serif;padding:24px;margin:0">
    <div style="max-width:560px;margin:0 auto">
      <h2 style="color:#f0883e;margin:0 0 6px 0">🎯 PI Radar — {date_str}</h2>
      <p style="color:#8b949e;margin:0 0 24px 0;font-size:14px">Tvůj denní report je připravený.</p>

      <table style="border-collapse:separate;border-spacing:8px 0;width:100%;margin-bottom:8px">
        <tr>
          <td style="background:#161b22;border-radius:8px;padding:14px;text-align:center;width:33%">
            <div style="color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Dipy ≥5 %</div>
            <div style="color:#f85149;font-size:24px;font-weight:700;margin-top:4px">{len(dips)}</div>
          </td>
          <td style="background:#161b22;border-radius:8px;padding:14px;text-align:center;width:33%">
            <div style="color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Watchlist</div>
            <div style="color:#e6edf3;font-size:24px;font-weight:700;margin-top:4px">{dashboard_data.get("total_tickers", len(tickers))}</div>
          </td>
          <td style="background:#161b22;border-radius:8px;padding:14px;text-align:center;width:33%">
            <div style="color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Top conv.</div>
            <div style="color:#3fb950;font-size:18px;font-weight:700;margin-top:4px">{top_conv_symbol}</div>
            <div style="color:#8b949e;font-size:11px;margin-top:2px">{top_conv_value}%</div>
          </td>
        </tr>
      </table>

      {dips_section}

      <div style="text-align:center;margin:32px 0 20px 0">
        <a href="{DASHBOARD_URL}" style="display:inline-block;background:#238636;color:#fff;padding:14px 32px;border-radius:8px;text-decoration:none;font-weight:700;font-size:15px">
          Otevřít full report →
        </a>
      </div>

      <p style="color:#484f58;font-size:11px;text-align:center;margin:24px 0 0 0">
        Automaticky generováno PIRadar · @Ouzo83
      </p>
    </div>
    </body></html>"""

    # Plain text fallback
    plain_lines = [
        f"PI Radar - {date_str}",
        "",
        f"Dipy >=5%:      {len(dips)}",
        f"Watchlist:      {dashboard_data.get('total_tickers', len(tickers))} tickerů",
        f"Top conviction: {top_conv_symbol} ({top_conv_value}%)",
        "",
    ]
    if top_dips:
        plain_lines.append("Top dipy:")
        for t in top_dips:
            q   = t.get("quote", {}) or {}
            w   = t.get("watchlist_meta", {}) or {}
            plain_lines.append(
                f"  {t['symbol']:<8} {q.get('change_pct', 0):+.1f}%   ({w.get('investor_count', 0)}/7 PI)"
            )
        plain_lines.append("")
    plain_lines.append(f"Full report: {DASHBOARD_URL}")
    plain_text = "\n".join(plain_lines)

    # Odeslání
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = GMAIL_TO
    msg.attach(MIMEText(plain_text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.send_message(msg)
        log(f"  ✓ Notifikační email odeslán: {subject}")
    except Exception as e:
        log(f"  [CHYBA] Notifikační email: {e}")

# ── Claude analýza ────────────────────────────────────────────────────────────
def claude_analyze(shortlist: list[dict]) -> str:
    """Pošle shortlist podhodnocených akcií Claudovi a dostane narativní analýzu."""
    if not shortlist:
        return "Žádné výrazně podhodnocené akcie dnes nenalezeny."

    # Připravíme strukturovaný prompt
    tickers_info = []
    for t in shortlist[:10]:   # max 10 tickerů pro Claude
        q = t.get("quote", {}) or {}
        f = t.get("fundamentals", {}) or {}
        v = t.get("valuation", {}) or {}
        a = t.get("analyst", {}) or {}
        w = t.get("watchlist_meta", {})
        p = t.get("profile", {}) or {}

        tickers_info.append({
            "symbol":          t["symbol"],
            "name":            p.get("name", t["symbol"]),
            "sector":          p.get("sector", ""),
            "price":           q.get("price"),
            "change_pct_today": q.get("change_pct"),
            "high_52w":        q.get("high_52w"),
            "low_52w":         q.get("low_52w"),
            "pe_ttm":          f.get("pe_ttm"),
            "revenue_growth":  f.get("revenue_growth"),
            "net_margin":      f.get("net_margin"),
            "roe":             f.get("roe"),
            "fair_value":      v.get("fair_value"),
            "margin_of_safety_pct": v.get("margin_of_safety_pct"),
            "discount_to_target":   v.get("discount_to_target"),
            "analyst_consensus":    a.get("consensus"),
            "analyst_target_mean":  t.get("price_target", {}).get("target_mean"),
            "investor_count":  w.get("investor_count"),
            "investors":       w.get("investors", []),
            "conviction_pct":  w.get("conviction"),
            "recent_news":     t.get("news", []),
        })

    prompt = f"""Jsi investiční analytik specializující se na hodnotové investování.
Analyzuj níže uvedené akcie, které jsou v portfoliích 1–4 prověřených eToro Popular Investors
(Ouzo83, smudliczek, Ctalbot44, michalhla) a zároveň vykazují znaky podhodnocení nebo výrazného dne pohybu.

Dnešní datum: {datetime.now().strftime('%d.%m.%Y')}
Data:
{json.dumps(tickers_info, ensure_ascii=False, indent=2)}

Pro každý ticker poskytni STRUČNOU analýzu (max 4 věty) ve formátu:
**SYMBOL — Název firmy**
- Proč je zajímavá: [1–2 věty o fundamentech a conviction]
- Vstupní pásmo: [konkrétní cenové rozsahy s odůvodněním]
- Výstupní cíl: [první target a stop-loss]
- Riziko: [hlavní rizikový faktor]

Pravidla:
- Vycházej POUZE z poskytnutých dat, nevymýšlej statistiky
- Nevyjmenovávej názvy analytických firem pokud je nemáš v datech
- Margin of safety záporné = cena pod fair value = příležitost
- Buď konkrétní v číslech, vyhni se vágním formulacím
- Řaď od nejzajímavější příležitosti po méně zajímavé
- Pokud jsou k dispozici recent_news, VŽDY je použij pro vysvětlení proč akcie klesla/rostla
- Pokud news chybí nebo jsou prázdné, analyzuj pouze z čísel a to výslovně uveď
- Na konci přidej 2–3 věty celkové tržní summary

Odpovídej česky."""

    try:
        r = requests.post(
            f"{CLAUDE_BASE}/messages",
            headers={
                "x-api-key":         CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-20250514",
                "max_tokens": 2000,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"]
    except Exception as e:
        log(f"  [CHYBA] Claude API: {e}")
        return f"Claude analýza nedostupná: {e}"

# ── Hlavní sběr dat ───────────────────────────────────────────────────────────
def fetch_market_data(watchlist: list[dict], alert_only: bool = False) -> list[dict]:
    """Stáhne tržní data pro všechny tickery ve watchlistu."""
    results = []
    total   = len(watchlist)

    log(f"Stahuji data pro {total} tickerů...")

    for i, item in enumerate(watchlist, 1):
        symbol = item["symbol"]
        log(f"  [{i:3d}/{total}] {symbol}")

        # Quote (cena + denní změna) — vždy
        quote = get_quote(symbol)
        time.sleep(SLEEP_FINNHUB)

        if not quote:
            log(f"           → přeskočeno (bez dat)")
            continue

        entry = {
            "symbol":        symbol,
            "watchlist_meta": item,
            "quote":         quote,
            "fundamentals":  None,
            "analyst":       None,
            "price_target":  None,
            "profile":       None,
            "valuation":     None,
            "news":          [],
            "score":         0,
        }

        # V alert módu stahujeme jen cenu (šetříme API volání)
        if not alert_only:
            fundamentals = get_fundamentals(symbol)
            time.sleep(SLEEP_FINNHUB)

            analyst = get_analyst_rec(symbol)
            time.sleep(SLEEP_FINNHUB)

            profile = get_company_profile(symbol)
            time.sleep(SLEEP_FINNHUB)

            price_target = get_price_target(symbol)
            time.sleep(SLEEP_FINNHUB)

            news = get_company_news(symbol)
            time.sleep(SLEEP_FINNHUB)

            valuation = {}
            if quote.get("price") and fundamentals:
                valuation = estimate_fair_value(
                    price       = quote["price"],
                    fundamentals= fundamentals,
                    price_target= price_target,
                    sector      = profile.get("sector", ""),
                )

            entry.update({
                "fundamentals": fundamentals,
                "analyst":      analyst,
                "price_target": price_target,
                "profile":      profile,
                "valuation":    valuation,
                "news":         news,
            })

        entry["score"] = compute_score(entry)
        results.append(entry)

    return results

# ── Alert run (každých 30 minut) ──────────────────────────────────────────────
def run_alert_check(watchlist: list[dict]):
    log("═══ PIRadar: Alert Check ═══")
    data        = fetch_market_data(watchlist, alert_only=True)
    alert_state = load_alert_state()
    alerts      = []

    for t in data:
        q          = t.get("quote", {}) or {}
        change_pct = q.get("change_pct")
        if change_pct is None:
            continue

        is_alert = (change_pct <= ALERT_DIP_PCT or change_pct >= ALERT_SURGE_PCT)
        if is_alert and should_alert(t["symbol"], change_pct, alert_state):
            w = t.get("watchlist_meta", {})
            alerts.append({
                "symbol":      t["symbol"],
                "price":       q.get("price"),
                "change_pct":  change_pct,
                "investors":   w.get("investors", []),
                "conviction":  w.get("conviction", 0),
            })
            alert_state = mark_alerted(t["symbol"], alert_state)
            log(f"  🚨 ALERT: {t['symbol']} {change_pct:+.1f}%")

    save_alert_state(alert_state)

    if alerts:
        send_alert_email(alerts)
    else:
        log(f"  Žádný alert (pohyb > ±{abs(ALERT_DIP_PCT):.0f}% nenalezen)")

    log("═══ Hotovo ═══")

# ── Full report run (denně 22:00) ─────────────────────────────────────────────
def run_full_report(watchlist: list[dict]):
    log("═══ PIRadar: Full Report ═══")

    data = fetch_market_data(watchlist, alert_only=False)

    # Seřaď podle score
    data.sort(key=lambda x: -x["score"])

    # Shortlist pro Claude: conviction >= 2 PI A (výrazný dip NEBO výrazně pod fair value)
    shortlist = [
        t for t in data
        if (
            # Velký dip (>10%) → stačí 1 PI investor
            (t["watchlist_meta"].get("investor_count", 0) >= CONVICTION_MIN_DIP
             and ((t.get("quote", {}) or {}).get("change_pct") or 0) <= ALERT_DIP_PCT)
            or
            # Menší dip nebo podhodnocení → vyžadujeme 2+ PI investory
            (t["watchlist_meta"].get("investor_count", 0) >= CONVICTION_MIN
             and (
                 ((t.get("quote", {}) or {}).get("change_pct") or 0) <= SCREENER_DIP_PCT
                 or ((t.get("valuation", {}) or {}).get("margin_of_safety_pct") or 0) <= -10
             ))
        )
    ]

    log(f"\nShortlist pro Claude analýzu: {len(shortlist)} tickerů")

    claude_analysis = ""
    if shortlist:
        log("Volám Claude API pro analýzu...")
        claude_analysis = claude_analyze(shortlist)

    # ── Uložení dashboard dat ─────────────────────────────────────────────────
    DASHBOARD_FILE.parent.mkdir(exist_ok=True)

    # Serializovatelný výstup
    def clean(obj):
        if isinstance(obj, dict):
            return {k: clean(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [clean(v) for v in obj]
        return obj

    dashboard_data = {
        "generated_at":    datetime.now().isoformat(),
        "total_tickers":   len(data),
        "shortlist_count": len(shortlist),
        "claude_analysis": claude_analysis,
        "tickers":         clean(data[:50]),   # top 50 pro dashboard
        "alerts_today": [
            t for t in data
            if abs((t.get("quote", {}) or {}).get("change_pct", 0) or 0) >= abs(SCREENER_DIP_PCT)
        ],
    }

    DASHBOARD_FILE.write_text(
        json.dumps(dashboard_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"\n✓ Dashboard data uložena: {DASHBOARD_FILE}")

    # Odeslat notifikační email o hotovém reportu (non-blocking)
    try:
        send_report_ready_email(dashboard_data)
    except Exception as e:
        log(f"  [CHYBA] Notifikační email (non-blocking): {e}")

    # Shrnutí do terminálu
    print()
    log("═══ TOP 10 příležitosti ═══")
    for i, t in enumerate(data[:10], 1):
        q   = t.get("quote", {}) or {}
        v   = t.get("valuation", {}) or {}
        w   = t.get("watchlist_meta", {})
        chg = q.get("change_pct")
        mos = v.get("margin_of_safety_pct")
        sign= "+" if (chg or 0) > 0 else ""
        log(f"  {i:2d}. {t['symbol']:<8} score={t['score']:3d} | "
            f"dnes: {sign}{chg:.1f}% | "
            f"MoS: {mos:.1f}%" if mos else
            f"  {i:2d}. {t['symbol']:<8} score={t['score']:3d} | dnes: {sign}{(chg or 0):.1f}%")

    if claude_analysis:
        print()
        log("═══ Claude analýza ═══")
        print(claude_analysis)

    log("\n═══ Hotovo ═══")

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="PIRadar Dip Screener")
    parser.add_argument("--alert",  action="store_true", help="Jen alert check (rychlý)")
    parser.add_argument("--report", action="store_true", help="Plný denní report")
    args = parser.parse_args()

    watchlist = load_watchlist()
    log(f"Načteno {len(watchlist)} tickerů z watchlistu.")

    if args.alert:
        run_alert_check(watchlist)
    else:
        # Bez přepínače nebo s --report → plný report
        run_full_report(watchlist)


if __name__ == "__main__":
    main()
