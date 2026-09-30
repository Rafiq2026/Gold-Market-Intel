"""
Gold Brain AI PRO - AI chart-screenshot analysis (vision).

Takes a chart screenshot the user uploads + our LIVE market context (technical
read, news/geopolitics, COT positioning, next event) and asks our vision engine
for a combined TECHNICAL + FUNDAMENTAL analysis. Our edge over a plain "send
chart to an AI" is that we feed the engine real, current context.

The underlying provider/model is an internal detail and is NEVER exposed to end
users (no provider name in any user-facing text or in the API response). The
model is configured server-side via env vars; without a key the feature reports
that it is temporarily unavailable - nothing is fabricated.

Server-side setup (admin only):
    set GBAI_GEMINI_KEY=your_key      (or GBAI_VISION_KEY)
"""

from __future__ import annotations
import os
import json
import time
import urllib.request
import urllib.error

KEY = os.environ.get("GBAI_GEMINI_KEY") or os.environ.get("GBAI_VISION_KEY") or ""
MODEL = os.environ.get("GBAI_GEMINI_MODEL", "gemini-2.5-flash")
# Try the configured model first, then stable fallbacks. The free tier throws
# 503 "high demand" on any single model at random, so we rotate + retry hard so
# the end user almost never sees a failure.
_FALLBACKS = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.5-flash-lite"]
_URL = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s"

# User-facing fallback message: generic, no provider name, no jargon.
_BUSY = ("The AI analyst is handling a lot of requests at the moment. "
         "Please press Analyze again in a few seconds.")
_OFF = ("AI chart analysis is temporarily unavailable. Please try again later.")

# How the trade horizon / target width scales with the chart timeframe.
_HORIZON = {
    "M1":  ("scalp", "very tight, a few points; minutes-to-an-hour horizon"),
    "M5":  ("scalp", "tight, intraday points; minutes-to-hours horizon"),
    "M15": ("intraday scalp", "tight intraday targets; a few hours horizon"),
    "M30": ("intraday", "intraday targets; same-session horizon"),
    "H1":  ("intraday / short swing", "intraday-to-1-day targets"),
    "H4":  ("swing", "WIDE multi-day targets; a swing trade held for days"),
    "D1":  ("swing / position", "wide targets over days-to-weeks"),
    "W1":  ("position", "very wide targets over weeks-to-months"),
    "MN":  ("position", "macro targets over months"),
}


_GUIDE = ("Calibrate the trade horizon and target width to the chart's ACTUAL timeframe: "
          "M1-M15 = scalp (very tight targets, a few points), M30-H1 = intraday, "
          "H4 = swing (WIDE multi-day targets), D1 = swing/position, W1+ = position.")


def _tf_line(tf):
    tf = (tf or "").upper().strip()
    if tf in _HORIZON:
        # The dropdown is the user's INTENDED timeframe - a cross-check, not a
        # command to relabel. If the image is a different timeframe, warn instead
        # of pretending, because a small-TF image has no large-TF structure.
        return ("The user intends a %s analysis. FIRST read the timeframe label printed on"
                " the chart itself (e.g. 'XAUUSD M5'). If that label clearly differs from"
                " %s, you MUST begin your reply with EXACTLY one line:\n"
                "> Heads-up: this looks like a <actual TF> chart, not %s. For a real %s"
                " read, switch your chart to %s and upload that screenshot. Below is the"
                " read of the chart you provided.\n"
                "Then analyze the chart AS SHOWN at its real visible scale - never inflate a"
                " small-timeframe window into a %s swing. If the label matches %s, analyze"
                " it as %s. %s" % (tf, tf, tf, tf, tf, tf, tf, _HORIZON[tf][0], _GUIDE))
    return "Detect the timeframe from the chart's own label. " + _GUIDE


