"""
Gold Brain AI PRO - local -> cloud broker-quote pusher.

Reads the live BROKER quote that the EA writes to the MT5 Files folder and
POSTs it to the cloud dashboard, so the cloud site shows the SAME gold price
as your MetaTrader (while this PC is on). When this stops, the cloud falls
back to a public gold feed automatically.

Usage (Windows, keep the window open):
    set GBAI_CLOUD_URL=https://gold-market-intel.onrender.com
    set GBAI_INGEST_TOKEN=your-secret
    python push_quote.py

Or pass the URL as an argument:
    python push_quote.py https://gold-market-intel.onrender.com your-secret
"""

from __future__ import annotations
import os
import sys
import json
import time
import urllib.request

MT5_QUOTE = os.environ.get("GBAI_MT5_QUOTE",
    r"C:\Users\ziaal\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\MQL5\Files\GoldBrainAI_Quote.txt")
CLOUD_URL = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GBAI_CLOUD_URL", "")).rstrip("/")
TOKEN = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("GBAI_INGEST_TOKEN", "")
INTERVAL = 5


def read_quote():
    with open(MT5_QUOTE, "r", encoding="ascii", errors="ignore") as fh:
        p = fh.read().strip().split(",")
    return {"symbol": "XAUUSD", "bid": float(p[0]), "ask": float(p[1]),
            "prevClose": float(p[3]) if len(p) > 3 else float(p[0]), "token": TOKEN}


def main():
    if not CLOUD_URL:
        print("Set GBAI_CLOUD_URL (your Render URL) or pass it as arg 1."); return
    print("[push] MT5 %s -> %s  every %ss" % (MT5_QUOTE, CLOUD_URL, INTERVAL))
    last = None
    while True:
        try:
            q = read_quote()
            if q["bid"] != last:
                body = json.dumps(q).encode("utf-8")
                req = urllib.request.Request(CLOUD_URL + "/ingest/quote", data=body,
                                             headers={"Content-Type": "application/json"})
                r = urllib.request.urlopen(req, timeout=8).read()
                last = q["bid"]
                print("[push] %.2f -> %s" % (q["bid"], r.decode()[:40]))
        except Exception as e:
            print("[push] err:", repr(e)[:80])
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
