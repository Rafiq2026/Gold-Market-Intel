"""
Gold Brain AI PRO - live news feed.

Runs in a BACKGROUND thread inside ai_server.py and keeps a cached snapshot of:
  * Scheduled economic events (Forex Factory weekly calendar, no API key) ->
    a hard "blocked" flag around high-impact releases (FOMC/CPI/NFP/rates...).
  * Breaking political / geopolitical headlines (Google News + CNBC RSS,
    no API key) -> a 0..1 geopolitical risk score and a safe-haven bias for
    gold (risk-off headlines lift gold).

The snapshot is served instantly to the EA via /predict and /news, so the
EA's WebRequest never waits on the slow external fetch.

All network access is wrapped in try/except: if a feed is down the module
degrades to "not blocked / zero risk" rather than failing.
"""

from __future__ import annotations
import os
import threading
import time
import re
import json
import urllib.request
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import sentiment as SENT

# ------------------------------------------------------------------ config ---
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Google News RSS search is key-free and query-driven; CNBC is a fallback.
NEWS_RSS = [
    ("https://news.google.com/rss/search?q=" +
     quote("(gold OR \"central bank\" OR sanctions OR war OR strike OR "
           "tariffs OR Fed OR inflation OR conflict) when:1d") +
     "&hl=en-US&gl=US&ceid=US:en"),
    # Breaking geopolitics / military / attacks (biggest, fastest market movers).
    ("https://news.google.com/rss/search?q=" +
     quote("(attack OR missile OR airstrike OR \"military strike\" OR invasion OR "
           "escalation OR \"breaking\" OR geopolitical OR Israel OR Iran OR Russia OR "
           "Ukraine OR China) when:1d") +
     "&hl=en-US&gl=US&ceid=US:en"),
    # US macro / Fed / data that move gold intraday.
    ("https://news.google.com/rss/search?q=" +
     quote("(Fed OR Powell OR FOMC OR CPI OR inflation OR jobs OR \"interest rate\" OR "
           "tariffs OR \"US economy\" OR Treasury OR dollar) when:1d") +
     "&hl=en-US&gl=US&ceid=US:en"),
    "https://www.cnbc.com/id/100727362/device/rss/rss.html",
]

# --- OPTIONAL professional news API (activated only if a key is present) ---
#   Set these environment variables before starting the server, e.g.:
#     set GBAI_NEWS_PROVIDER=marketaux
#     set GBAI_NEWSAPI_KEY=your_key_here
#   Supported providers: marketaux | newsapi | finnhub.  Defaults to free RSS.
NEWS_PROVIDER = os.environ.get("GBAI_NEWS_PROVIDER", "rss").lower()
NEWS_API_KEY = os.environ.get("GBAI_NEWSAPI_KEY", "").strip()

REFRESH_SEC = 300                 # re-fetch every 5 minutes
BLOCK_BEFORE_MIN = 30             # block window before a high-impact event
BLOCK_AFTER_MIN = 30             # block window after
# Tier-1 US events (Fed / jobs / inflation) get a wider protective window.
TIER1_BEFORE_MIN = 60
TIER1_AFTER_MIN = 45
TIER1_KEYWORDS = ("fomc", "federal funds", "rate decision", "interest rate",
                  "non-farm", "nonfarm", "nfp", "cpi", "consumer price",
                  "pce", "core pce", "ppi", "producer price", "ism",
                  "powell", "fed chair", "gdp")
BLOCK_CURRENCIES = {"USD", "EUR", "GBP", "ALL"}   # gold-relevant
BLOCK_IMPACTS = {"High"}          # which impacts trigger a hard block

# Geopolitical / macro risk keywords -> weight. Risk-off is bullish for gold.
RISK_KEYWORDS = {
    "war": 3, "invasion": 3, "missile": 3, "airstrike": 3, "strike": 2,
    "attack": 3, "nuclear": 3, "sanction": 2, "sanctions": 2, "conflict": 2,
    "escalat": 2, "tension": 1, "terror": 3, "tariff": 2, "trade war": 3,
    "shutdown": 2, "default": 2, "crisis": 2, "recession": 2, "geopolit": 2,
    "safe haven": 2, "safe-haven": 2, "hike": 1, "cut rates": 1,
}
# De-escalation lowers gold's safe-haven pull.
CALM_KEYWORDS = {
    "ceasefire": 3, "truce": 3, "peace deal": 3, "de-escalat": 2,
    "agreement": 1, "resolved": 1, "deal reached": 2, "diplomacy": 1,
}

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}

# Categorise risk headlines so the dashboard can show WHAT is driving geo risk.
_GEO_CATS = {
    "war/military": ("war", "invasion", "missile", "airstrike", "strike", "attack",
                     "nuclear", "troops", "military", "drone", "bombard", "offensive"),
    "sanctions": ("sanction", "embargo", "export ban", "blacklist", "asset freeze"),
    "central bank": ("fed", "fomc", "powell", "rate", "ecb", "boe", "central bank",
                     "hike", "cut", "hawkish", "dovish", "yield"),
    "trade/tariffs": ("tariff", "trade war", "trade deal", "import", "export control"),
    "energy": ("oil", "opec", "gas", "crude", "pipeline"),
}


