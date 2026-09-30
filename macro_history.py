"""
Gold Brain AI PRO - macro history (Phase 3d).

Pulls the REAL historical actuals for the major US releases from FRED
(fredgraph CSV, public, no API key) and uses them to sharpen the forward read
on an upcoming event:

  * recent prints of the indicator (in the calendar's own convention: m/m %,
    y/y %, level, or job change in thousands)
  * the recent average / trend
  * whether the upcoming FORECAST is above / below / in line with that trend
    -> an evidence-based skew (e.g. a forecast well above trend is an aggressive
       call, so a miss toward the mean is the base-rate risk).

Honesty: FRED actuals can differ slightly from the release headline (revisions /
seasonal adjustment), so figures are labelled as FRED-derived, and only a
curated set of cleanly-mappable series is supported. Everything degrades to
"no history" rather than guessing.
"""

from __future__ import annotations
import os
import json
import time
import threading
import urllib.request
from datetime import datetime, timezone

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "macro_cache.json")
TTL = 12 * 3600                      # refetch a series at most twice a day
_UA = {"User-Agent": "Mozilla/5.0 (GoldBrainAI macro history)"}

# Curated map: (keyword) -> (FRED series id, base kind). m/m vs y/y is taken
# from the event title. 'kind': pct=index->%, level=as-is, jobs=diff in thousands.
_MAP = [
    ("core pce",            "PCEPILFE", "pct"),
    ("pce",                 "PCEPI",    "pct"),
    ("core cpi",            "CPILFESL", "pct"),
    ("cpi",                 "CPIAUCSL", "pct"),
    ("core ppi",            "WPSFD49116", "pct"),
    ("ppi",                 "PPIFIS",   "pct"),
    ("unemployment rate",   "UNRATE",   "level"),
    ("non-farm",            "PAYEMS",   "jobs"),
    ("nonfarm",             "PAYEMS",   "jobs"),
    ("nfp",                 "PAYEMS",   "jobs"),
    ("payroll",             "PAYEMS",   "jobs"),
    ("retail sales",        "RSAFS",    "pct"),
    ("unemployment claims", "ICSA",     "level"),
    ("jobless",             "ICSA",     "level"),
]


def _get(url, timeout=20):
    return urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout).read().decode("utf-8", "ignore")


def _match(title):
    t = title.lower()
    for kw, sid, kind in _MAP:
        if kw in t:
            yoy = ("y/y" in t or "yoy" in t or "annual" in t)
            return sid, kind, yoy
    return None


class MacroHistory:
    def __init__(self):
        self._lock = threading.Lock()
        self._series = {}          # sid -> [(date, float), ...]
        self._fetched = {}         # sid -> epoch
        self._load()

    def _load(self):
        try:
            if os.path.exists(CACHE):
                with open(CACHE, "r", encoding="utf-8") as fh:
                    obj = json.load(fh)
                self._series = {k: [tuple(x) for x in v] for k, v in obj.get("series", {}).items()}
                self._fetched = obj.get("fetched", {})
        except Exception:
            pass

    def _save(self):
        try:
            with open(CACHE, "w", encoding="utf-8") as fh:
                json.dump({"series": self._series, "fetched": self._fetched}, fh)
        except Exception:
            pass

    def _ensure(self, sid):
        now = time.time()
        if sid in self._series and (now - self._fetched.get(sid, 0)) < TTL:
            return self._series[sid]
        try:
            csv = _get(FRED_CSV % sid)
            rows = []
            for ln in csv.strip().splitlines()[1:]:
                parts = ln.split(",")
                if len(parts) < 2 or parts[1] in (".", ""):
                    continue
                try:
                    rows.append((parts[0], float(parts[1])))
                except ValueError:
                    continue
            if rows:
                with self._lock:
                    self._series[sid] = rows[-60:]     # keep last 60 obs
                    self._fetched[sid] = now
                    self._save()
        except Exception:
            pass
        return self._series.get(sid, [])

    def history_for(self, title, n=12):
        m = _match(title)
        if not m:
            return None
        sid, kind, yoy = m
        rows = self._ensure(sid)
        if len(rows) < 3:
            return None
        vals = [v for _, v in rows]
        dates = [d for d, _ in rows]
        out = []
        if kind == "level":
            for i in range(len(vals)):
                out.append((dates[i], round(vals[i], 2)))
            unit = "%" if sid == "UNRATE" else "K"
        elif kind == "jobs":
            for i in range(1, len(vals)):
                out.append((dates[i], round(vals[i] - vals[i - 1], 0)))   # change in thousands
            unit = "K"
        else:  # pct (index -> % change)
            lag = 12 if yoy else 1
            for i in range(lag, len(vals)):
                if vals[i - lag]:
                    out.append((dates[i], round((vals[i] / vals[i - lag] - 1.0) * 100.0, 2)))
            unit = "%"
        recent = out[-n:]
        if not recent:
            return None
        series_vals = [v for _, v in recent]
        last6 = series_vals[-6:]
        mean = round(sum(last6) / len(last6), 2)     # 6-month average (skew basis)
        return {"series": sid, "unit": unit, "yoy": yoy,
                "recent": [{"date": d, "val": v} for d, v in recent],
                "mean": mean, "last": series_vals[-1], "n": len(recent)}


FEED = MacroHistory()


if __name__ == "__main__":
    for t in ["CPI m/m", "Core CPI y/y", "Unemployment Rate", "Non-Farm Employment Change", "Retail Sales m/m"]:
        h = FEED.history_for(t)
        if h:
            seq = " ".join("%.2f" % p["val"] for p in h["recent"])
            print("%-30s [%s] mean=%s%s  recent: %s" % (t, h["series"], h["mean"], h["unit"], seq))
        else:
            print("%-30s  (no series)" % t)
