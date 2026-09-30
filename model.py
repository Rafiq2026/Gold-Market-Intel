"""
Gold Brain AI PRO - model core.

Shared feature contract + prediction logic used by both the HTTP server
(ai_server.py) and the trainer (train_model.py).

Design goals:
  * Zero required dependencies for the default (heuristic) path - runs on a
    stock Python install so the bridge works out of the box.
  * If a trained scikit-learn model (model.pkl) is present AND joblib/sklearn
    are installed, it is used automatically instead of the heuristic.

The heuristic here MIRRORS the MQL5 CAIEngine::Heuristic so that live (server)
and offline (EA fallback / Strategy Tester) behaviour agree.
"""

from __future__ import annotations
import math
import os

# --- Canonical feature order. MUST match MQL5 SFeatureSet / BuildJson keys. ---
FEATURES = [
    "trend_bull", "trend_bear", "adx", "momentum", "mom_slope", "rsi",
    "macd_hist", "bb_pctb", "vol_ratio", "atr_ratio", "struct_bull",
    "struct_bear", "liq_bull", "liq_bear", "usd", "buy_score", "sell_score",
]

MODEL_PATH = os.path.join(os.path.dirname(__file__), "model.pkl")

_model = None            # lazily loaded sklearn estimator (or None)
_model_tried = False


def _sigmoid(z: float) -> float:
    if z < -60:
        return 0.0
    if z > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def heuristic_p_up(f: dict) -> float:
    """Logistic blend of the engine readings -> probability of an up move.

    Kept in lock-step with CAIEngine::Heuristic in AIEngine.mqh.
    """
    dir_trend = f.get("trend_bull", 0.0) - f.get("trend_bear", 0.0)
    dir_struct = f.get("struct_bull", 0.0) - f.get("struct_bear", 0.0)
    dir_liq = f.get("liq_bull", 0.0) - f.get("liq_bear", 0.0)
    mom = _clamp((f.get("momentum", 100.0) - 100.0) / 1.5, -1.0, 1.0)
    macd = _clamp(f.get("macd_hist", 0.0), -1.0, 1.0)
    rsi = (f.get("rsi", 50.0) - 50.0) / 50.0
    usd = -f.get("usd", 0.0)                       # strong dollar is bearish gold
    score = (f.get("buy_score", 0.0) - f.get("sell_score", 0.0)) / 100.0

    z = (1.8 * dir_trend + 1.4 * dir_struct + 1.0 * dir_liq +
         0.8 * mom + 0.6 * macd + 0.5 * rsi + 0.6 * usd + 1.5 * score)
    return _sigmoid(z)


def to_vector(f: dict) -> list:
    """Feature dict -> ordered vector for the ML model."""
    return [float(f.get(k, 0.0)) for k in FEATURES]


def _load_model():
    """Attempt to load model.pkl once. Returns estimator or None."""
    global _model, _model_tried
    if _model_tried:
        return _model
    _model_tried = True
    if not os.path.exists(MODEL_PATH):
        return None
    try:
        import joblib  # type: ignore
        _model = joblib.load(MODEL_PATH)
    except Exception:
        _model = None
    return _model


def predict_p_up(f: dict) -> tuple[float, str]:
    """Return (p_up, source). Uses the trained model if available."""
    model = _load_model()
    if model is not None:
        try:
            import numpy as np  # type: ignore
            x = np.array([to_vector(f)], dtype=float)
            if hasattr(model, "predict_proba"):
                p = float(model.predict_proba(x)[0][1])
            else:                                   # regressor fallback
                p = _clamp(float(model.predict(x)[0]), 0.0, 1.0)
            return p, "model"
        except Exception:
            pass
    return heuristic_p_up(f), "heuristic"


def prediction(f: dict) -> dict:
    """Full response payload consumed by the MQL5 CAIEngine."""
    p_up, source = predict_p_up(f)
    conv = abs(p_up - 0.5) * 2.0
    return {
        "ok": True,
        "p_up": round(p_up, 4),
        "bull": round(conv if p_up > 0.5 else 0.0, 4),
        "bear": round(conv if p_up < 0.5 else 0.0, 4),
        "confidence": round(conv * 100.0, 2),
        "source": source,
    }
