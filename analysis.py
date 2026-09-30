"""
Gold Brain AI PRO - natural-language market analysis.

Composes a short, plain-English paragraph for the selected instrument that
synthesises everything the dashboard knows: the live price, the macro / USD
backdrop, geopolitics, CFTC positioning (gold), and the next high-impact event
with its expectation vs the REAL prior-data trend (FRED) and the likely effect.

Rule-based and data-driven - every clause is built from a real feed value, so
nothing is fabricated. (Later this can be handed to the AI model for phrasing.)

Served via GET /analysis?symbol=
"""

from __future__ import annotations
from datetime import datetime, timezone

import news_feed as NF
import positioning_feed as PF
import events_feed as EF
import quotes_feed as QF
import crypto_flow as CFL


def _fmt_local_in(hours):
    if hours is None:
        return ""
    if hours < 0:
        return "%.1fh ago" % (-hours)
    if hours < 1:
        return "in %dm" % round(hours * 60)
    if hours < 24:
        return "in %.1fh" % hours
    return "in %dd" % round(hours / 24)


CRYPTO = ("BTCUSD", "ETHUSD")


def _pair_effect(symbol, ev):
    """How this event's expected outcome maps to the selected pair."""
    d = ev.get("impact_dir")
    if symbol.startswith("XAU"):
        return d  # gold uses the USD-macro direction directly
    if symbol in CRYPTO:
        # crypto is a USD-quoted risk asset: a dovish/weak-USD print (bullish gold,
        # d="up") is risk-on and supports crypto; hawkish is a headwind.
        return d if ev.get("currency", "") == "USD" else "na"
    base, quote = symbol[:3], symbol[3:6]
    c = ev.get("currency", "")
    cc = "str" if d == "down" else "weak" if d == "up" else None
    if cc is None or (c != base and c != quote):
        return "na"
    strong = (cc == "str")
    up = (strong and c == base) or (not strong and c == quote)
    return "up" if up else "down"


