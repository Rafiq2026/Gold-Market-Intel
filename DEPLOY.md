# Deploy the Gold Market Intelligence dashboard to the cloud (free)

The dashboard is a pure-Python web app (`web_server.py`) with **no external
dependencies**. Once deployed it runs 24/7 on a public URL — it keeps working
even when your laptop is off, and friends can open it from anywhere.

(The trading EA and the AI-model `/predict` endpoint stay LOCAL on your PC —
only this analysis dashboard goes to the cloud.)

## Files that get deployed
`web_server.py`, `dashboard.html`, `news_feed.py`, `positioning_feed.py`,
`events_feed.py`, `macro_history.py`, `quotes_feed.py`, `history_store.py`,
`scorecard.py`, `sentiment.py`, `Dockerfile`, `render.yaml`.

## Option A — Render.com (recommended, free, no credit card)

1. **Create a GitHub repo** (free): github.com → New repository → e.g.
   `gold-market-intel` → Create. Then "uploading an existing file" → drag in
   ALL the files listed above (from this `python` folder) → Commit.

2. **Create a Render account** (free): render.com → sign up with GitHub.

3. **Deploy:** Render dashboard → **New + → Blueprint** → pick your
   `gold-market-intel` repo. Render reads `render.yaml` and builds the
   Docker image automatically → **Apply / Deploy**.

4. After ~2–3 min you get a public URL like
   `https://gold-market-intel.onrender.com` — that is your website. Open it
   on any device; share it with friends.

## Keep it awake (so the scorecard keeps grading)

Render's free tier sleeps after ~15 min of no visits. To keep it always on:
- uptimerobot.com (free) → add a **HTTP(s) monitor** → your URL + `/health`
  → check every 5 minutes. This pings it so it never sleeps, and the
  scorecard grades events on schedule.

## Make the cloud gold price match YOUR MetaTrader (optional but recommended)

By default the cloud shows gold from Yahoo (COMEX futures, ~$40 above spot).
To make it match your broker EXACTLY, run the tiny pusher on your PC — it sends
your live MT5 quote to the cloud every 5s:

1. On Render → your service → Environment → add `GBAI_INGEST_TOKEN` = some secret.
2. On your PC (keep the window open, alongside start_server.bat):
   ```
   python push_quote.py https://gold-market-intel.onrender.com your-secret
   ```
   The cloud gold now equals your MetaTrader while your PC is on; when the PC
   is off, it falls back to the Yahoo feed automatically (labelled).

## Notes (honest)
- **Gold price in the cloud** = your broker when the pusher runs, else Yahoo
  (COMEX futures, ~$40 above spot). Everything else is identical.
- **Free tier has an ephemeral disk:** the scorecard / history reset on each
  redeploy or platform restart. They rebuild live from the feeds. For a
  permanent track record, add a small persistent disk (paid) later.

## Run locally (unchanged)
`python web_server.py`  → http://localhost:8008/   (or use start_server.bat
for the full local server incl. the EA's /predict model).
