"""
Gold Brain AI PRO - server-side AI translation for the generated analysis prose.

The AI-analyst and technical paragraphs are built in English from live data. When
the dashboard language is not English we translate the finished paragraph with the
same model the chat uses, so the result is NATIVE and fluent (not a phrase-by-phrase
template). To keep the free key safe, translations are cached GLOBALLY (shared by
all viewers) per (cache_key, lang) with a short TTL — so however often the paragraph
refreshes, we call the model at most ~once per key per TTL. On any failure we fall
back to the last good translation, else the original English (never blank).

Provider/model is an internal detail and is never exposed to end users.
"""

from __future__ import annotations
import time
import json
import threading
import urllib.request
import urllib.error

import chat as _CH   # reuse the same key/model/endpoint as the chat feature

_LANGS = {"fa": "Persian (Farsi)", "de": "German"}
TTL = 180                      # seconds a cached translation is reused (bounds quota)
_lock = threading.Lock()
_cache = {}                    # (cache_key, lang) -> (ts, translated_text)

_PROMPT = (
    "Translate the following gold/FX market analysis into {lang}. Write it natively "
    "and fluently in {lang}, as a professional market commentator would — do NOT translate "
    "word for word. Preserve EXACTLY, unchanged: all numbers, price levels, percentages, "
    "dates, currency codes (USD, XAUUSD…), and proper nouns (economic-event names, indicator "
    "and data-source names such as FRED, CFTC, Fed). Keep the same paragraph/line structure. "
    "Output ONLY the translation — no preamble, no notes, no quotes.\n\n---\n{text}"
)


def available() -> bool:
    return bool(_CH.KEY)


def _call(text, lang_name):
    body = {
        "contents": [{"role": "user", "parts": [{"text": _PROMPT.format(lang=lang_name, text=text)}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 900,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }
    payload = json.dumps(body).encode("utf-8")
    models = [_CH.MODEL] + [m for m in _CH._FALLBACKS if m != _CH.MODEL]
    for m in models:
        for attempt in range(2):
            try:
                req = urllib.request.Request(_CH._URL % (m, _CH.KEY), data=payload,
                                             headers={"Content-Type": "application/json"})
                r = urllib.request.urlopen(req, timeout=30).read()
                d = json.loads(r.decode("utf-8", "ignore"))
                return d["candidates"][0]["content"]["parts"][0]["text"].strip()
            except urllib.error.HTTPError as exc:
                if exc.code in (503, 429, 500):
                    time.sleep(1.0 * (attempt + 1)); continue
                break
            except Exception:
                time.sleep(0.8)
    return None


def to_lang(text, lang, cache_key, ttl=TTL):
    """Translate `text` to `lang` (en = passthrough). Globally cached per (cache_key, lang)
    so live-refreshing prose costs at most one model call per key per `ttl`."""
    lang = (lang or "en").lower()
    if lang == "en" or lang not in _LANGS or not text or not _CH.KEY:
        return text
    ck = (cache_key, lang)
    now = time.time()
    with _lock:
        hit = _cache.get(ck)
        if hit and (now - hit[0]) < ttl:
            return hit[1]
    out = _call(text, _LANGS[lang])
    with _lock:
        if not out:
            hit = _cache.get(ck)
            return hit[1] if hit else text        # keep last good, else English
        _cache[ck] = (now, out)
    return out
