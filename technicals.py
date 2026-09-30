"""
Gold Brain AI PRO - lightweight technical analysis (support/resistance + read).

For the selected symbol and timeframe it pulls a Yahoo price history, finds the
nearest and major support / resistance from swing pivots, reads the trend, and
writes a short, scenario-based note ("while it holds S-R expect ...; a break of
R opens ...; a break of S exposes ..."). Levels are anchored to the price the
dashboard shows (broker gold), so they line up with your chart.

NOT trading advice - an AI read only. Served via GET /technicals?symbol=&tf=
"""

from __future__ import annotations
import json
import time
import threading
import urllib.request
from datetime import datetime, timezone

import quotes_feed as QF
import translate as TR

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=%s"

# timeframe -> (yahoo range, interval, label)
TF = {
    "intraday": ("5d", "15m", "intraday (5d / 15m)"),
    "swing": ("3mo", "1d", "swing (3mo / daily)"),
    "position": ("2y", "1wk", "position (2y / weekly)"),
}

# Support/resistance are STRUCTURAL - they must stay put while price ticks, and
# only move when a new bar can form a new pivot. So the level set is computed
# once and cached per (symbol, tf); between refreshes we reuse the frozen levels
# and only update the live "now" price. TTL is sized to each timeframe's bar.
_LEVEL_TTL = {"intraday": 900, "swing": 21600, "position": 86400}  # 15m / 6h / 1d
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _dec(symbol):
    if symbol[:3] == "XAU":
        return 2
    if symbol == "XAGUSD":
        return 3
    if symbol == "BTCUSD":
        return 1
    if symbol == "ETHUSD":
        return 2
    if symbol[3:6] == "JPY":
        return 3
    return 5


# tf -> broker timeframe name (as written by the EA into GoldBrainAI_Bars.csv)
_TF_BROKER = {"intraday": "M15", "swing": "D1", "position": "W1"}
import os
_BARS_FILE = os.path.join(os.path.dirname(QF.MT5_QUOTE), "GoldBrainAI_Bars.csv")


def _broker_available(symbol, tf):
    """Cheap check (no parse): are fresh broker bars on disk for this symbol/tf?"""
    if symbol != "XAUUSD":
        return False
    name = _TF_BROKER.get(tf)
    try:
        return bool(name) and os.path.exists(_BARS_FILE) and \
               (time.time() - os.path.getmtime(_BARS_FILE) <= 900)
    except Exception:
        return False


def _broker_bars(symbol, tf):
    """Real broker OHLC that the EA publishes (XAUUSD only). Returns
    {high,low,close} in the broker's own price scale, or None if unavailable/stale."""
    if symbol != "XAUUSD":
        return None
    name = _TF_BROKER.get(tf)
    try:
        if not name or not os.path.exists(_BARS_FILE):
            return None
        if time.time() - os.path.getmtime(_BARS_FILE) > 900:   # stale (EA off) -> fall back
            return None
        H, L, C = [], [], []
        with open(_BARS_FILE, "r", encoding="ascii", errors="ignore") as fh:
            for ln in fh:
                p = ln.strip().split(",")     # name,open,high,low,close
                if len(p) != 5 or p[0] != name:
                    continue
                try:
                    H.append(float(p[2])); L.append(float(p[3])); C.append(float(p[4]))
                except ValueError:
                    continue
        return {"high": H, "low": L, "close": C} if len(C) >= 20 else None
    except Exception:
        return None


def _series(yahoo, rng, itv):
    url = _URL % (urllib.request.quote(yahoo), rng, itv)
    d = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=15).read())
    res = d["chart"]["result"][0]
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    cl = q.get("close") or []
    hi = q.get("high") or []
    lo = q.get("low") or []
    H, L, C = [], [], []
    for i in range(len(cl)):
        if cl[i] is None:
            continue
        C.append(cl[i])
        H.append(hi[i] if i < len(hi) and hi[i] is not None else cl[i])
        L.append(lo[i] if i < len(lo) and lo[i] is not None else cl[i])
    return {"high": H, "low": L, "close": C}


def _pivots(vals, k, want_high):
    """Swing pivots: local maxima (want_high) or minima over +/- k bars."""
    out = []
    for i in range(k, len(vals) - k):
        seg = vals[i - k:i + k + 1]
        if want_high and vals[i] == max(seg):
            out.append(vals[i])
        if (not want_high) and vals[i] == min(seg):
            out.append(vals[i])
    return out


def _zones(vals, tol):
    """Cluster nearby pivots into level zones -> [(level, touches)] so a level
    tested several times ranks above a lone micro-wiggle."""
    if not vals:
        return []
    s = sorted(vals)
    groups, cur = [], [s[0]]
    for v in s[1:]:
        if v - cur[-1] <= tol:
            cur.append(v)
        else:
            groups.append(cur); cur = [v]
    groups.append(cur)
    return [(sum(g) / len(g), len(g)) for g in groups]


def _live_price(symbol):
    return ((QF.FEED.snapshot().get("symbols", {}) or {}).get(symbol, {}) or {}).get("price")