def _categorize(headlines):
    tally = {k: 0 for k in _GEO_CATS}
    for h in headlines:
        low = h.lower()
        for cat, kws in _GEO_CATS.items():
            if any(k in low for k in kws):
                tally[cat] += 1
    top = sorted([(v, k) for k, v in tally.items() if v > 0], reverse=True)
    return tally, (top[0][1] if top else "")


def _headline_cat(low):
    for cat, kws in _GEO_CATS.items():
        if any(k in low for k in kws):
            return cat
    return ""


def _impact_headlines(headlines, limit=6):
    """Rank the raw headlines by market impact (geopolitical risk + macro weight) and
    return the top ones as {title, cat, lean} for the live geopolitics feed + the AI.
    lean = gold direction: 'up' (risk-off/safe-haven), 'down' (de-escalation), '' neutral."""
    out, seen = [], set()
    scored = []
    for h in headlines:
        if not h or len(h) < 12:
            continue
        low = h.lower()
        score = 0
        for kw, w in RISK_KEYWORDS.items():
            if kw in low:
                score += w
        calm = any(k in low for k in CALM_KEYWORDS)
        if calm:
            score += 3                      # de-escalation is also high-impact (fades gold)
        if any(k in low for k in TIER1_KEYWORDS):
            score += 3                      # Fed / CPI / jobs = big intraday movers
        if score <= 0:
            continue
        cat = _headline_cat(low)
        # Direction for gold: central-bank items are ambiguous -> neutral unless clearly
        # dovish/hawkish; risk events lift gold; de-escalation fades it.
        if calm:
            lean = "down"
        elif cat == "central bank":
            lean = "up" if ("cut" in low or "dovish" in low) else \
                   "down" if ("hike" in low or "hawkish" in low) else ""
        elif score >= 2:
            lean = "up"
        else:
            lean = ""
        scored.append((score, h, cat, lean))
    scored.sort(key=lambda z: z[0], reverse=True)
    for score, h, cat, lean in scored:
        key = h.lower()[:60]
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": h[:160], "cat": cat, "lean": lean})
        if len(out) >= limit:
            break
    return out


def _get(url: str, timeout: int = 12) -> bytes:
    req = urllib.request.Request(url, headers=_UA)
    return urllib.request.urlopen(req, timeout=timeout).read()


