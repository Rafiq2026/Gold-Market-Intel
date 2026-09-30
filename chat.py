"""
Gold Brain AI PRO - live market Q&A chat (text, stdlib only).

A small in-dashboard assistant that answers the user's gold/FX questions using our
LIVE market context (the same context the AI analyst sees). The provider/model is
an internal detail and is NEVER exposed to end users. Without a key configured the
feature reports that it is temporarily unavailable - nothing is fabricated.

Rate-limited per client (and globally per day) so a single shared free key can be
used by several friends without blowing the quota. Only SUCCESSFUL answers count
against the limit.

Server-side setup (admin only) - same key as the vision feature:
    set GBAI_GEMINI_KEY=your_key      (or GBAI_VISION_KEY)
Optional tuning:
    GBAI_CHAT_CLIENT_MAX (default 15)  questions per client per window
    GBAI_CHAT_WINDOW     (default 3600) window length in seconds
    GBAI_CHAT_DAY_MAX    (default 400)  global questions per day (quota guard)
"""

from __future__ import annotations
import os
import json
import time
import threading
import urllib.request
import urllib.error

KEY = os.environ.get("GBAI_GEMINI_KEY") or os.environ.get("GBAI_VISION_KEY") or ""
MODEL = os.environ.get("GBAI_GEMINI_MODEL", "gemini-2.5-flash")
_FALLBACKS = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.5-flash-lite"]
_URL = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s"

PER_CLIENT_MAX = int(os.environ.get("GBAI_CHAT_CLIENT_MAX", "20"))    # per client, per DAY
WINDOW_SEC = int(os.environ.get("GBAI_CHAT_WINDOW", "86400"))         # 24h rolling reset
GLOBAL_DAY_MAX = int(os.environ.get("GBAI_CHAT_DAY_MAX", "250"))      # whole-site quota guard/day

# Chat answer language (AI-native, NOT machine translation).
_LANGS = {"en": "English", "fa": "Persian (Farsi)", "de": "German"}

_lock = threading.Lock()
_hits = {}          # client_id -> [timestamps of successful answers]
_day = [0, 0.0]     # [count today, day-window start ts]

_BUSY = "The assistant is busy right now — please try again in a few seconds."
_OFF = "Chat is temporarily unavailable."
_LIMIT = "You've reached the question limit for now — please try again a bit later."

_SYS = (
    "You are Gold Brain AI, a concise gold & FX market assistant embedded in a live "
    "trading dashboard. Answer the user's question directly in 140 words or less, in "
    "plain language, and lean on the LIVE CONTEXT block when it is relevant. You may "
    "discuss price levels, news, economic events and technical/fundamental reasoning. "
    "Give a clear view rather than hedging endlessly, but never promise a guaranteed "
    "outcome. Do NOT mention any AI model, provider or that you are an AI. If asked "
    "directly for financial advice, answer with analysis and end with a short "
    "'Not financial advice.' note."
)


def available() -> bool:
    return bool(KEY)


def _prune(client_id, now):
    lst = [t for t in _hits.get(client_id, []) if now - t < WINDOW_SEC]
    _hits[client_id] = lst
    return lst


def remaining(client_id) -> int:
    now = time.time()
    with _lock:
        return max(0, PER_CLIENT_MAX - len(_prune(client_id, now)))


def _has_room(client_id) -> bool:
    now = time.time()
    with _lock:
        if now - _day[1] > 86400:
            _day[0] = 0
            _day[1] = now
        if _day[0] >= GLOBAL_DAY_MAX:
            return False
        return len(_prune(client_id, now)) < PER_CLIENT_MAX


def _consume(client_id):
    now = time.time()
    with _lock:
        _hits.setdefault(client_id, []).append(now)
        _day[0] += 1


def reply(message, history, context, client_id="anon", lang="en") -> dict:
    """Answer one question. history = [{"role":"user"|"model","text":...}, ...]."""
    if not KEY:
        return {"ok": False, "text": _OFF, "remaining": 0}
    message = (message or "").strip()[:700]
    if not message:
        return {"ok": False, "text": "Ask a question to begin.", "remaining": remaining(client_id)}
    if not _has_room(client_id):
        return {"ok": False, "limited": True, "text": _LIMIT, "remaining": 0}

    lang_name = _LANGS.get((lang or "en").lower(), "English")
    sys_text = _SYS + (" IMPORTANT: Write your ENTIRE reply in %s, regardless of the language "
                       "the user wrote in. Compose it natively and fluently in %s — do NOT "
                       "translate; use no other language and no mixed or awkward phrasing."
                       % (lang_name, lang_name))

    contents = []
    for h in (history or [])[-6:]:
        role = "user" if h.get("role") == "user" else "model"
        t = str(h.get("text", ""))[:600]
        if t:
            contents.append({"role": role, "parts": [{"text": t}]})
    user_text = message
    if context:
        user_text += "\n\n[LIVE CONTEXT]\n" + str(context)[:2200]
    contents.append({"role": "user", "parts": [{"text": user_text}]})

    body = {
        "systemInstruction": {"parts": [{"text": sys_text}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 600,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }
    payload = json.dumps(body).encode("utf-8")

    models = [MODEL] + [m for m in _FALLBACKS if m != MODEL]
    last = ""
    for m in models:
        for attempt in range(3):
            try:
                req = urllib.request.Request(_URL % (m, KEY), data=payload,
                                             headers={"Content-Type": "application/json"})
                r = urllib.request.urlopen(req, timeout=45).read()
                d = json.loads(r.decode("utf-8", "ignore"))
                text = d["candidates"][0]["content"]["parts"][0]["text"]
                _consume(client_id)          # only a real answer counts against the limit
                return {"ok": True, "text": text, "remaining": remaining(client_id)}
            except urllib.error.HTTPError as exc:
                code = exc.code
                try:
                    last = json.loads(exc.read().decode("utf-8", "ignore")).get("error", {}).get("message", "")
                except Exception:
                    last = "HTTP %s" % code
                if code in (503, 429, 500):
                    time.sleep(1.2 * (attempt + 1))
                    continue
                break
            except Exception as exc:
                last = str(exc)
                time.sleep(1.0)
    return {"ok": False, "text": _BUSY, "error": (last or "unavailable")[:160],
            "remaining": remaining(client_id)}
