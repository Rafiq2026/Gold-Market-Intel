"""
Gold Brain AI PRO - economic-event feed (Phase 3a).

Runs in a BACKGROUND thread inside ai_server.py and keeps a cached list of
UPCOMING high-impact economic events (dates + times) that move gold/USD, each
tagged with a TRANSPARENT, RULE-BASED expectation of how gold typically reacts
to a hot (above-forecast) print.

Honesty rules (per design):
  * The reaction rule is market CONVENTION (e.g. hot inflation -> stronger USD
    -> gold down), clearly labelled as a rule - NOT a fabricated price forecast
    and NOT a claim about a specific analyst's view.
  * Forecast / previous come straight from the calendar; if a field is missing
    it is shown empty, never invented.

Source: Forex Factory weekly calendar JSON (no API key), same feed the news
lockout already uses. Served to the app/EA via GET /events.
"""

from __future__ import annotations
import os
import threading
import time
import json
import urllib.request
from datetime import datetime, timezone

import macro_history as MH

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
# Shared calendar cache written by news_feed (one download for both modules).
CAL_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calendar_cache.json")
REFRESH_SEC = 1800                      # calendar changes slowly; every 30 min
RELEVANT = {"USD", "EUR", "GBP", "ALL", "CNY"}   # gold-relevant currencies
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}

# Tier-1 US market movers (widen attention).
_TIER1 = ("fomc", "federal funds", "rate decision", "interest rate", "non-farm",
          "nonfarm", "nfp", "cpi", "consumer price", "pce", "core pce", "powell",
          "fed chair")


def _get(url: str, timeout: int = 15) -> bytes:
    return urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout).read()


def _num_of(s):
    """Parse a calendar figure like '0.4%', '227K', '-1.2', '4.3%' -> float."""
    if not s:
        return None
    t = str(s).strip().replace("%", "").replace(",", "").replace("<", "").replace(">", "")
    mult = 1.0
    if t and t[-1] in "KkMmBbTt":
        mult = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}[t[-1].lower()]
        t = t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return None


def _family(title: str):
    """Classify the event into a reaction family (transparent market convention).

    families:
      'usd_hot'   - hot/above-forecast supports USD -> pressures gold
      'weak_up'   - a WEAKER economy reading (higher) supports gold
      'centralbank'/'fedspeak' - outcome/tone driven (hawkish vs dovish)
      'other'
    """
    t = title.lower()
    def has(*ks): return any(k in t for k in ks)
    if has("rate decision", "federal funds", "interest rate", "rate statement",
           "monetary policy", "official bank rate", "bank rate", "mpc", "cash rate",
           "deposit facility", "main refinancing", "official cash rate"):
        return "centralbank"
    if has("fomc", "powell", "fed chair", "member", "speaks", "testimony", "lagarde", "governor"):
        return "fedspeak"
    if has("unemployment rate", "jobless", "claims"):
        return "weak_up"
    if has("cpi", "consumer price", "ppi", "producer price", "pce", "inflation",
           "price index", "non-farm", "nonfarm", "nfp", "payroll", "employment change",
           "adp", "gdp", "retail sales", "ism", "pmi", "durable", "manufacturing",
           "services", "confidence", "sentiment", "spending"):
        return "usd_hot"
    return "other"


