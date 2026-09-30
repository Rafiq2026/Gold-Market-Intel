"""
Gold Brain AI PRO - cross-asset market heatmap (Finviz-style, legal source).

A one-glance snapshot of the assets that DRIVE gold: the US dollar, real/nominal
yields, equities (risk sentiment), oil, and crypto - each with today's % change.
Data comes from the Yahoo chart API (the same permitted feed we already use), so
there is no scraping of a paywalled/ToS-restricted site.

Served via GET /heatmap.
"""

from __future__ import annotations
import json
import time
import threading
import urllib.request

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=5d&interval=1d"

# display name, yahoo ticker, and how a RISE in it usually leans for gold
BASKET = [
    ("Gold",        "GC=F",       "self"),
    ("US Dollar",   "DX-Y.NYB",   "bearish"),   # strong USD -> gold headwind
    ("US 10Y Yield", "^TNX",      "bearish"),   # higher yields -> gold headwind
    ("S&P 500",     "^GSPC",      "bearish"),   # risk-on -> mild gold headwind
    ("Crude Oil",   "CL=F",       "neutral"),
    ("Silver",      "SI=F",       "bullish"),   # moves with gold
    ("Bitcoin",     "BTC-USD",    "neutral"),
]
_REFRESH = 300  # seconds


def _pct(ticker):
    url = _URL % urllib.request.quote(ticker)
    d = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=12).read())
    res = d["chart"]["result"][0]
    cl = [c for c in ((res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []) if c is not None]
    # Use the last two DAILY closes from the same series (today's live bar vs
    # yesterday's close) - consistent across tickers, unlike meta.chartPreviousClose
    # which is sometimes stale and produced absurd one-day moves.
    if len(cl) < 2:
        raise ValueError("no data")
    meta = res.get("meta", {})
    last = meta.get("regularMarketPrice") or cl[-1]
    prev = cl[-2]
    if not prev:
        raise ValueError("no prev")
    return last, (last - prev) / prev * 100.0


# friendly name -> Yahoo ticker, so users can type "gold" / "sp500" / "eurusd"
_ALIAS = {
    "GOLD": "GC=F", "XAUUSD": "GC=F", "SILVER": "SI=F", "XAGUSD": "SI=F",
    "OIL": "CL=F", "CRUDE": "CL=F", "WTI": "CL=F", "BRENT": "BZ=F", "NATGAS": "NG=F",
    "DXY": "DX-Y.NYB", "DOLLAR": "DX-Y.NYB", "USDX": "DX-Y.NYB",
    "SP500": "^GSPC", "SPX": "^GSPC", "S&P": "^GSPC", "SP": "^GSPC",
    "NASDAQ": "^IXIC", "NAS": "^IXIC", "DOW": "^DJI", "DAX": "^GDAXI",
    "VIX": "^VIX", "10Y": "^TNX", "YIELD": "^TNX", "US10Y": "^TNX", "2Y": "^IRX",
    "BTC": "BTC-USD", "BITCOIN": "BTC-USD", "BTCUSD": "BTC-USD",
    "ETH": "ETH-USD", "ETHUSD": "ETH-USD", "ETHEREUM": "ETH-USD",
}
_FX = {"EUR", "GBP", "USD", "JPY", "AUD", "CAD", "CHF", "NZD"}


def resolve(q):
    """Map a user query to a Yahoo ticker + display name."""
    s = (q or "").strip().upper()
    if not s:
        return None, None
    if s in _ALIAS:
        return _ALIAS[s], s.title()
    # a 6-letter FX pair like EURUSD / USDJPY -> EURUSD=X
    if len(s) == 6 and s[:3] in _FX and s[3:] in _FX:
        return s + "=X", s[:3] + "/" + s[3:]
    # otherwise treat it as a raw ticker (AAPL, TSLA, ^TNX, GC=F, BTC-USD)
    return s, s


def quote_any(q):
    ticker, name = resolve(q)
    if not ticker:
        return {"ok": False, "error": "empty query"}
    try:
        price, pct = _pct(ticker)
        return {"ok": True, "name": name, "ticker": ticker,
                "price": round(price, 4), "pct": round(pct, 2)}
    except Exception as exc:
        return {"ok": False, "error": "not found (%s)" % (str(exc)[:60]), "query": q}


class HeatmapFeed:
    def __init__(self):
        self._lock = threading.Lock()
        self._rows = []
        self._ts = 0
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            try:
                self._fetch()
            except Exception:
                pass
            time.sleep(_REFRESH)

    def _fetch(self):
        rows = []
        for name, tk, lean in BASKET:
            try:
                price, pct = _pct(tk)
                rows.append({"name": name, "ticker": tk, "price": round(price, 4),
                             "pct": round(pct, 2), "gold_lean": lean})
            except Exception:
                continue
        with self._lock:
            if rows:
                self._rows = rows
                self._ts = time.time()

    # Gold/Silver/BTC should match the REST of the dashboard (the broker/live
    # quote), not Yahoo's GC=F futures which sits ~$40-50 above spot. Applied at
    # READ time so it always reflects the current broker price, not a 5-min cache.
    def _apply_broker(self, rows):
        try:
            import quotes_feed as QF
            qs = (QF.FEED.snapshot().get("symbols") or {})
        except Exception:
            return rows
        ovr = {"Gold": "XAUUSD", "Silver": "XAGUSD", "Bitcoin": "BTCUSD"}
        out = []
        for r in rows:
            sym = ovr.get(r.get("name"))
            q = qs.get(sym) if sym else None
            if q and q.get("price"):
                r = dict(r)
                r["price"] = round(float(q["price"]), 4)
                if q.get("pct") is not None:
                    r["pct"] = round(float(q["pct"]), 2)
            out.append(r)
        return out

    def _gold_read(self, rows):
        """One-line macro read for gold from the cross-asset moves."""
        by = {r["name"]: r["pct"] for r in rows}
        score = 0.0
        if "US Dollar" in by:
            score -= by["US Dollar"] * 1.2      # dollar down -> gold up
        if "US 10Y Yield" in by:
            score -= by["US 10Y Yield"] * 0.6   # yields down -> gold up
        if "S&P 500" in by:
            score -= by["S&P 500"] * 0.2        # risk-off -> gold up
        lean = "supportive" if score > 0.15 else "a headwind" if score < -0.15 else "mixed"
        drivers = []
        if "US Dollar" in by:
            drivers.append("USD %+.2f%%" % by["US Dollar"])
        if "US 10Y Yield" in by:
            drivers.append("10Y %+.2f%%" % by["US 10Y Yield"])
        return "Cross-asset backdrop is %s for gold today (%s)." % (lean, ", ".join(drivers))

    def snapshot(self):
        with self._lock:
            rows = list(self._rows)
            fresh = (time.time() - self._ts) < (_REFRESH * 3)
        if not rows:
            return {"ok": False, "rows": [], "read": "market heatmap loading..."}
        rows = self._apply_broker(rows)     # gold/silver/BTC = live broker price
        return {"ok": fresh, "rows": rows, "read": self._gold_read(rows),
                "updated": int(self._ts)}


FEED = HeatmapFeed()