def build(symbol="XAUUSD"):
    symbol = (symbol or "XAUUSD").upper()
    q = QF.FEED.snapshot()
    n = NF.FEED.snapshot()
    p = PF.FEED.snapshot_for(symbol)
    ev = EF.FEED.snapshot()

    sym = (q.get("symbols") or {}).get(symbol) or {}
    price = sym.get("price"); pct = sym.get("pct")
    dec = sym.get("dec", 2)
    dxy = q.get("dxy") or {}

    is_gold = symbol.startswith("XAU")
    is_crypto = symbol in CRYPTO
    geo = float(n.get("geo_risk", 0.0) or 0.0)
    geo_bias = float(n.get("geo_bias", 0.0) or 0.0)
    geo_top = n.get("geo_top", "")
    headline = n.get("headline", "")
    cot_pair = float(p.get("pair_bias", 0.0) or 0.0) if p.get("ok") else 0.0

    usd = max(-1.0, min(1.0, (dxy.get("pct", 0.0) or 0.0) / 0.5))        # USD strength today
    if is_gold:
        comp = round(max(-1.0, min(1.0, 0.6 * geo_bias + 0.4 * cot_pair)) * 100)
    elif is_crypto:
        # risk asset: risk-off (geo_bias>0, which helps gold) is a HEADWIND for crypto;
        # a stronger USD is also a headwind; CFTC + Hyperliquid perp funding confirm.
        cf = CFL.FEED.snapshot_for(symbol)
        perp = float(cf.get("bias", 0.0) or 0.0) if cf.get("ok") else 0.0
        comp = round(max(-1.0, min(1.0, -0.35 * geo_bias - 0.25 * usd
                                    + 0.2 * cot_pair + 0.2 * perp)) * 100)
    else:
        base, quote = symbol[:3], symbol[3:6]
        usd_comp = (-usd if quote == "USD" else usd if base == "USD" else 0.0)
        comp = round(max(-1.0, min(1.0, 0.5 * usd_comp + 0.5 * cot_pair)) * 100)

    lean = "bullish" if comp > 8 else "bearish" if comp < -8 else "neutral"
    parts = []

    # 1) price + backdrop
    if price is not None:
        parts.append("%s is trading around %s (%s%.2f%% today)."
                     % (symbol, ("%.*f" % (dec, price)), "+" if (pct or 0) >= 0 else "", pct or 0.0))
    if is_gold:
        parts.append("The macro backdrop leans %s for gold (composite %+d): it blends the "
                     "geopolitical safe-haven bias with speculative positioning." % (lean, comp))
    elif is_crypto:
        parts.append("The macro backdrop leans %s for %s (composite %+d): as a risk asset it "
                     "blends market risk appetite, the USD's direction, and its own CFTC positioning."
                     % (lean, symbol, comp))
    else:
        parts.append("The macro backdrop leans %s for %s (composite %+d), from the USD's direction "
                     "and CFTC positioning in its currencies." % (lean, symbol, comp))

    # 2) geopolitics
    grisk = "elevated" if geo >= 0.6 else "moderate" if geo >= 0.35 else "low"
    if is_crypto:
        gdir = "risk-off, a headwind for risk assets like crypto" if geo_bias > 0.05 else \
               "risk-on, supportive for crypto" if geo_bias < -0.05 else "broadly balanced"
    else:
        gdir = "risk-off, which supports gold" if geo_bias > 0.05 else \
               "risk-on, a headwind for gold" if geo_bias < -0.05 else "broadly balanced"
    gsent = "Geopolitical risk is %s (%.0f%%)" % (grisk, geo * 100)
    if geo_top:
        gsent += ", driven mainly by %s" % geo_top
    gsent += " — currently %s." % gdir
    parts.append(gsent)
    if headline:
        parts.append("Top headline: \"%s\"." % headline[:140])

    # 3) positioning (per symbol)
    if p.get("ok") and p.get("report_date"):
        if is_gold:
            parts.append("CFTC positioning shows speculators %s (%s%% of open interest, as of %s) — "
                         "a crowded book can amplify sharp reversals."
                         % (p.get("label", "n/a"), p.get("spec_net_pct_oi", 0), p.get("report_date", "")))
        else:
            implic = "bullish" if cot_pair > 0.05 else "bearish" if cot_pair < -0.05 else "neutral"
            parts.append("CFTC positioning in %s futures is %s (%s%% of OI, as of %s) — a %s tilt for %s."
                         % (p.get("asset", ""), p.get("label", "n/a"), p.get("spec_net_pct_oi", 0),
                            p.get("report_date", ""), implic, symbol))

    # 3b) crypto perp flow (Hyperliquid funding) - the crypto positioning gauge
    if is_crypto:
        cf = CFL.FEED.snapshot_for(symbol)
        if cf.get("ok"):
            cav = ("; a crowded book that can snap back" if cf.get("crowded")
                   else "")
            parts.append("Perp funding (Hyperliquid) shows the crowd %s at %+.2f%%/day%s — the crypto positioning gauge."
                         % (cf.get("lean", "neutral"), cf.get("funding_day_pct", 0.0), cav))

    # 4) next high-impact event
    upcoming = [e for e in ev.get("events", []) if e.get("impact") == "High" and e.get("hours_until", 0) >= 0]
    if upcoming:
        e = upcoming[0]
        eff = _pair_effect(symbol, e)
        when = _fmt_local_in(e.get("hours_until"))
        s = "The next high-impact event is %s (%s) %s" % (e.get("title", ""), e.get("currency", ""), when)
        if e.get("forecast") or e.get("previous"):
            s += ", consensus %s vs previous %s" % (e.get("forecast") or "—", e.get("previous") or "—")
        s += "."
        parts.append(s)
        if e.get("note"):
            parts.append(e["note"])
        if eff in ("up", "down"):
            parts.append("On balance the setup points %s for %s if it prints as expected; the sharper move is on a surprise."
                         % ("higher" if eff == "up" else "lower", symbol))
        elif eff == "na":
            parts.append("Its direct effect on %s is limited (different currency); watch it for cross-market spillover." % symbol)
    else:
        parts.append("No high-impact event is scheduled in the near term — price is driven by flow and headlines for now.")

    # 5) bottom line
    if is_gold:
        bl = "Bottom line: the standing lean is %s; the decisive short-term driver will be the next US data / Fed surprise and any geopolitical shift." % lean
    elif is_crypto:
        bl = "Bottom line: %s trades on risk appetite and the USD/rate path — a dovish Fed and risk-on tape lift it, while risk-off and a firm dollar weigh; its own flows can amplify both." % symbol
    else:
        bl = "Bottom line: %s trades the interest-rate and data story of its two currencies — the next release above/below consensus sets the near-term direction." % symbol
    parts.append(bl)

    text = " ".join(parts)
    return {"ok": True, "symbol": symbol, "composite": comp, "lean": lean,
            "text": text, "updated": datetime.now(timezone.utc).strftime("%H:%M:%SZ")}


if __name__ == "__main__":
    import sys
    print(build(sys.argv[1] if len(sys.argv) > 1 else "XAUUSD")["text"])