def _analyze(title: str, forecast: str, previous: str):
    """Expectation-aware read: the market prices in the FORECAST, so gold trades
    the SURPRISE (actual vs consensus). Returns a dict describing what is priced
    in and the gold reaction under beat / in-line / miss - not a single guess."""
    fam = _family(title)
    fc = _num_of(forecast)
    pv = _num_of(previous)

    # Consensus setup: what does the forecast imply vs the last print?
    expects = "na"
    if fc is not None and pv is not None:
        diff = fc - pv
        eps = (abs(pv) * 0.03) if pv else 0.0
        if diff > eps:
            expects = "higher"
        elif diff < -eps:
            expects = "lower"
        else:
            expects = "steady"

    if fam == "centralbank":
        # A rate decision with numbers IS analysable: forecast vs previous = the
        # expected policy move. Only fall back to "tone-driven" when there is no rate.
        if fc is not None and pv is not None and abs(fc - pv) > 1e-9:
            if fc > pv:
                return {"family": fam, "priced_in": "hike expected",
                        "impact_label": "bearish bias — rate HIKE expected", "impact_dir": "down",
                        "scen": {"beat": "down", "inline": "muted", "miss": "up"},
                        "scen_kind": "hawkish/dovish",
                        "note": ("Market expects a HIKE to %g%% (from %g%%) - hawkish / USD-supportive -> gold DOWN. "
                                 "Largely priced in, so the surprise risk is a HOLD or a dovish dot-plot / "
                                 "press-conference tone -> gold UP." % (fc, pv))}
            return {"family": fam, "priced_in": "cut expected",
                    "impact_label": "bullish bias — rate CUT expected", "impact_dir": "up",
                    "scen": {"beat": "up", "inline": "muted", "miss": "down"},
                    "scen_kind": "hawkish/dovish",
                    "note": ("Market expects a CUT to %g%% (from %g%%) - dovish -> gold UP. Largely priced in, "
                             "so the surprise risk is a HOLD or a hawkish tone -> gold DOWN." % (fc, pv))}
        # forecast == previous (hold expected) or no rate given -> tone decides
        return {"family": fam, "priced_in": "hold expected" if fc is not None else "policy tone",
                "impact_label": "tone-driven: hawkish → ↓ / dovish → ↑", "impact_dir": "na",
                "scen": {"beat": "down", "inline": "muted", "miss": "up"},
                "scen_kind": "hawkish/dovish",
                "note": "A HOLD is expected - the STATEMENT, dot-plot and press-conference TONE decide: "
                        "more hawkish than priced -> gold DOWN, more dovish -> gold UP."}
    if fam == "fedspeak":
        return {"family": fam, "priced_in": "policy tone",
                "impact_label": "tone-driven: hawkish → ↓ / dovish → ↑", "impact_dir": "na",
                "scen": {"beat": "down", "inline": "muted", "miss": "up"},
                "scen_kind": "hawkish/dovish",
                "note": "A speech has no number - gold trades the TONE vs current market pricing: "
                        "hawkish surprise -> gold DOWN, dovish surprise -> gold UP."}
    if fam == "weak_up":
        lbl = {"higher": ("bullish bias — weaker data expected", "up"),
               "lower": ("bearish bias — stronger data expected", "down"),
               "steady": ("muted — in line, priced in", "muted"),
               "na": ("data-dependent", "na")}[expects]
        return {"family": fam, "priced_in": expects,
                "impact_label": lbl[0], "impact_dir": lbl[1],
                "scen": {"beat": "up", "inline": "muted", "miss": "down"},
                "scen_kind": "actual vs forecast",
                "note": "A higher-than-forecast (weaker economy) print supports gold; a lower print "
                        "weighs on it. In-line is largely priced in — the move comes from the surprise."}
    if fam == "usd_hot":
        lbl = {"higher": ("bearish bias — hot data expected", "down"),
               "lower": ("bullish bias — cooling expected", "up"),
               "steady": ("muted — in line, priced in", "muted"),
               "na": ("data-dependent", "na")}[expects]
        return {"family": fam, "priced_in": expects,
                "impact_label": lbl[0], "impact_dir": lbl[1],
                "scen": {"beat": "down", "inline": "muted", "miss": "up"},
                "scen_kind": "actual vs forecast",
                "note": "Consensus is priced in; the sharper move is the surprise — an above-forecast "
                        "print supports the USD and pressures gold, a miss can lift gold."}
    return {"family": "other", "priced_in": "na",
            "impact_label": "low directional impact", "impact_dir": "na",
            "scen": {"beat": "na", "inline": "na", "miss": "na"},
            "scen_kind": "", "note": "No strong directional convention for this release."}


