"""
Gold Brain AI PRO - local -> cloud broker-quote pusher.

Reads the live BROKER quote that the EA writes to the MT5 Files folder and
POSTs it to the cloud dashboard, so the cloud site shows the SAME gold price
as your MetaTrader (while this PC is on). When this stops, the cloud falls
back to a public gold feed automatically.

Usage (Windows, keep the window open):
    set GBAI_CLOUD_URL=https://gold-market-intel.onrender.com
    set GBAI_INGEST_TOKEN=your-secret
    python push_quote.py

Or pass the URL as an argument:
    python push_quote.py https://gold-market-intel.onrender.com your-secret
"""

from __future__ import annotations
import os
import sys
import json
import time
import urllib.request
from datetime import datetime, timezone, timedelta

MT5_QUOTE = os.environ.get("GBAI_MT5_QUOTE",
    r"C:\Users\ziaal\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\MQL5\Files\GoldBrainAI_Quote.txt")
MT5_FLOW = os.environ.get("GBAI_MT5_FLOW",
    r"C:\Users\ziaal\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\MQL5\Files\GoldBrainAI_Flow.txt")
# calendar the LOCAL server already downloaded (residential IP) -> push to the cloud
CAL_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calendar_cache.json")
# MT5's own calendar (WITH actuals), exported by CalendarExport.mq5 -> merged into the
# pushed calendar so the CLOUD can show released figures too (it can't read this file).
MT5_CAL = os.environ.get("GBAI_MT5_CAL",
    r"C:\Users\ziaal\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\MQL5\Files\hayula_calendar.json")
# real broker OHLC bars the EA writes -> push to the cloud for the live candle chart
MT5_BARS = os.environ.get("GBAI_MT5_BARS",
    r"C:\Users\ziaal\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\MQL5\Files\GoldBrainAI_Bars.csv")
CLOUD_URL = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GBAI_CLOUD_URL", "")).rstrip("/")
TOKEN = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("GBAI_INGEST_TOKEN", "")
INTERVAL = 5


def read_flow():
    """Optional: the EA's live buy/sell order-flow, so the cloud shows the bar too."""
    try:
        if time.time() - os.path.getmtime(MT5_FLOW) > 120:
            return None
        with open(MT5_FLOW, "r", encoding="ascii", errors="ignore") as fh:
            p = fh.read().strip().split(",")
        if len(p) < 7:
            return None
        return {"buy": float(p[0]), "sell": float(p[1]), "dom_active": p[2] == "1",
                "dom_imb": float(p[3]), "spread_pts": float(p[4]),
                "thin": p[5] == "1", "vacuum": p[6] == "1",
                "near_zone": p[7] if len(p) > 7 else ""}
    except Exception:
        return None


def read_quote():
    with open(MT5_QUOTE, "r", encoding="ascii", errors="ignore") as fh:
        p = fh.read().strip().split(",")
    q = {"symbol": "XAUUSD", "bid": float(p[0]), "ask": float(p[1]),
         "prevClose": float(p[3]) if len(p) > 3 else float(p[0]), "token": TOKEN}
    fl = read_flow()
    if fl:
        q["flow"] = fl
    return q


def _norm(s):
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def _load_mt5_actuals():
    """Released events (with actuals) from MT5's calendar export. [] if missing."""
    try:
        with open(MT5_CAL, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return []
    out = []
    for e in d:
        if not str(e.get("actual", "")).strip():
            continue
        out.append({"cur": e.get("currency", ""), "norm": _norm(e.get("event", "")),
                    "day": str(e.get("time", ""))[:10].replace(".", "-"), "raw": e})
    return out


_MT5_OK = None

def _m1_bars_server():
    """Recent M1 bars as (server_dt, high, low, close). [] if MT5 unavailable. The bar
    time and the MT5 calendar event time share the broker-server clock, so they align
    without any timezone conversion."""
    global _MT5_OK
    try:
        import MetaTrader5 as mt5
        if _MT5_OK is None:
            _MT5_OK = bool(mt5.initialize())
        if not _MT5_OK:
            return []
        r = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_M1, 0, 8000)   # ~5.5 days
        if r is None:
            return []
        return [(datetime.fromtimestamp(int(b["time"]), tz=timezone.utc),
                 float(b["high"]), float(b["low"]), float(b["close"])) for b in r]
    except Exception:
        return []


