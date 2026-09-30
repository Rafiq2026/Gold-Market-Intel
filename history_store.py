"""
Gold Brain AI PRO - composite drivers history (Phase 3c).

Samples the composite "gold drivers" score on a schedule and appends it to a
small on-disk ring buffer, so the dashboard chart survives browser restarts and
shows a real multi-hour/day trend (not just the current session).

Composite = 0.6 * news geo_bias + 0.4 * COT bias   (range -1..+1 -> shown -100..+100)
Served via GET /history.
"""

from __future__ import annotations
import os
import json
import time
import threading
from datetime import datetime, timezone

import news_feed as NF
import positioning_feed as PF

HIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gmi_history.json")
SAMPLE_SEC = 300           # sample every 5 minutes
MAX_POINTS = 2016          # ~7 days at 5-min spacing


def _clamp(v, a, b):
    return a if v < a else b if v > b else v


class HistoryStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._pts = self._load()
        self._thread = None

    def _load(self):
        try:
            if os.path.exists(HIST_FILE):
                with open(HIST_FILE, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
                if isinstance(d, list):
                    return d[-MAX_POINTS:]
        except Exception:
            pass
        return []

    def _save(self):
        try:
            with open(HIST_FILE, "w", encoding="utf-8") as fh:
                json.dump(self._pts, fh)
        except Exception:
            pass

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        time.sleep(20)   # let the feeds populate first
        while True:
            try:
                self.sample()
            except Exception:
                pass
            time.sleep(SAMPLE_SEC)

    def sample(self):
        n = NF.FEED.snapshot()
        p = PF.FEED.snapshot()
        geo = float(n.get("geo_bias", 0.0) or 0.0)
        cot = float(p.get("bias", 0.0) or 0.0) if p.get("report_date") else 0.0
        score = int(round(_clamp(0.6 * geo + 0.4 * cot, -1.0, 1.0) * 100))
        pt = {"t": int(time.time() * 1000), "s": score,
              "geo": round(geo, 3), "cot": round(cot, 3),
              "iso": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        with self._lock:
            self._pts.append(pt)
            if len(self._pts) > MAX_POINTS:
                self._pts = self._pts[-MAX_POINTS:]
            self._save()
        return pt

    def snapshot(self) -> dict:
        with self._lock:
            return {"ok": True, "count": len(self._pts), "points": list(self._pts)}


FEED = HistoryStore()