class NewsFeed:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = self._empty()
        self._thread = None

    def _empty(self) -> dict:
        return {
            "ok": False,
            "econ_blocked": False,
            "next_event": "",
            "next_currency": "",
            "next_impact": "",
            "next_minutes": -1,
            "geo_risk": 0.0,        # 0..1
            "geo_bias": 0.0,        # -1..+1 gold safe-haven direction
            "fear": 0.0,           # sentiment: world risk-off intensity
            "gold_direct": 0.0,    # sentiment: gold-specific tone
            "sentiment_backend": "",
            "provider": NEWS_PROVIDER if NEWS_API_KEY else "rss",
            "headline": "",
            "geo_categories": {},
            "geo_top": "",
            "geo_headlines": [],
            "updated": "",
        }

    # ---------------------------------------------------------- lifecycle ---
    def start(self):
        # Refresh happens INSIDE the background thread so a slow/blocked news
        # fetch never delays the HTTP server from binding and serving.
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            try:
                self.refresh()
            except Exception:
                pass
            time.sleep(REFRESH_SEC)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    # ------------------------------------------------------------ fetches ---
    def _fetch_calendar(self, now: datetime):
        blocked = False
        next_evt = ("", "", "", -1)   # title, currency, impact, minutes
        try:
            data = json.loads(_get(CALENDAR_URL).decode("utf-8", "ignore"))
            # Share the raw calendar so events_feed reuses it (one download,
            # avoids hitting the calendar host twice -> no 429 rate-limiting).
            try:
                with open("calendar_cache.json", "w", encoding="utf-8") as fh:
                    json.dump({"ts": now.isoformat(), "data": data}, fh)
            except Exception:
                pass
        except Exception:
            return blocked, next_evt, False

        best_future = None
        for ev in data:
            impact = str(ev.get("impact", ""))
            cur = str(ev.get("country", ""))
            if impact not in BLOCK_IMPACTS:
                continue
            if cur not in BLOCK_CURRENCIES:
                continue
            raw = ev.get("date", "")
            try:
                et = datetime.fromisoformat(raw)
                if et.tzinfo is None:
                    et = et.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            delta_min = (et - now).total_seconds() / 60.0
            # Tier-1 US events (Fed / jobs / inflation) get a wider window.
            title_low = str(ev.get("title", "")).lower()
            tier1 = (cur == "USD") and any(k in title_low for k in TIER1_KEYWORDS)
            before = TIER1_BEFORE_MIN if tier1 else BLOCK_BEFORE_MIN
            after = TIER1_AFTER_MIN if tier1 else BLOCK_AFTER_MIN
            if -after <= delta_min <= before:
                blocked = True
            # Track the nearest upcoming high-impact event.
            if delta_min >= -BLOCK_AFTER_MIN:
                if best_future is None or delta_min < best_future[3]:
                    best_future = (str(ev.get("title", "")), cur, impact, delta_min)
        if best_future is not None:
            next_evt = (best_future[0], best_future[1], best_future[2],
                        int(round(best_future[3])))
        return blocked, next_evt, True

    def _fetch_pro(self):
        """Optional professional provider (only if an API key is configured)."""
        if not NEWS_API_KEY:
            return []
        try:
            if NEWS_PROVIDER == "newsapi":
                url = ("https://newsapi.org/v2/everything?q=gold%20OR%20geopolitics"
                       "&language=en&sortBy=publishedAt&pageSize=40&apiKey=" + NEWS_API_KEY)
                j = json.loads(_get(url))
                return [a.get("title", "") for a in j.get("articles", [])]
            if NEWS_PROVIDER == "marketaux":
                url = ("https://api.marketaux.com/v1/news/all?language=en"
                       "&filter_entities=true&search=gold+OR+geopolitics&api_token=" + NEWS_API_KEY)
                j = json.loads(_get(url))
                return [a.get("title", "") for a in j.get("data", [])]
            if NEWS_PROVIDER == "finnhub":
                url = "https://finnhub.io/api/v1/news?category=general&token=" + NEWS_API_KEY
                j = json.loads(_get(url))
                return [a.get("headline", "") for a in j][:40]
        except Exception:
            return []
        return []

    def _fetch_headlines(self):
        titles = []
        ok = False
        # Professional provider first (higher quality) if a key is set.
        pro = self._fetch_pro()
        if pro:
            titles.extend(pro)
            ok = True
        for url in NEWS_RSS:
            try:
                raw = _get(url).decode("utf-8", "ignore")
                found = re.findall(r"<title>(.*?)</title>", raw, re.S)
                # first <title> is the feed name; skip it
                titles.extend(t for t in found[1:])
                ok = True
            except Exception:
                continue
        # clean CDATA / entities / tags
        clean = []
        for t in titles[:60]:
            t = re.sub(r"<!\[CDATA\[|\]\]>", "", t)
            t = re.sub(r"<.*?>", "", t)
            t = re.sub(r"&[a-z#0-9]+;", " ", t)
            clean.append(t.strip())
        return clean, ok

    def _score_geo(self, headlines):
        risk_raw = 0.0
        calm_raw = 0.0
        top = ""
        top_w = 0
        for h in headlines:
            low = h.lower()
            hw = 0
            for kw, w in RISK_KEYWORDS.items():
                if kw in low:
                    risk_raw += w
                    hw += w
            for kw, w in CALM_KEYWORDS.items():
                if kw in low:
                    calm_raw += w
            if hw > top_w:
                top_w = hw
                top = h
        kw_risk = max(0.0, min(1.0, risk_raw / 12.0))
        calm = max(0.0, min(1.0, calm_raw / 8.0))

        # --- sentiment model (VADER / lexicon) ---
        sent = SENT.score(headlines)
        fear = sent["fear"]                 # 0..1 world risk-off intensity
        gold_direct = sent["gold_direct"]   # -1..+1 gold-specific tone

        # Blend keyword risk with model fear for a robust risk magnitude.
        risk_off = max(0.0, min(1.0, 0.5 * kw_risk + 0.5 * fear))
        # Gold safe-haven bias: risk-off lifts gold, de-escalation fades it,
        # and headlines specifically about gold pull in their own direction.
        bias = max(-1.0, min(1.0, risk_off - 0.6 * calm + 0.5 * gold_direct))
        risk = max(0.0, min(1.0, max(risk_off, abs(gold_direct))))
        return risk, bias, top, sent

    # ------------------------------------------------------------- refresh ---
    def refresh(self):
        now = datetime.now(timezone.utc)
        econ_blocked, (title, cur, impact, minutes), cal_ok = self._fetch_calendar(now)
        headlines, news_ok = self._fetch_headlines()
        geo_risk, geo_bias, top, sent = self._score_geo(headlines)
        geo_cats, geo_top = _categorize(headlines)
        geo_headlines = _impact_headlines(headlines)

        state = {
            "ok": cal_ok or news_ok,
            "econ_blocked": bool(econ_blocked),
            "next_event": title,
            "next_currency": cur,
            "next_impact": impact,
            "next_minutes": minutes,
            "geo_risk": round(geo_risk, 3),
            "geo_bias": round(geo_bias, 3),
            "fear": sent["fear"],
            "gold_direct": sent["gold_direct"],
            "sentiment_backend": sent["backend"],
            "provider": NEWS_PROVIDER if NEWS_API_KEY else "rss",
            "headline": top[:120],
            "geo_categories": geo_cats,
            "geo_top": geo_top,
            "geo_headlines": geo_headlines,
            "updated": now.strftime("%Y-%m-%d %H:%M:%SZ"),
        }
        with self._lock:
            self._state = state
        return state


# Module-level singleton used by the server.
FEED = NewsFeed()


if __name__ == "__main__":
    # Manual smoke test.
    FEED.refresh()
    import pprint
    pprint.pprint(FEED.snapshot())