def _reaction(bars, ev_time_str, win_min=5):
    """Gold's REAL isolated reaction in the first `win_min` min after the release, from
    real M1 OHLC - captures the violent intra-candle spike (e.g. the 40-dollar CPI
    candle) via candle high/low, not just close. ev_time_str = MT5 cal 'YYYY.MM.DD HH:MM'."""
    try:
        et = datetime.strptime(str(ev_time_str)[:16], "%Y.%m.%d %H:%M").replace(tzinfo=timezone.utc)
    except Exception:
        return None
    end = et + timedelta(minutes=win_min)
    pre = None; hi = None; lo = None; last = None
    for (bt, h, l, c) in bars:
        if bt < et:
            pre = c
        elif bt <= end:
            hi = h if hi is None else max(hi, h)
            lo = l if lo is None else min(lo, l)
            last = c
    if pre is None or hi is None:
        return None
    return {"pre": round(pre, 2), "net": round(last - pre, 2),
            "pop": round(hi - pre, 2), "drop": round(lo - pre, 2), "win_min": win_min}


def _enrich_actuals(data):
    """Merge MT5 actuals into the calendar rows (match by currency + title + ~day) and,
    for released events, attach the REAL measured gold reaction - so the cloud shows the
    released figure AND what gold actually did, not a forecast-based guess."""
    cal = _load_mt5_actuals()
    if not cal:
        return data
    bars = _m1_bars_server()
    n = 0; rx_n = 0
    for row in data:
        try:
            et = datetime.fromisoformat(str(row.get("date", "")))
        except Exception:
            continue
        nt = _norm(row.get("title", "")); cur = row.get("country", "")
        best = None; bestscore = -1
        for m in cal:
            if m["cur"] != cur:
                continue
            mn = m["norm"]
            if not (mn == nt or (mn and mn in nt) or (nt and nt in mn)):
                continue
            try:
                dday = abs((datetime.fromisoformat(m["day"]).date() - et.date()).days)
            except Exception:
                dday = 0
            if dday > 1:
                continue
            # prefer EXACT title match, then the most-specific (longest) name, so
            # "Core CPI m/m" takes core's value - not plain "CPI m/m".
            score = (1000 if mn == nt else len(mn)) - dday
            if score > bestscore:
                best = m; bestscore = score
        if best and str(best["raw"].get("actual", "")).strip():
            r = best["raw"]
            row["actual"] = r["actual"]
            if str(r.get("forecast", "")).strip():
                row["forecast"] = r["forecast"]
            if str(r.get("previous", "")).strip():
                row["previous"] = r["previous"]
            n += 1
            if bars:                              # attach the REAL measured gold reaction
                rx = _reaction(bars, r.get("time", ""))
                if rx:
                    row["reaction"] = rx; rx_n += 1
    if n:
        print("[push] enriched %d events with MT5 actuals (%d with gold reaction)" % (n, rx_n))
    return data