def _compute_levels(symbol, tf):
    """Derive the STRUCTURAL level set once (cached by caller).

    Prefers REAL broker OHLC (published by the EA) so levels match the user's own
    chart; falls back to a market feed otherwise. Resistance is read from swing
    HIGHS and support from swing LOWS, then clustered into zones so R1/S1 are
    meaningful, multi-touch levels - not the nearest micro-wiggle."""
    rng_lbl, itv, tflabel = TF[tf]
    broker = _broker_bars(symbol, tf)
    if broker is not None:
        H, L, C = broker["high"], broker["low"], broker["close"]
        source = "broker"
    else:
        s = _series(QF.SYMS[symbol][0], rng_lbl, itv)
        H, L, C = s["high"], s["low"], s["close"]
        source = "feed"
    if len(C) < 20:
        raise ValueError("not enough data")

    dec = _dec(symbol)
    last_series = C[-1]
    disp = _live_price(symbol) or last_series
    # Broker bars are already in the user's price scale -> no shift; a 3rd-party
    # feed is aligned to the broker price with a basis captured once and frozen.
    shift = 0.0 if source == "broker" else (disp - last_series)

    hi_all = round(max(H) + shift, dec)
    lo_all = round(min(L) + shift, dec)
    span = max(H) - min(L)
    k = max(2, len(C) // 30)
    tol = max(disp * 0.0012, span * 0.012)      # merge levels this close together
    res_z = [(round(v + shift, dec), n) for v, n in _zones(_pivots(H, k, True), tol)]
    sup_z = [(round(v + shift, dec), n) for v, n in _zones(_pivots(L, k, False), tol)]

    anchor = disp
    gap = max(disp * 0.0008, span * 0.03)        # ignore trivial levels hugging price

    def pick(zones, above):
        if above:
            cand = [z for z in zones if z[0] > anchor + gap]
        else:
            cand = [z for z in zones if z[0] < anchor - gap]
        if not cand:
            return hi_all if above else lo_all
        strong = [z for z in cand if z[1] >= 2]  # prefer multi-touch levels
        pool = strong or cand
        return (min if above else max)(pool, key=lambda z: z[0])[0]

    R1 = pick(res_z, True)
    S1 = pick(sup_z, False)
    rng_size = max(1e-9, hi_all - lo_all)
    slope = C[-1] - C[max(0, len(C) - 20)]
    trend = "up" if slope > rng_size * 0.12 else "down" if slope < -rng_size * 0.12 else "sideways"
    return {"dec": dec, "tflabel": tflabel, "hi_all": hi_all, "lo_all": lo_all,
            "R1": R1, "S1": S1, "trend": trend, "shift": shift, "source": source,
            "last_series": last_series, "ts": time.time()}


def build(symbol="XAUUSD", tf="swing", lang="en"):
    symbol = (symbol or "XAUUSD").upper()
    tf = tf if tf in TF else "swing"
    if symbol not in QF.SYMS:
        return {"ok": False, "symbol": symbol, "tf": tf,
                "text": "No chart history is available for this instrument."}

    key = (symbol, tf)
    with _CACHE_LOCK:
        lv = _CACHE.get(key)
        fresh = lv and (time.time() - lv["ts"] < _LEVEL_TTL[tf])
        # Never keep serving 3rd-party feed levels once real broker bars exist -
        # upgrade immediately instead of waiting out the cache TTL.
        if fresh and lv.get("source") == "feed" and _broker_available(symbol, tf):
            fresh = False
        if not fresh:
            try:
                lv = _compute_levels(symbol, tf)
                _CACHE[key] = lv
            except Exception as exc:
                if lv is None:      # nothing cached to fall back on
                    return {"ok": False, "symbol": symbol, "tf": tf,
                            "text": "Chart data unavailable (%s)." % exc}
                # else: keep serving the last good level set

    dec, tflabel = lv["dec"], lv["tflabel"]
    hi_all, lo_all = lv["hi_all"], lv["lo_all"]
    R1, S1, trend = lv["R1"], lv["S1"], lv["trend"]
    # Only the live "now" price ticks; the structural levels above are frozen.
    cur = _live_price(symbol) or (lv["last_series"] + lv["shift"])

    def f(v):
        return "%.*f" % (dec, v)

    trword = {"up": "in an uptrend", "down": "in a downtrend", "sideways": "moving sideways"}[trend]
    hold = {"up": "the up-move likely continues toward resistance",
            "down": "pressure likely stays toward support",
            "sideways": "expect choppy, range-bound trade"}[trend]
    above = "fresh highs" if hi_all <= R1 * 1.001 else f(hi_all)
    below = "fresh lows" if lo_all >= S1 * 0.999 else f(lo_all)
    text = ("On the %s timeframe, %s is around %s, %s. It is holding between support %s and "
            "resistance %s. While it stays in that band, %s. A clean break above %s would open the "
            "way toward %s; a break below %s would expose %s."
            % (tflabel, symbol, f(cur), trword, f(S1), f(R1), hold, f(R1), above, f(S1), below))

    text = TR.to_lang(text, lang, "tech:%s:%s" % (symbol, tf))   # native AI translation (cached)
    return {"ok": True, "symbol": symbol, "tf": tf, "tf_label": tflabel, "dec": dec,
            "current": round(cur, dec), "support": S1, "resistance": R1,
            "major_support": lo_all, "major_resistance": hi_all,
            "trend": trend, "text": text, "source": lv.get("source", "feed"),
            "updated": datetime.now(timezone.utc).strftime("%H:%M:%SZ")}


if __name__ == "__main__":
    import sys
    print(build(sys.argv[1] if len(sys.argv) > 1 else "XAUUSD",
                sys.argv[2] if len(sys.argv) > 2 else "swing")["text"])
