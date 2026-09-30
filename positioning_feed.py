"""
Gold Brain AI PRO - CFTC Commitments of Traders (COT) positioning feed.

Weekly CFTC positioning for the instruments the dashboard covers - gold, silver
and the major currency futures - so every symbol gets its own "what are the big
players doing" read. For an FX pair the COT is on the non-USD leg and its
direction is mapped onto the pair (e.g. specs net-long JPY = bearish USDJPY).

Honesty: weekly, delayed positioning data - NOT a live trader count. Each field
carries the report date + a confidence from data age; unreachable data degrades
to "unavailable", never fabricated.

Source: CFTC public reporting API (Socrata), no key. Served via GET /positioning
(gold, default) and /positioning?symbol= (per instrument).
"""

from __future__ import annotations
import os
import json
import time
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timezone

_DATASET = "6dca-aqww"     # CFTC legacy futures-only
_BASE = "https://publicreporting.cftc.gov/resource/%s.json" % _DATASET
_UA = {"User-Agent": "Mozilla/5.0 (GoldBrainAI positioning feed)"}
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cot_cache.json")
REFRESH_SEC = 6 * 3600

# CFTC contract-market codes.
CONTRACTS = {
    "XAU": ("088691", "Gold"), "XAG": ("084691", "Silver"),
    "EUR": ("099741", "Euro"), "GBP": ("096742", "Pound"), "JPY": ("097741", "Yen"),
    "AUD": ("232741", "Aussie"), "CAD": ("090741", "Loonie"), "CHF": ("092741", "Franc"),
    "BTC": ("133741", "Bitcoin"), "ETH": ("146021", "Ether"),
}
# symbol -> (contract key, relation to the pair: metal/base/quote)
# 'metal' = direct USD-quoted asset (net long = bullish for the instrument).
SYMBOL_COT = {
    "XAUUSD": ("XAU", "metal"), "XAUEUR": ("XAU", "metal"), "XAGUSD": ("XAG", "metal"),
    "BTCUSD": ("BTC", "metal"), "ETHUSD": ("ETH", "metal"),
    "EURUSD": ("EUR", "base"), "GBPUSD": ("GBP", "base"), "AUDUSD": ("AUD", "base"),
    "USDJPY": ("JPY", "quote"), "USDCAD": ("CAD", "quote"), "USDCHF": ("CHF", "quote"),
}


def _get(url, timeout=15):
    return urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout).read()


def _num(row, *names):
    for n in names:
        if n in row and row[n] not in (None, ""):
            try:
                return float(row[n])
            except (TypeError, ValueError):
                continue
    return 0.0


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


class PositioningFeed:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = {}          # contract key -> parsed record
        self._thread = None
        self._load_cache()

    def _load_cache(self):
        try:
            if os.path.exists(CACHE):
                with open(CACHE, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
                if isinstance(d, dict):
                    self._data = d
        except Exception:
            pass

    def _save_cache(self):
        try:
            with open(CACHE, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh)
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

    def _fetch_contract(self, code):
        where = urllib.parse.quote("cftc_contract_market_code='%s'" % code, safe="='")
        url = "%s?$where=%s&$order=report_date_as_yyyy_mm_dd%%20DESC&$limit=2" % (_BASE, where)
        rows = json.loads(_get(url).decode("utf-8", "ignore"))
        return rows if isinstance(rows, list) else []

    def _parse(self, row):
        ncl = _num(row, "noncomm_positions_long_all")
        ncs = _num(row, "noncomm_positions_short_all")
        cl = _num(row, "comm_positions_long_all")
        cs = _num(row, "comm_positions_short_all")
        oi = _num(row, "open_interest_all")
        traders = _num(row, "traders_tot_all")
        rdate = str(row.get("report_date_as_yyyy_mm_dd", ""))[:10]
        return {"noncomm_long": int(ncl), "noncomm_short": int(ncs), "noncomm_net": int(ncl - ncs),
                "comm_long": int(cl), "comm_short": int(cs), "comm_net": int(cl - cs),
                "open_interest": int(oi), "traders_total": int(traders), "report_date": rdate}

    def refresh(self):
        for key, (code, label) in CONTRACTS.items():
            try:
                rows = self._fetch_contract(code)
                if not rows:
                    continue
                cur = self._parse(rows[0])
                prev_net = self._parse(rows[1])["noncomm_net"] if len(rows) > 1 else cur["noncomm_net"]
                oi = max(1, cur["open_interest"])
                spec_pct = 100.0 * cur["noncomm_net"] / oi
                wk = cur["noncomm_net"] - prev_net
                bias = _clamp(wk / 30000.0, -1.0, 1.0)          # fresh-flow lean of THIS currency
                if spec_pct > 5:
                    lbl = "spec NET LONG"
                elif spec_pct < -5:
                    lbl = "spec NET SHORT"
                else:
                    lbl = "balanced"
                if abs(spec_pct) >= 45:
                    lbl += " (crowded)"
                rec = dict(cur)
                rec.update({"label": lbl, "asset": label, "spec_net_pct_oi": round(spec_pct, 1),
                            "weekly_change_net": int(wk), "bias": round(bias, 3),
                            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")})
                with self._lock:
                    self._data[key] = rec
                self._save_cache()
            except Exception:
                continue
        return self.snapshot()

    def _conf(self, rec):
        try:
            rd = datetime.strptime(rec["report_date"][:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - rd).days
            return (age, 90 if age <= 8 else 70 if age <= 15 else 40)
        except Exception:
            return (-1, 0)

    def _view(self, key, relation=None):
        with self._lock:
            rec = dict(self._data.get(key, {}))
        if not rec:
            return {"ok": False, "availability": "unavailable", "source": "CFTC COT (weekly)",
                    "note": "weekly positioning data - NOT a real-time trader count"}
        age, conf = self._conf(rec)
        rec["ok"] = True
        rec["availability"] = "live"
        rec["source"] = "CFTC COT (weekly)"
        rec["data_age_days"] = age
        rec["confidence"] = conf
        rec["note"] = "weekly positioning data - NOT a real-time trader count"
        # map the currency's bias onto the selected pair
        if relation == "quote":         # non-USD is the QUOTE (e.g. JPY in USDJPY)
            rec["pair_bias"] = round(-rec.get("bias", 0.0), 3)
        else:                            # metal or base -> same direction as the pair
            rec["pair_bias"] = round(rec.get("bias", 0.0), 3)
        rec["relation"] = relation or "metal"
        return rec

    def snapshot(self):
        # default = gold (back-compat with the existing EA /predict blend & panel)
        return self._view("XAU", "metal")

    def snapshot_for(self, symbol):
        symbol = (symbol or "XAUUSD").upper()
        key, rel = SYMBOL_COT.get(symbol, ("XAU", "metal"))
        v = self._view(key, rel)
        v["symbol"] = symbol
        return v


FEED = PositioningFeed()


if __name__ == "__main__":
    FEED.refresh()
    for s in ["XAUUSD", "EURUSD", "USDJPY", "GBPUSD"]:
        v = FEED.snapshot_for(s)
        print("%-7s %-16s net=%s pct=%s bias=%s pair_bias=%s" %
              (s, v.get("label"), v.get("noncomm_net"), v.get("spec_net_pct_oi"),
               v.get("bias"), v.get("pair_bias")))
