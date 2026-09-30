"""
Gold Brain AI PRO - AI confidence model HTTP server.

A tiny, dependency-free HTTP service that the MQL5 EA calls via WebRequest.
Runs on a stock Python install (uses only the standard library). If a trained
model.pkl (+ scikit-learn/joblib) is present it is used automatically.

Endpoints:
  POST /predict   body: JSON feature dict  -> {ok,p_up,bull,bear,confidence,source}
  GET  /health                             -> {ok,model_loaded,source}

Run:
  python ai_server.py                 # binds 127.0.0.1:8008
  python ai_server.py --port 8008 --host 127.0.0.1

In MetaTrader 5, allow the URL under:
  Tools > Options > Expert Advisors > "Allow WebRequest for listed URL"
  -> add  http://127.0.0.1:8008
"""

from __future__ import annotations
import argparse
import json
import os
import hmac
import hashlib
from urllib.parse import parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import model as M
import news_feed as NF
import positioning_feed as PF
import events_feed as EF
import history_store as HS
import quotes_feed as QF
import scorecard as SC
import analysis as AN
import technicals as TA
import vision as VIS
import crypto_flow as CFL
import heatmap as HM
import marketsize as MS


# ------------------------------------------------------------------ auth ----
# A simple password lock for the DASHBOARD (not the EA). Set the password once:
#   setx GBAI_DASH_PASSWORD "your-strong-pass"   (then restart the server)
# If it is unset, the lock is OFF (open) so you can never lock yourself out by
# accident. The EA endpoints /predict and /health stay PUBLIC either way, because
# MetaTrader's WebRequest cannot log in (and the watchdog pings /health).
_AUTH_PW = os.environ.get("GBAI_DASH_PASSWORD", "")
_AUTH_ON = bool(_AUTH_PW)
_SECRET = os.urandom(32)                       # new per server start (restart = re-login)
_COOKIE = "gbai_auth"
_SESSION_MAXAGE = 7 * 24 * 3600                # stay logged in ~7 days


def _session_token() -> str:
    return hmac.new(_SECRET, b"gbai-session-v1", hashlib.sha256).hexdigest()


LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gold Market Intelligence — Sign in</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
<style>
 *{box-sizing:border-box}
 body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
  font-family:'Inter',-apple-system,Segoe UI,Roboto,sans-serif;color:#eef3f8;
  background:radial-gradient(1000px 560px at 82% -10%,rgba(241,196,83,.12),transparent 60%),
             radial-gradient(800px 560px at 8% 110%,rgba(56,120,220,.14),transparent 55%),
             linear-gradient(180deg,#0a0d13,#080a0f)}
 .box{width:340px;max-width:calc(100vw - 32px);background:rgba(20,25,33,.7);border:1px solid rgba(255,255,255,.1);
  border-radius:18px;padding:26px 24px;backdrop-filter:blur(18px);
  box-shadow:0 24px 60px -24px rgba(0,0,0,.9)}
 h1{font-size:19px;margin:0 0 4px;font-weight:800}
 h1 b{background:linear-gradient(92deg,#f1c453,#ffe6a0,#e0a02a);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
 p{color:#93a1b0;font-size:12.5px;margin:0 0 20px}
 label{font-size:12px;color:#93a1b0;display:block;margin-bottom:6px}
 input{width:100%;background:rgba(28,36,48,.7);color:#eef3f8;border:1px solid rgba(255,255,255,.12);
  border-radius:10px;padding:11px 12px;font-size:14px;outline:none}
 input:focus{border-color:#f1c453;box-shadow:0 0 0 3px rgba(241,196,83,.18)}
 button{width:100%;margin-top:16px;background:linear-gradient(92deg,#f1c453,#e0a02a);color:#151515;border:0;
  border-radius:10px;padding:12px;font-size:14px;font-weight:800;cursor:pointer;transition:.15s}
 button:hover{filter:brightness(1.06)}
 .err{color:#ff5c6c;font-size:12.5px;margin-top:12px;min-height:16px}
 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#37d67a;
  box-shadow:0 0 8px #37d67a;margin-right:7px;vertical-align:middle}
</style></head><body>
 <form class="box" method="POST" action="/login">
  <h1><span class="dot"></span><b>Gold</b> Market Intelligence</h1>
  <p>Private dashboard — please sign in.</p>
  <label for="pw">Password</label>
  <input id="pw" name="password" type="password" autofocus autocomplete="current-password" placeholder="••••••••">
  <button type="submit">Sign in</button>
  <div class="err">__ERR__</div>
 </form>
</body></html>"""


def _vision_context(sym):
    try:
        a = AN.build(sym).get("text", "")
    except Exception:
        a = ""
    try:
        t = TA.build(sym, "swing").get("text", "")
    except Exception:
        t = ""
    return (a + ("\nTECHNICAL (our engine): " + t if t else "")).strip()

# How strongly live geopolitical safe-haven bias nudges the model probability.
NEWS_BLEND = 0.12
# How strongly weekly CFTC positioning nudges it (confirmation, not a trigger).
COT_BLEND = 0.06


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


class Handler(BaseHTTPRequestHandler):
    server_version = "GoldBrainAI/1.0"
    # HTTP/1.1 keep-alive so MetaTrader's WinINet client can reuse the
    # connection. With HTTP/1.0 the server closed each socket and MT5's
    # reused (stale) connection failed on every other WebRequest (err 1003).
    protocol_version = "HTTP/1.1"

    # Append a concise access line so we can confirm the EA is reaching us.
    def log_message(self, fmt, *args):
        try:
            with open("requests.log", "a", encoding="utf-8") as fh:
                fh.write("%s %s %s\n" % (self.address_string(),
                                         self.log_date_time_string(),
                                         (fmt % args)))
        except Exception:
            pass

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: str):
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(here, path), "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            self._send(404, {"ok": False, "error": "page not found: %s" % exc})

    # ------------------------------------------------------------ auth utils
    def _cookies(self) -> dict:
        out = {}
        for part in (self.headers.get("Cookie", "") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def _is_authed(self) -> bool:
        if not _AUTH_ON:
            return True
        return hmac.compare_digest(self._cookies().get(_COOKIE, ""), _session_token())

    def _redirect(self, loc: str, cookie: str = ""):
        self.send_response(303)
        self.send_header("Location", loc)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _login_page(self, err: str = ""):
        body = LOGIN_HTML.replace("__ERR__", err).encode("utf-8")
        self.send_response(200 if not err else 401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.rstrip("/")
        # --- public endpoints (no login needed) ---
        if p == "/login":
            self._login_page()
            return
        if p == "/logout":
            self._redirect("/login", _COOKIE + "=; Path=/; Max-Age=0")
            return
        if self.path.startswith("/health"):  # EA + watchdog + status: always open
            loaded = M._load_model() is not None
            self._send(200, {"ok": True, "model_loaded": loaded,
                             "source": "model" if loaded else "heuristic",
                             "auth": _AUTH_ON,
                             "news": NF.FEED.snapshot(),
                             "positioning": PF.FEED.snapshot()})
            return
        # --- everything below requires a valid session when the lock is on ---
        if not self._is_authed():
            if p == "" or p.startswith("/dashboard") or p.startswith("/app"):
                self._redirect("/login")
            else:
                self._send(401, {"ok": False, "error": "auth required"})
            return
        if p == "" or p.startswith("/dashboard") or p.startswith("/app"):
            self._send_html("dashboard.html")
            return
        if False:  # (health handled above)
            loaded = M._load_model() is not None
            self._send(200, {"ok": True, "model_loaded": loaded,
                             "source": "model" if loaded else "heuristic",
                             "news": NF.FEED.snapshot(),
                             "positioning": PF.FEED.snapshot()})
        elif self.path.startswith("/news"):
            self._send(200, NF.FEED.snapshot())
        elif self.path.startswith("/positioning") or self.path.startswith("/market/positioning"):
            from urllib.parse import urlparse, parse_qs
            sym = (parse_qs(urlparse(self.path).query).get("symbol", ["XAUUSD"])[0])
            self._send(200, PF.FEED.snapshot_for(sym))
        elif self.path.startswith("/events") or self.path.startswith("/market/events"):
            self._send(200, EF.FEED.snapshot())
        elif self.path.startswith("/history"):
            self._send(200, HS.FEED.snapshot())
        elif self.path.startswith("/quotes"):
            self._send(200, QF.FEED.snapshot())
        elif self.path.startswith("/series"):
            from urllib.parse import urlparse, parse_qs
            sym = (parse_qs(urlparse(self.path).query).get("symbol", ["XAUUSD"])[0])
            self._send(200, {"ok": True, "symbol": sym.upper(), "series": QF.FEED.series(sym)})
        elif self.path.startswith("/ohlc"):
            from urllib.parse import urlparse, parse_qs
            qp = parse_qs(urlparse(self.path).query)
            sym = qp.get("symbol", ["XAUUSD"])[0]; tf = qp.get("tf", ["intraday"])[0]
            _oc = QF.FEED.ohlc(sym, tf)
            self._send(200, {"ok": True, "symbol": sym.upper(), "tf": tf,
                             "candles": _oc.get("candles", []), "source": _oc.get("source", "")})
        elif self.path.startswith("/scorecard"):
            from urllib.parse import urlparse, parse_qs
            sym = (parse_qs(urlparse(self.path).query).get("symbol", ["XAUUSD"])[0])
            self._send(200, SC.FEED.snapshot_for(sym))
        elif self.path.startswith("/analysis"):
            from urllib.parse import urlparse, parse_qs
            sym = (parse_qs(urlparse(self.path).query).get("symbol", ["XAUUSD"])[0])
            self._send(200, AN.build(sym))
        elif self.path.startswith("/technicals"):
            from urllib.parse import urlparse, parse_qs
            qp = parse_qs(urlparse(self.path).query)
            self._send(200, TA.build(qp.get("symbol", ["XAUUSD"])[0], qp.get("tf", ["swing"])[0]))
        elif self.path.startswith("/cryptoflow"):
            from urllib.parse import urlparse, parse_qs
            sym = (parse_qs(urlparse(self.path).query).get("symbol", ["BTCUSD"])[0])
            self._send(200, CFL.FEED.snapshot_for(sym))
        elif self.path.startswith("/heatmap"):
            self._send(200, HM.FEED.snapshot())
        elif self.path.startswith("/quote_any"):
            from urllib.parse import urlparse, parse_qs
            q = (parse_qs(urlparse(self.path).query).get("q", [""])[0])
            self._send(200, HM.quote_any(q))
        elif self.path.startswith("/marketsize"):
            from urllib.parse import urlparse, parse_qs
            sym = (parse_qs(urlparse(self.path).query).get("symbol", ["XAUUSD"])[0])
            self._send(200, MS.snapshot_for(sym))
        elif self.path.startswith("/market/summary"):
            self._send(200, {"ok": True,
                             "news": NF.FEED.snapshot(),
                             "positioning": PF.FEED.snapshot(),
                             "events": EF.FEED.snapshot()})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        # --- login (public) ---
        if self.path.rstrip("/") == "/login":
            try:
                n = int(self.headers.get("Content-Length", 0))
                form = parse_qs(self.rfile.read(n).decode("utf-8")) if n > 0 else {}
            except Exception:
                form = {}
            pw = (form.get("password", [""])[0])
            if _AUTH_ON and hmac.compare_digest(pw, _AUTH_PW):
                cookie = "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax" % (
                    _COOKIE, _session_token(), _SESSION_MAXAGE)
                self._redirect("/", cookie)
            elif not _AUTH_ON:
                self._redirect("/")            # lock disabled -> just go in
            else:
                self._login_page("Wrong password — try again.")
            return
        # --- EA prediction (public: MetaTrader cannot log in) ---
        if self.path.startswith("/predict"):
            self._do_predict()
            return
        # --- everything else needs a session ---
        if not self._is_authed():
            self._send(401, {"ok": False, "error": "auth required"})
            return
        if self.path.startswith("/vision"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n).decode("utf-8")) if n > 0 else {}
            except Exception:
                d = {}
            sym = (d.get("symbol") or "XAUUSD").upper()
            self._send(200, VIS.analyze(d.get("image", ""), d.get("mime", "image/png"),
                                        sym, _vision_context(sym), d.get("tf", "")))
            return
        self._send(404, {"ok": False, "error": "not found"})

    def _do_predict(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            features = json.loads(raw.decode("utf-8"))
            if not isinstance(features, dict):
                raise ValueError("payload must be a JSON object")

            # Base model probability.
            p_model, source = M.predict_p_up(features)
            # Fold live geopolitical safe-haven bias into the probability.
            news = NF.FEED.snapshot()
            pos = PF.FEED.snapshot()
            # Weekly CFTC positioning: only a fresh, live/cache reading nudges.
            cot_bias = pos.get("bias", 0.0) if pos.get("report_date") else 0.0
            p_up = _clamp(p_model
                          + NEWS_BLEND * news.get("geo_bias", 0.0)
                          + COT_BLEND * cot_bias, 0.0, 1.0)
            conv = abs(p_up - 0.5) * 2.0

            self._send(200, {
                "ok": True,
                "p_up": round(p_up, 4),
                "p_up_model": round(p_model, 4),
                "bull": round(conv if p_up > 0.5 else 0.0, 4),
                "bear": round(conv if p_up < 0.5 else 0.0, 4),
                "confidence": round(conv * 100.0, 2),
                "source": source,
                # --- live news fields consumed by the EA ---
                "news_blocked": 1 if news.get("econ_blocked") else 0,
                "geo_risk": news.get("geo_risk", 0.0),
                "geo_bias": news.get("geo_bias", 0.0),
                "next_minutes": news.get("next_minutes", -1),
                "next_event": news.get("next_event", ""),
                "headline": news.get("headline", ""),
                "news_ok": 1 if news.get("ok") else 0,
                # --- weekly CFTC positioning (context, labelled) ---
                "cot_bias": cot_bias,
                "cot_label": pos.get("label", "n/a"),
                "cot_net": pos.get("noncomm_net", 0),
                "cot_pct_oi": pos.get("spec_net_pct_oi", 0.0),
                "cot_date": pos.get("report_date", ""),
                "cot_conf": pos.get("confidence", 0),
            })
        except Exception as exc:  # never 500 the EA - degrade cleanly
            self._send(200, {"ok": False, "error": str(exc),
                             "p_up": 0.5, "bull": 0.0, "bear": 0.0,
                             "confidence": 0.0, "source": "error"})


def main():
    ap = argparse.ArgumentParser(description="Gold Brain AI model server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("GBAI_PORT", 8008)))
    args = ap.parse_args()

    loaded = M._load_model() is not None
    print("[GoldBrainAI] starting live news feed (economic calendar + geo headlines)...")
    NF.FEED.start()
    snap = NF.FEED.snapshot()
    print(f"[GoldBrainAI] news ready: ok={snap['ok']} geo_risk={snap['geo_risk']} "
          f"blocked={snap['econ_blocked']} next='{snap['next_event']}' in {snap['next_minutes']}m")
    print("[GoldBrainAI] starting CFTC positioning feed (weekly COT, COMEX gold)...")
    PF.FEED.start()
    pos = PF.FEED.snapshot()
    print(f"[GoldBrainAI] positioning: avail={pos['availability']} {pos['label']} "
          f"net={pos['noncomm_net']} ({pos['spec_net_pct_oi']}% OI) as-of {pos['report_date'] or 'n/a'}")
    print("[GoldBrainAI] starting economic-events feed (calendar + gold-impact rules)...")
    EF.FEED.start()
    print("[GoldBrainAI] starting composite-history sampler (chart persistence)...")
    HS.FEED.start()
    print("[GoldBrainAI] starting live quotes (gold GC=F + USDX DX-Y.NYB)...")
    QF.FEED.start()
    print("[GoldBrainAI] starting prediction scorecard (auto-grades past events)...")
    SC.FEED.start()
    print("[GoldBrainAI] starting crypto perp flow (Hyperliquid funding/OI)...")
    CFL.FEED.start()
    print("[GoldBrainAI] starting cross-asset heatmap (USD/yields/equities/oil/crypto)...")
    HM.FEED.start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[GoldBrainAI] model server on http://{args.host}:{args.port}  "
          f"(source={'model' if loaded else 'heuristic'})")
    print("[GoldBrainAI] dashboard: http://%s:%d/  (Gold Market Intelligence)" % (args.host, args.port))
    if _AUTH_ON:
        print("[GoldBrainAI] dashboard LOCK: ON (password required). EA /predict + /health stay public.")
    else:
        print("[GoldBrainAI] dashboard LOCK: OFF. Set GBAI_DASH_PASSWORD to require a login.")
    print("[GoldBrainAI] endpoints: POST /predict, GET /health, /news, /positioning, /events, /market/summary")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[GoldBrainAI] shutting down")
        srv.shutdown()


if __name__ == "__main__":
    main()
