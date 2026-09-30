"""
Gold Brain AI PRO - crypto perp flow (Hyperliquid public API).

Free, no-key, on-chain-derived positioning for BTC/ETH: funding rate, open
interest and mark-vs-oracle premium from Hyperliquid's public /info endpoint.
Funding is the crypto analogue of CFTC positioning - persistently positive
funding = the crowd is paying to be LONG (a bullish lean, but a crowded book
that can snap back). We surface it as a labelled bias, never a signal.

Endpoint (POST): https://api.hyperliquid.xyz/info  {"type":"metaAndAssetCtxs"}
"""

from __future__ import annotations
import json
import time
import threading
import urllib.request

_URL = "https://api.hyperliquid.xyz/info"
# our symbol -> Hyperliquid perp name
_MAP = {"BTCUSD": "BTC", "ETHUSD": "ETH"}
_REFRESH = 120  # seconds


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


class CryptoFlowFeed:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = {}     # HL name -> parsed record
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
        req = urllib.request.Request(_URL, data=json.dumps({"type": "metaAndAssetCtxs"}).encode(),
                                     headers={"Content-Type": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore"))
        universe = d[0]["universe"]
        ctxs = d[1]
        idx = {u["name"]: i for i, u in enumerate(universe)}
        out = {}
        for name in set(_MAP.values()):
            i = idx.get(name)
            if i is None:
                continue
            c = ctxs[i]
            fh = float(c.get("funding") or 0.0)          # hourly funding rate
            oi = float(c.get("openInterest") or 0.0)
            prem = float(c.get("premium") or 0.0)
            mark = float(c.get("markPx") or 0.0)
            vol = float(c.get("dayNtlVlm") or 0.0)
            day_funding = fh * 24.0
            # bias: hourly funding scaled so ~0.01%/hr is a full-crowd reading
            bias = _clamp(fh / 0.0001, -1.0, 1.0)
            out[name] = {"funding_hr": fh, "funding_day_pct": round(day_funding * 100, 4),
                         "open_interest": oi, "premium": prem, "mark": mark,
                         "day_volume_usd": vol, "bias": round(bias, 3)}
        with self._lock:
            self._data = out
            self._ts = time.time()

    def snapshot_for(self, symbol):
        name = _MAP.get((symbol or "").upper())
        if not name:
            return {"ok": False, "available": False, "reason": "not a supported crypto symbol"}
        with self._lock:
            rec = dict(self._data.get(name, {}))
            fresh = (time.time() - self._ts) < (_REFRESH * 3)
        if not rec or not fresh:
            return {"ok": False, "available": False, "asset": name}
        b = rec["bias"]
        lean = "long" if b > 0.05 else "short" if b < -0.05 else "neutral"
        crowded = abs(b) >= 0.7
        label = ("crowded %s" % lean) if crowded else ("%s lean" % lean if lean != "neutral" else "balanced")
        return {"ok": True, "available": True, "asset": name, "source": "Hyperliquid perp",
                "bias": b, "lean": lean, "crowded": crowded, "label": label,
                "funding_day_pct": rec["funding_day_pct"], "open_interest": rec["open_interest"],
                "premium": rec["premium"], "day_volume_usd": rec["day_volume_usd"]}


FEED = CryptoFlowFeed()
