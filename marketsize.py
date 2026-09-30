"""
Gold Brain AI PRO - "money in the market" (market activity).

The honest answer to "how much money / how many trades are in the market":
  * Open Interest  = money committed to open positions right now
  * Volume ($)     = money actually traded (today / 24h)
  * Reportable traders = the real count of large traders (CFTC COT)
  * Buy/sell lean  = net positioning (COT) or perp funding (crypto)

Real proxies only - the EXACT number of retail traders is unknowable, and we say
so. Sources: Hyperliquid (crypto, live), CFTC COT (gold/FX, weekly, real trader
count + OI), Yahoo (futures daily volume). Served via GET /marketsize?symbol=.
"""

from __future__ import annotations
import json
import os
import time
import urllib.request

import quotes_feed as QF
import positioning_feed as PF
import crypto_flow as CFL

_FLOW_FILE = os.path.join(os.path.dirname(QF.MT5_QUOTE), "GoldBrainAI_Flow.txt")


def _broker_flow():
    """Live broker buy/sell aggression (XAUUSD). LOCAL: the EA's flow file.
    CLOUD: whatever the local PC pushed via push_quote.py (quotes_feed.pushed_flow)."""
    # 1) local MT5 flow file (when running on the trading PC)
    try:
        if os.path.exists(_FLOW_FILE) and time.time() - os.path.getmtime(_FLOW_FILE) <= 120:
            with open(_FLOW_FILE, "r", encoding="ascii", errors="ignore") as fh:
                p = fh.read().strip().split(",")
            if len(p) >= 7:
                buy, sell = float(p[0]), float(p[1])
                return {"buy": buy, "sell": sell, "net": round(buy - sell, 1),
                        "dom_active": p[2] == "1", "dom_imb": float(p[3]),
                        "spread_pts": float(p[4]), "thin": p[5] == "1", "vacuum": p[6] == "1",
                        "near_zone": p[7] if len(p) > 7 else ""}
    except Exception:
        pass
    # 2) cloud: pushed from the local PC
    try:
        import quotes_feed as _QF
        fl = _QF.FEED.pushed_flow()
        if fl and "buy" in fl:
            buy, sell = float(fl["buy"]), float(fl["sell"])
            return {"buy": buy, "sell": sell, "net": round(buy - sell, 1),
                    "dom_active": bool(fl.get("dom_active")), "dom_imb": float(fl.get("dom_imb", 0)),
                    "spread_pts": float(fl.get("spread_pts", 0)), "thin": bool(fl.get("thin")),
                    "vacuum": bool(fl.get("vacuum")), "near_zone": fl.get("near_zone", "")}
    except Exception:
        pass
    return None

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=5d&interval=1d"

# symbol -> (yahoo futures ticker, contract unit size) for a clean $ notional
_FUT = {"XAUUSD": ("GC=F", 100.0), "XAGUSD": ("SI=F", 5000.0)}
_CRYPTO = ("BTCUSD", "ETHUSD")

_vol_cache = {}   # ticker -> (ts, volume_contracts)


def _human_usd(n):
    a = abs(n)
    if a >= 1e12: return "$%.2fT" % (n / 1e12)
    if a >= 1e9:  return "$%.2fB" % (n / 1e9)
    if a >= 1e6:  return "$%.0fM" % (n / 1e6)
    if a >= 1e3:  return "$%.0fK" % (n / 1e3)
    return "$%.0f" % n


def _human(n):
    a = abs(n)
    if a >= 1e9: return "%.2fB" % (n / 1e9)
    if a >= 1e6: return "%.2fM" % (n / 1e6)
    if a >= 1e3: return "%.1fK" % (n / 1e3)
    return "%.0f" % n


def _live_price(symbol):
    return ((QF.FEED.snapshot().get("symbols", {}) or {}).get(symbol, {}) or {}).get("price")


