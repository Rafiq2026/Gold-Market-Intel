"""
Gold Market Intelligence - standalone WEB server (cloud-deployable).

Serves ONLY the public Market Intelligence dashboard + its data feeds:
  GET /                 -> dashboard.html
  GET /health
  GET /news
  GET /positioning      (+ /market/positioning)
  GET /events           (+ /market/events)
  GET /history
  GET /quotes
  GET /scorecard
  GET /market/summary

Pure Python standard library - NO scikit-learn / numpy / MT5. (The AI model
/predict endpoint stays on the LOCAL ai_server.py that the EA talks to.)

Runs anywhere Python runs. On a cloud host it binds 0.0.0.0 and reads the port
from $PORT:
    python web_server.py                      # 0.0.0.0:8008
    PORT=10000 python web_server.py           # cloud (Render sets $PORT)
"""

from __future__ import annotations
import os
import json
import hmac
import hashlib
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import news_feed as NF
import positioning_feed as PF
import events_feed as EF
import history_store as HS
import quotes_feed as QF
import scorecard as SC
import analysis as AN
import technicals as TA
import vision as VIS
import chat as CH
import crypto_flow as CFL
import heatmap as HM
import marketsize as MS


# ------------------------------------------------------------------ auth ----
# Shared-password lock for the dashboard. Set GBAI_DASH_PASSWORD (on Render:
# an Environment Variable) to require login; share that one password + the link
# with your friends. If unset, the site is open. /health and /ingest/quote stay
# public (uptime pings + the local MT5 price pusher cannot log in).
_AUTH_PW = os.environ.get("GBAI_DASH_PASSWORD", "")
_AUTH_ON = bool(_AUTH_PW)
_SECRET = os.urandom(32)
_COOKIE = "gbai_auth"
_SESSION_MAXAGE = 7 * 24 * 3600


def _session_token():
    return hmac.new(_SECRET, b"gbai-session-v1", hashlib.sha256).hexdigest()


LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gold Market Intelligence — Sign in</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
<style>
 *{box-sizing:border-box}
 body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
  font-family:'Inter',-apple-system,Segoe UI,Roboto,sans-serif;color:#eef3f8;
  background:radial-gradient(1000px 560px at 82% -10%,rgba(150,90,255,.16),transparent 60%),
             radial-gradient(800px 560px at 8% 110%,rgba(60,120,225,.16),transparent 55%),
             linear-gradient(180deg,#0a0a16,#07070f)}
 .box{width:340px;max-width:calc(100vw - 32px);background:rgba(20,28,44,.75);border:1.5px solid rgba(95,220,255,.5);
  border-radius:16px;padding:26px 24px;backdrop-filter:blur(18px);
  box-shadow:0 0 28px rgba(65,230,255,.2),0 24px 60px -24px rgba(0,0,0,.9)}
 h1{font-size:19px;margin:0 0 4px;font-weight:800}
 h1 b{background:linear-gradient(92deg,#f1c453,#ffe6a0,#e0a02a);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
 p{color:#93a1b0;font-size:12.5px;margin:0 0 20px}
 label{font-size:12px;color:#93a1b0;display:block;margin-bottom:6px}
 input{width:100%;background:rgba(20,30,48,.8);color:#eef3f8;border:1px solid rgba(95,220,255,.35);
  border-radius:10px;padding:11px 12px;font-size:14px;outline:none}
 input:focus{border-color:#41e6ff;box-shadow:0 0 0 3px rgba(65,230,255,.18)}
 button{width:100%;margin-top:16px;background:linear-gradient(92deg,#41e6ff,#00b4d8);color:#04121a;border:0;
  border-radius:10px;padding:12px;font-size:14px;font-weight:800;cursor:pointer;transition:.15s}
 button:hover{filter:brightness(1.08)}
 .err{color:#ff5c6c;font-size:12.5px;margin-top:12px;min-height:16px}
 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#41e6ff;
  box-shadow:0 0 8px #41e6ff;margin-right:7px;vertical-align:middle}
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
    """Compact live context (fundamental + technical) fed to the vision model."""
    try:
        a = AN.build(sym).get("text", "")
    except Exception:
        a = ""
    try:
        t = TA.build(sym, "swing").get("text", "")
    except Exception:
        t = ""
    return (a + ("\nTECHNICAL (our engine): " + t if t else "")).strip()

HERE = os.path.dirname(os.path.abspath(__file__))


class Handler(BaseHTTPRequestHandler):
    server_version = "GoldMarketIntel/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path):
        try:
            with open(os.path.join(HERE, path), "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            self._send(404, {"ok": False, "error": "page not found: %s" % exc})

    # ------------------------------------------------------------ auth utils
    def _cookies(self):
        out = {}
        for part in (self.headers.get("Cookie", "") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def _is_authed(self):
        if not _AUTH_ON:
            return True
        return hmac.compare_digest(self._cookies().get(_COOKIE, ""), _session_token())

    def _redirect(self, loc, cookie=""):
        self.send_response(303)
        self.send_header("Location", loc)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _login_page(self, err=""):
        body = LOGIN_HTML.replace("__ERR__", err).encode("utf-8")
        self.send_response(200 if not err else 401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.rstrip("/")
        # public (no login): login page, logout, health (uptime pings)
        if p == "/login":
            self._login_page(); return
        if p == "/logout":
            self._redirect("/login", _COOKIE + "=; Path=/; Max-Age=0"); return
        if self.path.startswith("/health"):
            self._send(200, {"ok": True, "service": "market-intelligence", "auth": _AUTH_ON,
                             "news": NF.FEED.snapshot().get("ok"),
                             "positioning": PF.FEED.snapshot().get("availability"),
                             "events": EF.FEED.snapshot().get("ok")})
            return
        # everything below requires a valid session when the lock is on
        if not self._is_authed():
            if p == "" or p.startswith("/dashboard") or p.startswith("/app"):
                self._redirect("/login")
            else:
                self._send(401, {"ok": False, "error": "auth required"})
            return
        if p == "" or p.startswith("/dashboard") or p.startswith("/app"):
            self._send_html("dashboard.html")
        elif self.path.startswith("/geo_history"):
            self._send(200, NF.FEED.archive(200, parse_qs(urlparse(self.path).query).get("q", [""])[0]))
        elif self.path.startswith("/news"):
            self._send(200, NF.FEED.snapshot())
        elif self.path.startswith("/positioning") or self.path.startswith("/market/positioning"):
            sym = (parse_qs(urlparse(self.path).query).get("symbol", ["XAUUSD"])[0])
            self._send(200, PF.FEED.snapshot_for(sym))
        elif self.path.startswith("/events") or self.path.startswith("/market/events"):
            self._send(200, EF.FEED.snapshot())
        elif self.path.startswith("/history"):
            self._send(200, HS.FEED.snapshot())
        elif self.path.startswith("/quotes"):
            self._send(200, QF.FEED.snapshot())
        elif self.path.startswith("/series"):
            sym = (parse_qs(urlparse(self.path).query).get("symbol", ["XAUUSD"])[0])
            self._send(200, {"ok": True, "symbol": sym.upper(), "series": QF.FEED.series(sym)})
        elif self.path.startswith("/ohlc"):
            qp = parse_qs(urlparse(self.path).query)
            sym = qp.get("symbol", ["XAUUSD"])[0]; tf = qp.get("tf", ["intraday"])[0]
            _oc = QF.FEED.ohlc(sym, tf)
            self._send(200, {"ok": True, "symbol": sym.upper(), "tf": tf,
                             "candles": _oc.get("candles", []), "source": _oc.get("source", "")})
        elif self.path.startswith("/chat/status"):
            cid = self._cookies().get(_COOKIE) or self.client_address[0]
            self._send(200, {"ok": True, "available": CH.available(),
                             "remaining": CH.remaining(cid), "per_max": CH.PER_CLIENT_MAX})
        elif self.path.startswith("/scorecard"):
            sym = (parse_qs(urlparse(self.path).query).get("symbol", ["XAUUSD"])[0])
            self._send(200, SC.FEED.snapshot_for(sym))
        elif self.path.startswith("/analysis"):
            qp = parse_qs(urlparse(self.path).query)
            sym = qp.get("symbol", ["XAUUSD"])[0]
            self._send(200, AN.build(sym, qp.get("lang", ["en"])[0]))
        elif self.path.startswith("/technicals"):
            qp = parse_qs(urlparse(self.path).query)
            self._send(200, TA.build(qp.get("symbol", ["XAUUSD"])[0], qp.get("tf", ["swing"])[0], qp.get("lang", ["en"])[0]))
        elif self.path.startswith("/cryptoflow"):
            sym = (parse_qs(urlparse(self.path).query).get("symbol", ["BTCUSD"])[0])
            self._send(200, CFL.FEED.snapshot_for(sym))
        elif self.path.startswith("/heatmap"):
            self._send(200, HM.FEED.snapshot())
        elif self.path.startswith("/quote_any"):
            q = (parse_qs(urlparse(self.path).query).get("q", [""])[0])
            self._send(200, HM.quote_any(q))
        elif self.path.startswith("/marketsize"):
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
        # login (public)
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
                self._redirect("/")
            else:
                self._login_page("Wrong password — try again.")
            return
        # vision (requires login)
        if self.path.startswith("/vision"):
            if not self._is_authed():
                self._send(401, {"ok": False, "error": "auth required"}); return
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n).decode("utf-8")) if n > 0 else {}
            except Exception:
                d = {}
            sym = (d.get("symbol") or "XAUUSD").upper()
            self._send(200, VIS.analyze(d.get("image", ""), d.get("mime", "image/png"),
                                        sym, _vision_context(sym), d.get("tf", ""), d.get("lang", "en")))
            return
        if self.path.startswith("/chat"):
            if not self._is_authed():
                self._send(401, {"ok": False, "error": "auth required"}); return
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n).decode("utf-8")) if n > 0 else {}
            except Exception:
                d = {}
            sym = (d.get("symbol") or "XAUUSD").upper()
            cid = self._cookies().get(_COOKIE) or self.client_address[0]
            self._send(200, CH.reply(d.get("message", ""), d.get("history", []),
                                     _vision_context(sym), cid, d.get("lang", "en")))
            return
        # Live broker-quote ingest from the local MT5 pusher (push_quote.py), so
        # the cloud gold price matches MetaTrader exactly while the PC is on.
        if self.path.startswith("/ingest/quote"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n).decode("utf-8")) if n > 0 else {}
            except Exception:
                d = {}
            tok = os.environ.get("GBAI_INGEST_TOKEN", "")
            if tok and d.get("token") != tok:
                self._send(403, {"ok": False, "error": "bad token"})
                return
            self._send(200, {"ok": QF.FEED.ingest(d)})
        # Live economic calendar pushed from the local PC (the datacenter IP can be
        # blocked by the calendar CDN, so the local machine feeds it to the cloud).
        elif self.path.startswith("/ingest/calendar"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n).decode("utf-8")) if n > 0 else {}
            except Exception:
                d = {}
            tok = os.environ.get("GBAI_INGEST_TOKEN", "")
            if tok and d.get("token") != tok:
                self._send(403, {"ok": False, "error": "bad token"})
                return
            ok = EF.FEED.ingest_calendar(d.get("data") or [])
            if ok:
                try:
                    EF.FEED.refresh()   # rebuild the events list from the fresh calendar now
                except Exception:
                    pass
            self._send(200, {"ok": ok})
        # Real broker OHLC bars pushed from the local PC (for the live candle chart).
        elif self.path.startswith("/ingest/bars"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n).decode("utf-8")) if n > 0 else {}
            except Exception:
                d = {}
            tok = os.environ.get("GBAI_INGEST_TOKEN", "")
            if tok and d.get("token") != tok:
                self._send(403, {"ok": False, "error": "bad token"})
                return
            self._send(200, {"ok": QF.FEED.ingest_bars(d.get("bars") or {})})
        else:
            self._send(404, {"ok": False, "error": "not found"})


def main():
    host = os.environ.get("GBAI_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", os.environ.get("GBAI_PORT", 8008)))
    print("[MarketIntel] starting feeds (news, COT, events, quotes, history, scorecard)...")
    NF.FEED.start()
    PF.FEED.start()
    EF.FEED.start()
    HS.FEED.start()
    QF.FEED.start()
    SC.FEED.start()
    CFL.FEED.start()
    HM.FEED.start()
    srv = ThreadingHTTPServer((host, port), Handler)
    print("[MarketIntel] dashboard live on http://%s:%d/" % (host, port))
    print("[MarketIntel] login lock: %s" % ("ON (GBAI_DASH_PASSWORD set)" if _AUTH_ON
          else "OFF — set GBAI_DASH_PASSWORD to require a password"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
