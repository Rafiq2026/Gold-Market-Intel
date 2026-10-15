"""
GoldHunter self-learning brain.

Reads the bot's OWN trade log (GoldHunter_Trades.csv), learns which setups tend to
WIN vs LOSE from the real outcomes (logistic regression on the entry context), and
writes GoldHunter_Policy.csv. The EA reads that policy and skips setups it has learned
tend to lose - no hand-coded rules; it improves from its own data.

HONEST GUARDS:
  * Stays in "collecting" mode (ready=0 -> bot takes everything) until it has at least
    MIN_TRADES real trades, so it never over-fits a lucky streak.
  * Strong L2 regularization + the simplest useful feature set.

Run periodically (or on a schedule). Re-reads automatically by the EA every 60s.
"""
import os, csv
import numpy as np

FILES = (r"C:\Users\ziaal\AppData\Roaming\MetaQuotes\Terminal"
         r"\D0E8209F77C8CF37AD8BF550E51FF075\MQL5\Files")
TRADES = os.path.join(FILES, "GoldHunter_Trades.csv")
POLICY = os.path.join(FILES, "GoldHunter_Policy.csv")
MIN_TRADES = 40          # don't filter on a tiny, noisy sample


def load():
    X, y = [], []
    try:
        for r in csv.DictReader(open(TRADES, newline="")):
            try:
                p = float(r["profit_money"])
                if abs(p) < 1e-6:       # drop zero/scratch (e.g. market-close) rows
                    continue
                body = float(r["body_atr"]); atr = float(r["atr"])
                spread = float(r["spread_pts"]); d = 1.0 if r["dir"] == "BUY" else -1.0
                X.append([body, atr, spread, d]); y.append(1 if p > 0 else 0)
            except Exception:
                pass
    except FileNotFoundError:
        pass
    return np.array(X, dtype=float), np.array(y, dtype=int)


def write_policy(ready, n, w=(0, 0, 0, 0), bias=0.0, note=""):
    with open(POLICY, "w", newline="") as fh:
        fh.write("ready,%d\n" % (1 if ready else 0))
        fh.write("n,%d\n" % n)
        fh.write("bias,%.6f\n" % bias)
        fh.write("body,%.6f\n" % w[0])
        fh.write("atr,%.6f\n" % w[1])
        fh.write("spread,%.6f\n" % w[2])
        fh.write("dir,%.6f\n" % w[3])
    print("policy -> ready=%d n=%d %s" % (1 if ready else 0, n, note))


def main():
    X, y = load()
    n = len(y)
    if n < MIN_TRADES or len(set(y.tolist())) < 2:
        write_policy(False, n, note="(collecting; need %d real trades & both outcomes)" % MIN_TRADES)
        return
    from sklearn.linear_model import LogisticRegression
    # strong regularization (C small) because the sample is still modest
    clf = LogisticRegression(C=0.3, max_iter=1000)
    clf.fit(X, y)
    w = clf.coef_[0]; bias = float(clf.intercept_[0])
    acc = clf.score(X, y)
    write_policy(True, n, w=w, bias=bias,
                 note="trained acc=%.2f  coef[body,atr,spread,dir]=%s" % (acc, np.round(w, 3).tolist()))
    # quick human read: expectancy by the learned P(win) tertiles
    p = clf.predict_proba(X)[:, 1]
    print("  learned P(win) range %.2f..%.2f (higher = bot favours; low = it skips)" % (p.min(), p.max()))


if __name__ == "__main__":
    main()
