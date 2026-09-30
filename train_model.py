"""
Gold Brain AI PRO - model trainer.

Trains a scikit-learn classifier that predicts P(next move is up) from the
engine feature vector, and saves it as model.pkl for ai_server.py to load.

Data source (in order of preference):
  1. --csv PATH : a CSV whose columns are the FEATURES plus a 'label' column
                  (label = 1 if the forward return was positive, else 0).
                  The natural source is the EA's own journal enriched with the
                  realised outcome of each signal.
  2. synthetic  : if no CSV is given, a labelled synthetic dataset is generated
                  from the heuristic + noise so the pipeline is runnable today.

Requires: scikit-learn, numpy, joblib  (pip install -r requirements.txt)
Usage:
  python train_model.py                 # synthetic data -> model.pkl
  python train_model.py --csv data.csv  # real data
"""

from __future__ import annotations
import argparse
import os
import random

import model as M


def _load_csv(path: str):
    import csv
    X, y = [], []
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            X.append([float(row[k]) for k in M.FEATURES])
            y.append(int(float(row["label"])))
    return X, y


def _synthetic(n: int = 6000, seed: int = 7):
    """Generate labelled samples from the heuristic + realistic noise."""
    rng = random.Random(seed)
    X, y = [], []
    for _ in range(n):
        f = {
            "trend_bull": rng.random(), "trend_bear": rng.random(),
            "adx": rng.uniform(10, 45),
            "momentum": rng.uniform(98.5, 101.5),
            "mom_slope": rng.uniform(-1, 1),
            "rsi": rng.uniform(20, 80),
            "macd_hist": rng.uniform(-1, 1),
            "bb_pctb": rng.random(),
            "vol_ratio": rng.uniform(0.5, 2.2),
            "atr_ratio": rng.uniform(0.5, 2.0),
            "struct_bull": rng.random(), "struct_bear": rng.random(),
            "liq_bull": rng.random(), "liq_bear": rng.random(),
            "usd": rng.choice([-1.0, 0.0, 1.0]),
            "buy_score": rng.uniform(0, 100), "sell_score": rng.uniform(0, 100),
        }
        p = M.heuristic_p_up(f)
        # Label from the latent probability with stochastic noise, so the model
        # has to learn a boundary rather than memorise the heuristic exactly.
        label = 1 if (p + rng.uniform(-0.35, 0.35)) > 0.5 else 0
        X.append(M.to_vector(f))
        y.append(label)
    return X, y


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=None, help="training CSV (FEATURES + label)")
    ap.add_argument("--out", default=M.MODEL_PATH, help="output model path")
    args = ap.parse_args()

    try:
        import numpy as np
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import accuracy_score, roc_auc_score
        import joblib
    except Exception:
        raise SystemExit(
            "scikit-learn/numpy/joblib not installed.\n"
            "  pip install -r requirements.txt")

    if args.csv and os.path.exists(args.csv):
        print(f"[train] loading {args.csv}")
        X, y = _load_csv(args.csv)
    else:
        print("[train] no CSV -> generating synthetic dataset")
        X, y = _synthetic()

    X = np.array(X, dtype=float)
    y = np.array(y, dtype=int)
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.25, random_state=1)

    clf = GradientBoostingClassifier(n_estimators=180, max_depth=3,
                                     learning_rate=0.06, subsample=0.9)
    clf.fit(Xtr, ytr)

    proba = clf.predict_proba(Xte)[:, 1]
    pred = (proba >= 0.5).astype(int)
    print(f"[train] samples={len(y)}  acc={accuracy_score(yte, pred):.3f}  "
          f"auc={roc_auc_score(yte, proba):.3f}")

    joblib.dump(clf, args.out)
    print(f"[train] saved -> {args.out}")
    print("[train] restart ai_server.py to pick up the new model.")


if __name__ == "__main__":
    main()