def _hist_read(family, fc, h):
    """Turn real FRED history into a forward-looking, evidence-based read:
    how the upcoming forecast sits vs the indicator's recent trend."""
    if not h:
        return "", ""
    unit = h["unit"]
    seq = " ".join(("%g" % p["val"]) for p in h["recent"])
    base = "FRED actuals (last %d): %s%s · avg %g%s" % (h["n"], seq, unit, h["mean"], unit)
    if fc is None:
        return base + ".", ""
    dev = fc - h["mean"]
    tol = max(abs(h["mean"]) * 0.15, 0.05)
    pos = "above trend" if dev > tol else "below trend" if dev < -tol else "in line with trend"
    imp = ""
    if family == "usd_hot":
        imp = {"above trend": "aggressive call — a miss back toward trend would support gold",
               "below trend": "soft call — gold-supportive if it holds",
               "in line with trend": "matches trend — largely priced in"}[pos]
    elif family == "weak_up":
        imp = {"above trend": "weaker than trend — gold-supportive",
               "below trend": "stronger than trend — gold-negative",
               "in line with trend": "matches trend"}[pos]
    return "%s. Forecast %g%s is %s — %s." % (base, fc, unit, pos, imp), pos


def _trend_score(fc, hist):
    """+ = data heating (recent rising and/or forecast above trend),
       - = data cooling, 0 = flat. Basis for a mild lean when consensus is flat."""
    vals = [p["val"] for p in hist["recent"]]
    mean = hist["mean"]
    tol = max(abs(mean) * 0.10, 0.03)
    s = 0
    if len(vals) >= 6:
        t = sum(vals[-3:]) / 3.0 - sum(vals[-6:-3]) / 3.0
        s += 1 if t > tol else -1 if t < -tol else 0
    if fc is not None:
        d = fc - mean
        s += 1 if d > tol else -1 if d < -tol else 0
    return s


def _lean_from_history(family, fc, hist):
    """When the forecast is in line (would be 'muted'), derive a MILD directional
    lean from the indicator's real recent trend instead of just 'muted'."""
    if not hist or family not in ("usd_hot", "weak_up"):
        return None
    s = _trend_score(fc, hist)
    if family == "usd_hot":
        if s > 0:  return ("lean bearish — data trending hot", "down")
        if s < 0:  return ("lean bullish — data cooling", "up")
        return ("neutral — flat & priced in", "muted")
    # weak_up: a higher (weaker-economy) reading supports gold
    if s > 0:  return ("lean bullish — data softening", "up")
    if s < 0:  return ("lean bearish — data firming", "down")
    return ("neutral — flat & priced in", "muted")


