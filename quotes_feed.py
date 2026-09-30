"""
Gold Brain AI PRO - live multi-symbol quotes.

Polls Yahoo Finance (public, no key) for the most-traded, most news-sensitive
instruments, plus the US Dollar Index as macro context:

    XAUUSD (Gold), EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF, XAGUSD (Silver)

Gold gets the highest-accuracy treatment: a live BROKER quote pushed from the
local MT5 (ingest) or read from the MT5 file takes priority over Yahoo's
futures. Everything else uses Yahoo spot FX (uniform across brokers).

Endpoints served from this feed:
    GET /quotes          -> all symbols' latest quote + DXY
    GET /series?symbol=  -> that symbol's dense 1-min intraday (reaction charts)
"""

from __future__ import annotations
import os
import json
import time
import threading
import urllib.request
from datetime import datetime, timezone

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1m&range=1d"
_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=%s&range=%s"
# candle timeframe CODE -> (yahoo interval, range) for non-gold / fallback.
_YMAP = {"M5": ("5m", "5d"), "M15": ("15m", "5d"), "M30": ("30m", "1mo"),
         "H1": ("60m", "1mo"), "H4": ("60m", "3mo"), "D1": ("1d", "6mo"), "W1": ("1wk", "2y")}
# code -> approx bar spacing in ms (to synthesize timestamps for broker bars)
_STEP = {"M5": 300000, "M15": 900000, "M30": 1800000, "H1": 3600000,
         "H4": 14400000, "D1": 86400000, "W1": 604800000}


def _ohlc_from(res):
    ts = res.get("timestamp") or []
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    o, h, l, c = q.get("open") or [], q.get("high") or [], q.get("low") or [], q.get("close") or []
    out = []
    for i, t in enumerate(ts):
        try:
            oo, hh, ll, cc = o[i], h[i], l[i], c[i]
            if None in (oo, hh, ll, cc):
                continue
            out.append([int(t) * 1000, round(float(oo), 5), round(float(hh), 5),
                        round(float(ll), 5), round(float(cc), 5)])
        except Exception:
            continue
    return out

# internal symbol -> (Yahoo ticker, decimals, display label)
SYMS = {
    "XAUUSD": ("GC=F", 2, "Gold"),
    "EURUSD": ("EURUSD=X", 5, "EUR/USD"),
    "GBPUSD": ("GBPUSD=X", 5, "GBP/USD"),
    "USDJPY": ("USDJPY=X", 3, "USD/JPY"),
    "AUDUSD": ("AUDUSD=X", 5, "AUD/USD"),
    "USDCAD": ("USDCAD=X", 5, "USD/CAD"),
    "USDCHF": ("USDCHF=X", 5, "USD/CHF"),
    "XAGUSD": ("SI=F", 3, "Silver"),
    "BTCUSD": ("BTC-USD", 1, "Bitcoin"),
    "ETHUSD": ("ETH-USD", 2, "Ethereum"),
}
DXY = "DX-Y.NYB"
REFRESH_SEC = 20

GOLD_HIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gold_hist.json")
SERIES_STEP = 45
SERIES_MAX = 1920   # ~24h of broker-priced samples @45s (covers a full day of events)
MT5_QUOTE = os.environ.get("GBAI_MT5_QUOTE",
    r"C:\Users\ziaal\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\MQL5\Files\GoldBrainAI_Quote.txt")
_BARS_FILE = os.environ.get("GBAI_MT5_BARS",
    r"C:\Users\ziaal\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\MQL5\Files\GoldBrainAI_Bars.csv")


def _get(url, timeout=12):
    return urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout).read().decode("utf-8", "ignore")


def _fetch(sym):
    d = json.loads(_get(_URL % urllib.request.quote(sym)))
    return d["chart"]["result"][0]


def _quote_from(res):
    m = res["meta"]
    price = m.get("regularMarketPrice")
    prev = m.get("chartPreviousClose") or m.get("previousClose")
    if price is None or prev is None:
        return None
    chg = price - prev
    pct = (chg / prev * 100.0) if prev else 0.0
    return {"price": round(price, 5), "prev": round(prev, 5),
            "chg": round(chg, 5), "pct": round(pct, 2), "source": "Yahoo"}


