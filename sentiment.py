"""
Gold Brain AI PRO - news sentiment model.

Replaces the raw keyword count with a real sentiment model and maps it to a
GOLD-directional signal, because market sentiment != gold direction:

  * "fear"        - world risk-off tone (war/crisis/crash). Bullish gold.
  * "gold_direct" - sentiment of headlines that talk about gold/bullion itself
                    (e.g. "gold slumps" is negative -> bearish gold).

Backends (auto-selected):
  1. VADER (vaderSentiment) - rule-based, tuned for headlines, no API key.
  2. Built-in finance lexicon fallback (with simple negation) if VADER is
     not installed, so the module always works.
"""

from __future__ import annotations
import re

# ---- optional VADER backend -------------------------------------------------
_vader = None
try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _vader = SentimentIntensityAnalyzer()
except Exception:
    _vader = None

GOLD_TERMS = ("gold", "bullion", "xau", "precious metal", "spot gold")

# Fallback finance lexicon (word -> polarity). Only used when VADER is absent.
_LEX_POS = {
    "rise": 1, "rises": 1, "rally": 1, "surge": 1, "jump": 1, "gain": 1,
    "gains": 1, "climb": 1, "record": 1, "soar": 1, "boost": 1, "up": 0.5,
    "recover": 1, "optimism": 1, "ease": 1, "eases": 1, "cools": 1,
    "ceasefire": 1, "truce": 1, "deal": 1, "agreement": 1,
}
_LEX_NEG = {
    "fall": 1, "falls": 1, "drop": 1, "plunge": 1, "slump": 1, "crash": 1,
    "tumble": 1, "loss": 1, "losses": 1, "down": 0.5, "sink": 1, "fear": 1,
    "fears": 1, "war": 1, "strike": 1, "attack": 1, "sanction": 1,
    "conflict": 1, "crisis": 1, "recession": 1, "inflation": 1, "hike": 0.6,
    "tension": 1, "risk": 0.6, "default": 1, "shutdown": 1, "tariff": 1,
}
_NEGATIONS = ("no", "not", "never", "without", "avoids", "eases", "cools")


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def _compound(text: str) -> float:
    """Return a -1..+1 sentiment for one headline."""
    if _vader is not None:
        return _vader.polarity_scores(text)["compound"]
    # --- fallback lexicon with crude negation ---
    words = re.findall(r"[a-z']+", text.lower())
    score = 0.0
    for i, w in enumerate(words):
        pol = _LEX_POS.get(w, 0.0) - _LEX_NEG.get(w, 0.0)
        if pol and i > 0 and words[i - 1] in _NEGATIONS:
            pol = -pol
        score += pol
    # squash to -1..1
    return _clamp(score / 3.0, -1.0, 1.0)


def score(headlines) -> dict:
    """Aggregate a list of headline strings into gold-directional signals."""
    if not headlines:
        return {"backend": "vader" if _vader else "lexicon",
                "fear": 0.0, "gold_direct": 0.0, "tone": 0.0, "n": 0}

    comps = []
    gold_comps = []
    for h in headlines:
        c = _compound(h)
        comps.append(c)
        low = h.lower()
        if any(t in low for t in GOLD_TERMS):
            gold_comps.append(c)

    # World fear = average intensity of NEGATIVE tone across the feed.
    neg = [max(0.0, -c) for c in comps]
    fear = _clamp((sum(neg) / len(neg)) * 1.6, 0.0, 1.0)
    tone = sum(comps) / len(comps)
    gold_direct = (sum(gold_comps) / len(gold_comps)) if gold_comps else 0.0

    return {
        "backend": "vader" if _vader else "lexicon",
        "fear": round(fear, 3),
        "gold_direct": round(_clamp(gold_direct, -1.0, 1.0), 3),
        "tone": round(_clamp(tone, -1.0, 1.0), 3),
        "n": len(headlines),
    }


if __name__ == "__main__":
    demo = [
        "U.S. launches fresh wave of strikes on Iran, forever war fears grow",
        "Gold surges to record high as investors seek safe haven",
        "EU fails to agree on new sanctions against Russia",
        "Stocks rally as inflation cools and Fed signals rate cuts",
    ]
    import pprint
    pprint.pprint(score(demo))