def _daily_volume(ticker):
    now = time.time()
    c = _vol_cache.get(ticker)
    if c and now - c[0] < 300:
        return c[1]
    try:
        d = json.loads(urllib.request.urlopen(
            urllib.request.Request(_CHART % urllib.request.quote(ticker), headers=_UA), timeout=12).read())
        vol = (d["chart"]["result"][0].get("indicators", {}).get("quote") or [{}])[0].get("volume") or []
        vol = [v for v in vol if v]
        v = float(vol[-1]) if vol else 0.0
    except Exception:
        v = 0.0
    _vol_cache[ticker] = (now, v)
    return v


def snapshot_for(symbol):
    symbol = (symbol or "XAUUSD").upper()
    price = _live_price(symbol) or 0.0

    # ---- Crypto: live dollar OI + 24h dollar volume from Hyperliquid ----
    if symbol in _CRYPTO:
        cf = CFL.FEED.snapshot_for(symbol)
        if not cf.get("ok"):
            return {"ok": False, "symbol": symbol, "note": "crypto flow unavailable"}
        oi_usd = cf.get("open_interest", 0.0) * (cf.get("mark") or price or 0.0)
        return {"ok": True, "symbol": symbol, "asset": cf.get("asset"),
                "source": "Hyperliquid perp (live)",
                "oi_usd": oi_usd, "oi_display": _human_usd(oi_usd),
                "vol_usd": cf.get("day_volume_usd", 0.0),
                "vol_display": _human_usd(cf.get("day_volume_usd", 0.0)), "vol_label": "24h volume",
                "traders": None,
                "lean": cf.get("lean", "neutral"),
                "lean_display": cf.get("label", "—") + " (funding %+.2f%%/day)" % cf.get("funding_day_pct", 0.0),
                "note": "Live perp market. Exact retail trader count is not published anywhere."}

    # ---- Gold/FX: CFTC OI + real reportable-trader count; futures $ volume ----
    pos = PF.FEED.snapshot_for(symbol)
    contracts = pos.get("open_interest", 0) if pos.get("ok") else 0
    traders = pos.get("traders_total", 0) if pos.get("ok") else 0
    lean_txt = (pos.get("label", "—") + " (%s%% of OI)" % pos.get("spec_net_pct_oi", 0)) if pos.get("ok") else "—"

    oi_display, vol_display, vol_label = "—", "—", "daily volume"
    if symbol in _FUT and price:
        tk, size = _FUT[symbol]
        oi_usd = contracts * size * price
        oi_display = "%s contracts ≈ %s" % (_human(contracts), _human_usd(oi_usd))
        vc = _daily_volume(tk)
        if vc > 0:
            vol_display = "%s contracts ≈ %s" % (_human(vc), _human_usd(vc * size * price))
    elif contracts:
        # FX: futures OI in contracts + real trader count (spot FX has no central volume)
        oi_display = "%s contracts" % _human(contracts)
        vol_label = "spot volume"
        vol_display = "n/a (FX is decentralized — use futures OI)"

    out = {"ok": True, "symbol": symbol, "asset": pos.get("asset", symbol),
           "source": "CFTC COT (weekly) + futures", "report_date": pos.get("report_date", ""),
           "oi_display": oi_display, "vol_display": vol_display, "vol_label": vol_label,
           "traders": int(traders) if traders else None,
           "lean": "long" if pos.get("bias", 0) > 0 else "short" if pos.get("bias", 0) < 0 else "neutral",
           "lean_display": lean_txt,
           "note": "Open interest + reportable large-trader count are real (CFTC). "
                   "The exact number of ALL traders is not published — nobody can know it."}
    # Live broker buy/sell aggression (gold only, from the EA's order-flow engine).
    fl = _broker_flow() if symbol == "XAUUSD" else None
    if fl:
        out["flow"] = fl
        out["source"] = "CFTC COT + futures + live broker order-flow"
        dom = (" · DOM %s" % ("bid-heavy" if fl["dom_imb"] < 0 else "ask-heavy")) if fl["dom_active"] else ""
        out["flow_display"] = "%.0f%% BUY / %.0f%% SELL%s%s" % (
            fl["buy"], fl["sell"], (" · THIN" if fl["thin"] else ""), dom)
    return out


if __name__ == "__main__":
    for s in ("XAUUSD", "BTCUSD", "EURUSD"):
        print(s, snapshot_for(s))