_PROMPT = (
    "You are a professional price-action analyst. A user uploaded a chart screenshot of"
    " {symbol}. {tf_line}\n"
    "Read the ACTUAL price levels off the chart's price axis. Return the analysis in"
    " EXACTLY this structure, using real numbers from the image, as short bullet lines."
    " Use Markdown headings (##) and bullets ('- '). Keep total under ~230 words.\n\n"
    "## Overview\n"
    "- Timeframe (from chart): <the TF actually shown on the chart> | Trend: <Up / Down /"
    " Sideways> (<weak/strong>)\n"
    "- Bias: <Bullish / Bearish / Neutral> and why in <=12 words\n\n"
    "## Resistance (above price)\n- R1: <price> — <one-line note>\n- R2: <price> — <note>\n\n"
    "## Support (below price)\n- S1: <price> — <note>\n- S2: <price> — <note>\n\n"
    "## Trade Idea (in the trend direction)\n"
    "- Horizon: <scalp / intraday / swing / position — matching the chart's timeframe>\n"
    "- Direction: <Buy / Sell>\n"
    "- Entry zone: <price or range> — <precise trigger, e.g. rejection at R1 / close back below R1>\n"
    "- Stop-loss: <price> — place it JUST BEYOND the level that invalidates THIS entry (for a"
    " sell, a bit above the entry swing-high; for a buy, a bit below the entry swing-low), with"
    " a small buffer for wicks/overshoot. NEVER put it at a far level like the opposite S/R.\n"
    "- Invalidation: a candle CLOSE beyond <the entry level> on this timeframe (an intraday"
    " wick/spike does NOT count) — the stop sits just past that same level.\n"
    "- Take-profit 1: <price>  |  Take-profit 2: <price>\n"
    "- Risk/Reward: ~<x:1> — MUST be >= 1.5:1. If the only clean setup gives less, instead"
    " write 'No clean setup here — wait for price to reach <level>'.\n"
    "## Alternative scenario\n- If the invalidation triggers (a close beyond the entry level),"
    " the bias flips: <opposite direction and its next target>. This is the mirror of the"
    " stop, not a second stop.\n\n"
    "## Fundamental view\n"
    "- Use the LIVE CONTEXT below (news, geopolitics, positioning, next event); say in"
    " 1-2 lines whether fundamentals SUPPORT or OPPOSE the technical bias.\n\n"
    "Rules: base every price on what is visibly on the chart; keep target width consistent"
    " with the chart's real timeframe. Entry, stop-loss and invalidation MUST be mutually"
    " consistent — the stop sits just past the SAME level whose candle-close defines"
    " invalidation, with a buffer for overshoot; a wick through a level is NOT a break."
    " Do NOT mention any AI model or provider. End with exactly this line on its own:\n"
    "_AI analysis - not financial advice; your trade, your responsibility._\n\n"
    "LIVE CONTEXT for {symbol}:\n{context}\n"
)


def available():
    return bool(KEY)


_LANGS = {"en": "English", "fa": "Persian (Farsi)", "de": "German"}


def analyze(image_b64, mime, symbol, context, tf="", lang="en"):
    # 'error' carries an internal code for logs/admin only; 'text' is what the
    # user sees and never names the provider or model.
    if not KEY:
        return {"ok": False, "error": "no_key", "text": _OFF}
    tfU = (tf or "").upper().strip()
    prompt = _PROMPT.format(symbol=symbol, context=(context or "(none)")[:2500],
                            tf_line=_tf_line(tfU))
    lang_name = _LANGS.get((lang or "en").lower(), "English")
    if lang_name != "English":
        prompt += ("\n\nIMPORTANT: Write the ENTIRE analysis — headings and all — in %s. "
                   "Compose it natively and fluently in %s (do NOT translate; no mixed "
                   "language). Keep the '##' heading markers and '- ' bullet markers, and "
                   "keep the final one-line disclaimer, but write them in %s."
                   % (lang_name, lang_name, lang_name))
    body = {
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": mime or "image/png", "data": image_b64}},
        ]}],
        # thinkingBudget 0 disables the 2.5 "thinking" pass so the whole token
        # budget goes to the visible answer (otherwise long structured replies get
        # truncated mid-sentence). Higher cap leaves room for the full layout.
        # Low temperature so the same chart gives a stable, repeatable read
        # (a trading tool must not swing its entry/stop between runs).
        "generationConfig": {"temperature": 0.15, "maxOutputTokens": 1400,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }
    payload = json.dumps(body).encode("utf-8")

    # Configured model first, then stable fallbacks; a few tries each on overload.
    models = [MODEL] + [m for m in _FALLBACKS if m != MODEL]
    last = ""
    for m in models:
        for attempt in range(3):          # three tries per model on transient overload
            try:
                req = urllib.request.Request(_URL % (m, KEY), data=payload,
                                             headers={"Content-Type": "application/json"})
                r = urllib.request.urlopen(req, timeout=60).read()
                d = json.loads(r.decode("utf-8", "ignore"))
                text = d["candidates"][0]["content"]["parts"][0]["text"]
                # NOTE: model name deliberately NOT returned to the client.
                return {"ok": True, "text": text}
            except urllib.error.HTTPError as exc:
                code = exc.code
                try:
                    last = json.loads(exc.read().decode("utf-8", "ignore")).get("error", {}).get("message", "")
                except Exception:
                    last = "HTTP %s" % code
                if code in (503, 429, 500):   # overloaded / rate-limited -> back off, try next
                    time.sleep(1.2 * (attempt + 1))
                    continue
                break                          # 400/404 etc.: don't retry this model
            except Exception as exc:
                last = str(exc)
                time.sleep(1.0)
    # Internal detail stays in 'error'; the user only ever sees the generic _BUSY.
    return {"ok": False, "error": (last or "unavailable")[:160], "text": _BUSY}