class EventsFeed:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = {"ok": False, "count": 0, "next_event": "", "next_when": "",
                       "next_hours": -1, "events": [], "updated": ""}
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        time.sleep(8)   # let news_feed download & write the shared calendar first
        while True:
            try:
                self.refresh()
            except Exception:
                pass
            time.sleep(REFRESH_SEC)

    def snapshot(self) -> dict:
        with self._lock:
            s = dict(self._state)
        # recompute "hours until" live so the list stays fresh between refreshes
        now = datetime.now(timezone.utc)
        for e in s["events"]:
            try:
                et = datetime.fromisoformat(e["when"])
                e["hours_until"] = round((et - now).total_seconds() / 3600.0, 1)
            except Exception:
                pass
        return s

    def _load_shared_calendar(self):
        """Reuse the calendar news_feed already downloaded (avoids a 2nd fetch)."""
        try:
            if os.path.exists(CAL_CACHE):
                with open(CAL_CACHE, "r", encoding="utf-8") as fh:
                    obj = json.load(fh)
                ts = datetime.fromisoformat(obj["ts"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - ts).total_seconds() < 7200:
                    return obj["data"]
        except Exception:
            pass
        return None

    def ingest_calendar(self, data):
        """Accept a calendar pushed from the local PC (reliable source) — used on the
        cloud where the datacenter IP may be blocked by the calendar CDN."""
        try:
            if isinstance(data, list) and data:
                self._pushed_cal = data
                self._pushed_cal_ts = time.time()
                return True
        except Exception:
            pass
        return False

    def refresh(self):
        # 1) calendar pushed from the local PC (most reliable on the cloud).
        data = None
        try:
            if time.time() - getattr(self, "_pushed_cal_ts", 0) < 7200:
                data = getattr(self, "_pushed_cal", None)
        except Exception:
            pass
        # 2) shared cache (local news_feed); 3) direct fetch.
        if data is None:
            data = self._load_shared_calendar()
        if data is None:
            try:
                data = json.loads(_get(CALENDAR_URL).decode("utf-8", "ignore"))
                try:
                    with open(CAL_CACHE, "w", encoding="utf-8") as fh:
                        json.dump({"ts": datetime.now(timezone.utc).isoformat(), "data": data}, fh)
                except Exception:
                    pass
            except Exception:
                return self.snapshot()   # keep last good; never crash to empty
        now = datetime.now(timezone.utc)
        events = []
        for ev in data:
            impact = str(ev.get("impact", ""))
            cur = str(ev.get("country", ""))
            if impact not in ("High", "Medium"):
                continue
            if cur not in RELEVANT:
                continue
            raw = ev.get("date", "")
            try:
                et = datetime.fromisoformat(raw)
                if et.tzinfo is None:
                    et = et.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            hrs = (et - now).total_seconds() / 3600.0
            if hrs < -26:                     # keep ~1 day of past events (for scoring;
                continue                       # the dashboard table still shows only recent)
            title = str(ev.get("title", ""))
            fc = str(ev.get("forecast", "")); pv = str(ev.get("previous", ""))
            a = _analyze(title, fc, pv)
            # Real historical actuals (FRED) -> forward, evidence-based read.
            hist = MH.FEED.history_for(title) if cur == "USD" else None
            fc_num = _num_of(fc)
            hist_note, hist_pos = _hist_read(a["family"], fc_num, hist)
            note = a["note"] + ((" | " + hist_note) if hist_note else "")
            avg_txt = ("%g%s" % (hist["mean"], hist["unit"])) if hist else ""
            # When the consensus read would be 'muted', replace it with a mild
            # lean derived from the REAL recent trend (only if history exists).
            impact_label, impact_dir = a["impact_label"], a["impact_dir"]
            if impact_dir == "muted":
                lean = _lean_from_history(a["family"], fc_num, hist)
                if lean:
                    impact_label, impact_dir = lean
            tier1 = (cur == "USD") and any(k in title.lower() for k in _TIER1)
            events.append({
                "title": title,
                "currency": cur,
                "impact": impact,
                "tier1": tier1,
                "when": et.astimezone(timezone.utc).isoformat(),
                "hours_until": round(hrs, 1),
                "forecast": fc,
                "previous": pv,
                "family": a["family"],
                "expects": a["priced_in"],          # higher/lower/steady/policy tone
                "impact_label": impact_label,       # single analytical read (no "if")
                "impact_dir": impact_dir,           # up/down/muted/na (for colouring)
                "scen": a["scen"],                  # {beat,inline,miss} (kept for hover detail)
                "scen_kind": a["scen_kind"],
                "hist": hist,                       # FRED recent actuals (or None)
                "hist_pos": hist_pos,               # forecast vs trend: above/below/in line
                "avg": avg_txt,                     # e.g. "0.32%" recent average
                "note": note,                       # expectation + historical evidence
            })
        events.sort(key=lambda e: e["when"])
        upcoming = [e for e in events if e["hours_until"] >= 0]
        nxt = upcoming[0] if upcoming else None
        state = {
            "ok": True,
            "count": len(events),
            "next_event": nxt["title"] if nxt else "",
            "next_when": nxt["when"] if nxt else "",
            "next_hours": nxt["hours_until"] if nxt else -1,
            "events": events,
            "updated": now.strftime("%Y-%m-%d %H:%M:%SZ"),
        }
        with self._lock:
            self._state = state
        return state


# Module-level singleton used by the server.
FEED = EventsFeed()


if __name__ == "__main__":
    FEED.refresh()
    s = FEED.snapshot()
    print("ok=%s count=%s next='%s' in %.1fh" % (s["ok"], s["count"], s["next_event"], s["next_hours"]))
    for e in s["events"][:12]:
        print("  %-4s %-5s %+6.1fh %-30s f=%-6s p=%-6s expects=%-7s beat->%s miss->%s"
              % (e["currency"], e["impact"], e["hours_until"], e["title"][:30],
                 e["forecast"], e["previous"], e["expects"],
                 e["scen"]["beat"], e["scen"]["miss"]))
        print("       -> " + e["note"])