def _intraday_from(res):
    ts = res.get("timestamp") or []
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    cl = q.get("close") or []
    out = []
    for t, c in zip(ts, cl):
        if c is not None:
            out.append([int(t) * 1000, round(float(c), 5)])
    return out


def _mt5_gold():
    try:
        if not os.path.exists(MT5_QUOTE) or time.time() - os.path.getmtime(MT5_QUOTE) > 90:
            return None
        with open(MT5_QUOTE, "r", encoding="ascii", errors="ignore") as fh:
            p = fh.read().strip().split(",")
        if len(p) < 4:
            return None
        bid = float(p[0]); prev = float(p[3]); chg = bid - prev
        return {"price": round(bid, 3), "prev": round(prev, 3), "chg": round(chg, 3),
                "pct": round(chg / prev * 100.0, 2) if prev else 0.0,
                "source": "MT5 broker (spot)"}
    except Exception:
        return None


class QuotesFeed:
    def __init__(self):
        self._lock = threading.Lock()
        self._q = {}                    # symbol -> quote dict
        self._dxy = None
        self._intraday = []             # XAUUSD 1-min (primary, back-compat)
        self._icache = {}               # symbol -> (ts, intraday) on-demand cache
        self._series = self._load_series()
        self._pushed = None; self._pushed_ts = 0
        self._updated = ""
        self._thread = None

    # ---- broker push (gold) ----
    def ingest(self, d):
        try:
            bid = float(d["bid"]); prev = float(d.get("prevClose") or d.get("prev") or bid)
            chg = bid - prev
            with self._lock:
                self._pushed = {"price": round(bid, 3), "prev": round(prev, 3),
                                "chg": round(chg, 3), "pct": round(chg / prev * 100.0, 2) if prev else 0.0,
                                "source": "MT5 broker (live)", "dec": 2, "label": "Gold"}
                self._pushed_ts = time.time()
                # optional live broker order-flow (buy/sell aggression) pushed alongside
                fl = d.get("flow")
                if isinstance(fl, dict) and ("buy" in fl):
                    self._pushed_flow = fl
                    self._pushed_flow_ts = time.time()
            return True
        except Exception:
            return False

    def pushed_flow(self):
        """Last broker order-flow pushed from the local PC (fresh < 120s), else None."""
        try:
            if time.time() - getattr(self, "_pushed_flow_ts", 0) < 120:
                return getattr(self, "_pushed_flow", None)
        except Exception:
            pass
        return None

    # ---- series persistence (gold sampler) ----
    def _load_series(self):
        try:
            if os.path.exists(GOLD_HIST):
                with open(GOLD_HIST, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
                if isinstance(d, list):
                    return d[-SERIES_MAX:]
        except Exception:
            pass
        return []

    def _save_series(self):
        try:
            with open(GOLD_HIST, "w", encoding="utf-8") as fh:
                json.dump(self._series, fh)
        except Exception:
            pass

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            try:
                self.refresh()
            except Exception:
                pass
            time.sleep(REFRESH_SEC)

    def refresh(self):
        q = {}
        for sym, (yt, dec, lbl) in SYMS.items():
            try:
                res = _fetch(yt)
                quote = _quote_from(res)
                if quote:
                    quote["dec"] = dec; quote["label"] = lbl
                    q[sym] = quote
                if sym == "XAUUSD":
                    intr = _intraday_from(res)
                    if intr:
                        with self._lock:
                            self._intraday = intr
                            self._icache["XAUUSD"] = (time.time(), intr)
            except Exception:
                pass
        # Gold price accuracy: pushed broker > MT5 file > Yahoo futures.
        pushed = None
        if self._pushed and (time.time() - self._pushed_ts) < 90:
            pushed = dict(self._pushed)
        gm = _mt5_gold()
        if pushed or gm:
            best = pushed or gm
            best["dec"] = 2; best["label"] = "Gold"
            q["XAUUSD"] = best
        try:
            self._dxy = _quote_from(_fetch(DXY))
        except Exception:
            pass
        # Gold priced in EUR (XAUEUR) - derived from XAUUSD / EURUSD.
        if "XAUUSD" in q and "EURUSD" in q and q["XAUUSD"].get("price") and q["EURUSD"].get("price"):
            xu, eu = q["XAUUSD"], q["EURUSD"]
            pr = xu["price"] / eu["price"]
            pv = (xu.get("prev") or pr) / (eu.get("prev") or 1.0)
            chg = pr - pv
            q["XAUEUR"] = {"price": round(pr, 2), "prev": round(pv, 2), "chg": round(chg, 2),
                           "pct": round(chg / pv * 100.0, 2) if pv else 0.0,
                           "source": "derived (XAUUSD/EURUSD)", "dec": 2, "label": "Gold/EUR"}
        with self._lock:
            self._q = q
            self._updated = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
            g = q.get("XAUUSD")
            if g and g.get("price"):
                now_ms = int(time.time() * 1000)
                if not self._series or (now_ms - self._series[-1][0]) >= SERIES_STEP * 1000:
                    self._series.append([now_ms, g["price"]])
                    if len(self._series) > SERIES_MAX:
                        self._series = self._series[-SERIES_MAX:]
                    self._save_series()
        return self.snapshot()

    def gold_rich_series(self):
        """A dense, BROKER-priced gold price series with REAL timestamps for the
        event-reaction window: real broker M5 candle closes (~16h of history that
        survives redeploys, since the PC re-pushes them) MERGED with the live 45s
        sampler for fine detail near 'now'. All points are broker scale — no Yahoo
        mixing, no synthetic cliffs. 20s cache. Empty -> caller falls back to Yahoo."""
        try:
            cc = getattr(self, "_grs_cache", None)
            if cc and (time.time() - cc[0]) < 20:
                return cc[1]
            out = [[int(c[0]), float(c[4])] for c in self._broker_candles("M5")]
            last_t = out[-1][0] if out else 0
            with self._lock:
                live = list(self._series)
            for t, p in live:
                if int(t) > last_t:
                    out.append([int(t), float(p)])
            out.sort(key=lambda z: z[0])
            self._grs_cache = (time.time(), out)
            return out
        except Exception:
            with self._lock:
                return list(self._series)

    def series(self, symbol):
        """On-demand dense intraday for any symbol (60s cache). Gold uses the REAL
        broker-priced rich series (see gold_rich_series) so the event-reaction window
        matches MetaTrader, not Yahoo. Falls back to Yahoo only when there is no broker
        data at all (fresh instance / PC off)."""
        symbol = (symbol or "XAUUSD").upper()
        if symbol == "XAUUSD":
            rs = self.gold_rich_series()
            if len(rs) >= 2:
                return rs
        if symbol not in SYMS:
            return []
        c = self._icache.get(symbol)
        if c and (time.time() - c[0]) < 60:
            return c[1]
        try:
            intr = _intraday_from(_fetch(SYMS[symbol][0]))
            if intr:
                with self._lock:
                    self._icache[symbol] = (time.time(), intr)
            return intr
        except Exception:
            return c[1] if c else []

    def ingest_bars(self, data):
        """Accept BROKER OHLC bars pushed from the local PC (the EA writes real MT5
        bars to GoldBrainAI_Bars.csv). data = {"M15":[[o,h,l,c],...], "D1":[...], "W1":[...]}."""
        try:
            if isinstance(data, dict) and any(data.get(k) for k in
                    ("M5", "M15", "M30", "H1", "H4", "D1", "W1")):
                self._pushed_bars = data
                self._pushed_bars_ts = time.time()
                return True
        except Exception:
            pass
        return False

    def _broker_candles(self, tf):
        """Real broker gold candles for the chart (tf = M5/M15/M30/H1/H4/D1/W1).
        Reads the EA's local bars file; on the cloud uses whatever the PC pushed."""
        btf = tf if tf in _STEP else "M15"
        step_ms = _STEP[btf]
        rows = None
        # local: the EA's bars file. New format = TF,epoch_sec,o,h,l,c (6 cols, REAL time);
        # old format = TF,o,h,l,c (5 cols, no time -> synthesized below).
        try:
            if os.path.exists(_BARS_FILE) and time.time() - os.path.getmtime(_BARS_FILE) < 3600:
                rows = []
                with open(_BARS_FILE, "r", encoding="ascii", errors="ignore") as fh:
                    for ln in fh:
                        p = ln.strip().split(",")
                        if len(p) == 6 and p[0] == btf:
                            rows.append([int(float(p[1])) * 1000, float(p[2]), float(p[3]), float(p[4]), float(p[5])])
                        elif len(p) == 5 and p[0] == btf:
                            rows.append([float(p[1]), float(p[2]), float(p[3]), float(p[4])])
        except Exception:
            rows = None
        # cloud: pushed bars (rows may be [t,o,h,l,c] with real time, or legacy [o,h,l,c])
        if not rows:
            try:
                if time.time() - getattr(self, "_pushed_bars_ts", 0) < 3600:
                    rows = (getattr(self, "_pushed_bars", {}) or {}).get(btf)
            except Exception:
                rows = None
        if not rows:
            return []
        # Rows with a real timestamp (5 fields) are returned as-is; timeless rows
        # (4 fields) get synthesized timestamps ending "now".
        now = int(time.time() * 1000)
        n = len(rows)
        out = []
        for i, r in enumerate(rows):
            if len(r) >= 5:
                out.append([int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])])
            else:
                out.append([now - (n - 1 - i) * step_ms, float(r[0]), float(r[1]), float(r[2]), float(r[3])])
        return out

    def ohlc(self, symbol, tf="M15"):
        """OHLC candles at a timeframe code (M5..W1). XAUUSD = REAL BROKER bars (MT5);
        other symbols = Yahoo. Cached (60s for <=M15, else 15m)."""
        symbol = (symbol or "XAUUSD").upper()
        tf = tf if tf in _YMAP else "M15"
        if symbol == "XAUUSD":
            bc = self._broker_candles(tf)
            if bc:
                return {"candles": bc[-260:], "source": "broker"}
        if symbol not in SYMS:
            return {"candles": [], "source": "none"}
        if not hasattr(self, "_ocache"):
            self._ocache = {}
        key = symbol + "|" + tf
        cc = self._ocache.get(key)
        ttl = 60 if tf in ("M5", "M15") else 900
        if cc and (time.time() - cc[0]) < ttl:
            return {"candles": cc[1], "source": "feed"}
        try:
            iv, rng = _YMAP[tf]
            res = json.loads(_get(_CHART % (urllib.request.quote(SYMS[symbol][0]), iv, rng)))
            bars = _ohlc_from(res["chart"]["result"][0])[-260:]
            if bars:
                with self._lock:
                    self._ocache[key] = (time.time(), bars)
            return {"candles": bars, "source": "feed"}
        except Exception:
            return {"candles": cc[1] if cc else [], "source": "feed"}

    def snapshot(self):
        rich = self.gold_rich_series()   # broker-priced, real timestamps (for the reaction window)
        with self._lock:
            return {"ok": bool(self._q), "symbols": dict(self._q), "dxy": self._dxy,
                    "gold": self._q.get("XAUUSD"),          # back-compat
                    "gold_intraday": list(self._intraday),   # back-compat (XAUUSD, Yahoo)
                    "gold_series": rich if len(rich) >= 2 else list(self._series),
                    "updated": self._updated}


FEED = QuotesFeed()

if __name__ == "__main__":
    FEED.refresh()
    s = FEED.snapshot()
    for k, v in s["symbols"].items():
        print("%-7s %-16s %s (%+.2f%%)" % (k, v["label"], v["price"], v["pct"]))
