"""
Gold Brain AI PRO - PER-SYMBOL prediction scorecard (track record).

Grades every PAST high-impact event that carried a directional call against the
REAL isolated reaction of each tracked instrument, and keeps a permanent record
+ running hit-rate PER SYMBOL. This is the credibility log for the product.

For each passed event, for each symbol where our per-pair read is directional
(bullish/bearish, not "indirect"), we measure that symbol's net move in a 2h
window after the release and score hit / miss / flat. Metals are scored only on
USD events (their dominant driver). Graded once, then frozen.

Nothing is fabricated: an event we could not measure (server down over its
window, or no price data) simply stays ungraded. Served via GET /scorecard and
/scorecard?symbol=.
"""

from __future__ import annotations
import os
import json
import time
import threading
from datetime import datetime, timezone

import events_feed as EF
import quotes_feed as QF

SCORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scorecard.json")
GRADE_AFTER_H = 2.0
GRADE_BEFORE_H = 20.0
WINDOW_MIN = 120
FLAT_PCT = 0.0005          # |net|/pre below this = flat / noise (works across instruments)
LOOP_SEC = 600
TRACK = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "XAGUSD"]


def _ms(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _pair_effect(symbol, e):
    """Directional call of this event FOR the given symbol ('up'/'down'/'na')."""
    d = e.get("impact_dir")
    if symbol[:3] in ("XAU", "XAG"):
        # metals are USD-driven; only score them on USD releases
        return d if (e.get("currency") == "USD" and d in ("up", "down")) else "na"
    base, quote = symbol[:3], symbol[3:6]
    c = e.get("currency", "")
    cc = "str" if d == "down" else "weak" if d == "up" else None
    if cc is None or (c != base and c != quote):
        return "na"
    strong = (cc == "str")
    up = (strong and c == base) or (not strong and c == quote)
    return "up" if up else "down"


def _reaction(series, when_iso):
    try:
        et = _ms(when_iso)
    except Exception:
        return None
    base = None
    for tm, pr in series:
        if tm <= et - 5 * 60000:
            base = pr
    aft = [(tm, pr) for tm, pr in series if et <= tm <= et + WINDOW_MIN * 60000]
    if base is None or not aft:
        return None
    last = aft[-1][1]
    return {"pre": base, "net": last - base}


class Scorecard:
    def __init__(self):
        self._lock = threading.Lock()
        self._rec = {}          # id -> graded record (id = symbol|title|when)
        self._load()
        self._thread = None

    def _load(self):
        try:
            if os.path.exists(SCORE_FILE):
                with open(SCORE_FILE, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
                if isinstance(d, dict):
                    # migrate legacy records (no 'symbol' field / 2-part key) to
                    # the new per-symbol key so the existing gold track record survives.
                    mig = {}
                    for r in d.values():
                        if "symbol" not in r:
                            r["symbol"] = "XAUUSD"
                        mig["%s|%s|%s" % (r.get("symbol", "XAUUSD"), r.get("title", ""), r.get("when", ""))] = r
                    self._rec = mig
        except Exception:
            pass

    def _save(self):
        try:
            with open(SCORE_FILE, "w", encoding="utf-8") as fh:
                json.dump(self._rec, fh)
        except Exception:
            pass

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        time.sleep(25)
        while True:
            try:
                self.grade()
            except Exception:
                pass
            time.sleep(LOOP_SEC)

    def grade(self):
        ev = EF.FEED.snapshot()
        gradeable = [e for e in ev.get("events", [])
                     if e.get("impact") == "High"
                     and -GRADE_BEFORE_H <= e.get("hours_until", 0) <= -GRADE_AFTER_H]
        if not gradeable:
            return
        changed = False
        for symbol in TRACK:
            calls = [(e, _pair_effect(symbol, e)) for e in gradeable]
            calls = [(e, d) for e, d in calls if d in ("up", "down")
                     and ("%s|%s|%s" % (symbol, e.get("title", ""), e.get("when", ""))) not in self._rec]
            if not calls:
                continue
            series = [(p[0], p[1]) for p in QF.FEED.series(symbol)]
            if not series:
                continue
            for e, d in calls:
                r = _reaction(series, e.get("when", ""))
                if not r:
                    continue
                net = r["net"]
                pct = abs(net) / r["pre"] if r["pre"] else 0
                if pct < FLAT_PCT:
                    result = "flat"
                elif (net < 0 and d == "down") or (net > 0 and d == "up"):
                    result = "hit"
                else:
                    result = "miss"
                eid = "%s|%s|%s" % (symbol, e.get("title", ""), e.get("when", ""))
                with self._lock:
                    self._rec[eid] = {
                        "symbol": symbol, "title": e.get("title", ""), "currency": e.get("currency", ""),
                        "when": e.get("when", ""), "predicted": d, "net": round(net, 5),
                        "forecast": e.get("forecast", ""), "previous": e.get("previous", ""),
                        "net_pct": round(pct * 100, 2), "result": result,
                        "graded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                    changed = True
        if changed:
            self._save()

    def snapshot_for(self, symbol="XAUUSD"):
        symbol = (symbol or "XAUUSD").upper()
        with self._lock:
            recs = sorted([r for r in self._rec.values() if r.get("symbol") == symbol],
                          key=lambda r: r.get("when", ""), reverse=True)
        graded = [r for r in recs if r["result"] in ("hit", "miss")]
        hits = sum(1 for r in graded if r["result"] == "hit")
        misses = sum(1 for r in graded if r["result"] == "miss")
        flat = sum(1 for r in recs if r["result"] == "flat")
        rate = round(100.0 * hits / len(graded), 0) if graded else None
        return {"ok": True, "symbol": symbol, "count": len(recs),
                "directional": len(graded), "hits": hits, "misses": misses,
                "flat": flat, "hit_rate": rate, "records": recs[:40]}

    def snapshot(self):
        return self.snapshot_for("XAUUSD")


FEED = Scorecard()


if __name__ == "__main__":
    FEED.grade()
    import pprint
    pprint.pprint(FEED.snapshot_for("XAUUSD"))