def push_calendar():
    """Send the locally-downloaded economic calendar to the cloud (it may be blocked
    on the datacenter IP). Best-effort; the cloud keeps its last good copy otherwise."""
    try:
        with open(CAL_CACHE, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        data = obj.get("data") or []
        if not data:
            return
        data = _enrich_actuals(data)        # add MT5 released figures for the cloud
        body = json.dumps({"data": data, "token": TOKEN}).encode("utf-8")
        req = urllib.request.Request(CLOUD_URL + "/ingest/calendar", data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=12).read()
        print("[push] calendar %d events -> cloud" % len(data))
    except Exception as e:
        print("[push] cal err:", repr(e)[:70])


def push_news():
    """The cloud (datacenter IP) gets thin news from Google; the local PC (residential
    IP) gets the full read. Forward the local server's news + headline archive to the
    cloud so its live feed + 'back room' archive are complete."""
    try:
        base = "http://127.0.0.1:8008"
        news = json.loads(urllib.request.urlopen(base + "/news", timeout=10).read())
        arch = {}
        try:
            arch = json.loads(urllib.request.urlopen(base + "/geo_history?q=", timeout=10).read())
        except Exception:
            arch = {}
        payload = {
            "token": TOKEN,
            "geo_risk": news.get("geo_risk"), "geo_bias": news.get("geo_bias"),
            "geo_categories": news.get("geo_categories"), "geo_top": news.get("geo_top"),
            "headline": news.get("headline"), "geo_headlines": news.get("geo_headlines") or [],
            "fear": news.get("fear"), "gold_direct": news.get("gold_direct"),
            "archive": (arch.get("items") or [])[:80],
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(CLOUD_URL + "/ingest/news", data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=12).read()
        print("[push] news %d live / %d archive -> cloud" %
              (len(payload["geo_headlines"]), len(payload["archive"])))
    except Exception as e:
        print("[push] news err:", repr(e)[:70])


def push_bars():
    """Send the EA's real broker OHLC bars (M15/D1/W1) to the cloud candle chart."""
    try:
        if time.time() - os.path.getmtime(MT5_BARS) > 3600:
            return
        bars = {"M1": [], "M5": [], "M15": [], "M30": [], "H1": [], "H4": [], "D1": [], "W1": []}
        with open(MT5_BARS, "r", encoding="ascii", errors="ignore") as fh:
            for ln in fh:
                p = ln.strip().split(",")
                if len(p) == 6 and p[0] in bars:          # TF,epoch_sec,o,h,l,c (real time)
                    bars[p[0]].append([int(float(p[1])) * 1000, float(p[2]), float(p[3]), float(p[4]), float(p[5])])
                elif len(p) == 5 and p[0] in bars:        # legacy TF,o,h,l,c (no time)
                    bars[p[0]].append([float(p[1]), float(p[2]), float(p[3]), float(p[4])])
        if not any(bars.values()):
            return
        body = json.dumps({"bars": bars, "token": TOKEN}).encode("utf-8")
        req = urllib.request.Request(CLOUD_URL + "/ingest/bars", data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=12).read()
        print("[push] bars -> cloud (" + ",".join("%s/%d" % (k, len(v)) for k, v in bars.items() if v) + ")")
    except Exception as e:
        print("[push] bars err:", repr(e)[:70])


def main():
    if not CLOUD_URL:
        print("Set GBAI_CLOUD_URL (your Render URL) or pass it as arg 1."); return
    print("[push] MT5 %s -> %s  every %ss" % (MT5_QUOTE, CLOUD_URL, INTERVAL))
    last_cal = 0; last_bars = 0; last_news = 0; last_learn = 0
    while True:
        try:
            q = read_quote()               # push every cycle so price AND flow stay fresh
            body = json.dumps(q).encode("utf-8")
            req = urllib.request.Request(CLOUD_URL + "/ingest/quote", data=body,
                                         headers={"Content-Type": "application/json"})
            r = urllib.request.urlopen(req, timeout=8).read()
            print("[push] %.2f%s -> %s" % (q["bid"], (" +flow" if "flow" in q else ""), r.decode()[:30]))
        except Exception as e:
            print("[push] err:", repr(e)[:80])
        # push the calendar right away then every ~5 min (keeps cloud events populated)
        if time.time() - last_cal > 300:
            push_calendar(); last_cal = time.time()
        if time.time() - last_bars > 60:
            push_bars(); last_bars = time.time()
        if time.time() - last_news > 120:
            push_news(); last_news = time.time()
        if time.time() - last_learn > 1800:        # GoldHunter learns from its own trades every 30 min
            try:
                import learn_hunter
                import importlib; importlib.reload(learn_hunter)
                learn_hunter.main()
            except Exception as e:
                print("[learn] err:", repr(e)[:80])
            last_learn = time.time()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
