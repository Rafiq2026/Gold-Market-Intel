# Gold Brain AI PRO — Python AI Confidence Model

An external model service the MQL5 EA calls over HTTP (`WebRequest`) to obtain a
probability-of-up-move that is folded into the `SignalEngine` as one more weighted
input (`InpWeightAI`).

The bridge is **fail-safe**: if this service is down — or the EA is running in the
Strategy Tester where `WebRequest` is disabled — the EA falls back to a built-in
logistic heuristic that mirrors `heuristic_p_up()` here, so it keeps trading.

## Files

| File | Purpose |
|------|---------|
| `model.py` | Feature contract (`FEATURES`), heuristic, and model load/predict. Zero deps. |
| `ai_server.py` | Stdlib HTTP server. `POST /predict`, `GET /health`. |
| `train_model.py` | Trains a scikit-learn model → `model.pkl` (real CSV or synthetic). |
| `requirements.txt` | Only needed for training / model-backed serving. |

## Quick start (no dependencies)

```bash
python ai_server.py            # http://127.0.0.1:8008  (heuristic mode)
```

Then in MetaTrader 5: **Tools → Options → Expert Advisors →
“Allow WebRequest for listed URL”** → add `http://127.0.0.1:8008`.
Set `InpUseAI = true` on the EA (default).

## Upgrade to a trained ML model (optional)

```bash
pip install -r requirements.txt
python train_model.py                 # synthetic data → model.pkl
#   or, with your own labelled data:
python train_model.py --csv mydata.csv
python ai_server.py                   # now serves source="model"
```

Verify:
```bash
curl http://127.0.0.1:8008/health
# {"ok": true, "model_loaded": true, "source": "model"}
```

## API

`POST /predict` — body is a JSON object of the 17 features in `model.py:FEATURES`:

```json
{"trend_bull":0.9,"trend_bear":0.0,"adx":32,"momentum":100.8,"mom_slope":0.4,
 "rsi":58,"macd_hist":0.3,"bb_pctb":0.6,"vol_ratio":1.4,"atr_ratio":1.1,
 "struct_bull":0.8,"struct_bear":0.0,"liq_bull":0.5,"liq_bear":0.0,"usd":-1,
 "buy_score":78,"sell_score":22}
```

Response (consumed by `CAIEngine` — it reads `p_up`):

```json
{"ok":true,"p_up":0.9942,"bull":0.9883,"bear":0.0,"confidence":98.83,"source":"model"}
```

- `p_up` — probability the next move is up (0..1).
- `bull` / `bear` — conviction `|p-0.5|*2` on the winning side (the EA's 0..1 contract).
- `usd` feature — dollar **strength** (`+1` strong, `-1` weak, `0` neutral); the model
  treats strong dollar as bearish for gold.

## Live news feed (economic + political)

`news_feed.py` runs in a background thread inside the server and keeps a cached
snapshot served instantly via `/news` and folded into `/predict`:

- **Economic (scheduled):** Forex Factory weekly calendar (no key) → hard block
  around high-impact events (`news_blocked`).
- **Political / geopolitical (breaking):** Google News + CNBC RSS (no key) →
  headlines scored by a **sentiment model** (`sentiment.py`, VADER; falls back to
  a finance lexicon) into `geo_risk` (0..1) and `geo_bias` (−1..+1 safe-haven
  direction for gold). Risk-off headlines (war/sanctions/crisis) lift gold.

`geo_bias` nudges the model probability by `NEWS_BLEND` (default 0.12).

### Optional professional news API (bring your own key)

The plumbing is already wired — set environment variables before starting the
server and it switches provider automatically (else it uses free RSS):

```bat
set GBAI_NEWS_PROVIDER=marketaux   REM  marketaux | newsapi | finnhub
set GBAI_NEWSAPI_KEY=your_key_here
python ai_server.py
```

`GET /news` shows `"provider"` and `"sentiment_backend"` so you can confirm which
sources are live. I do not enter API keys for you — add your own key above.

## Training on REAL outcomes

The natural training set is the EA's own journal (`GoldBrainAI_Journal.csv` in the
terminal `MQL5/Files` folder) enriched with the realised result of each signal:
build a CSV whose columns are exactly `model.py:FEATURES` plus a `label` column
(`1` if the forward return over your chosen horizon was positive, else `0`), then
`python train_model.py --csv that.csv`. Restart the server to load the new model.
