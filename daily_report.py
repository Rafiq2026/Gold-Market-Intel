"""
Gold Brain AI PRO - Daily Excel trade report.

Reads the EA's closed-trade ledger (GoldBrainAI_Training.csv, written by the
MQL5 TradeRecorder) and produces a formatted .xlsx with:
  * Summary sheet - per-day stats (P&L, win rate, profit factor, expectancy,
    breakdown by direction / regime / session)
  * Trades sheet  - one row per closed trade, colour-coded win/loss

Why this design: the MetaTrader5 Python bridge is blocked by Windows
Application Control on this PC, so the EA (which is connected to the account)
exports the data and this tool turns it into Excel - no MT5 DLL needed.

Usage:
  python daily_report.py                       # today, auto-locates the CSV
  python daily_report.py --date 2026.08.25
  python daily_report.py --all                 # whole history
  python daily_report.py --csv <path> --out <path.xlsx>
"""

from __future__ import annotations
import argparse
import csv
import os
from datetime import datetime, timezone

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

REGIME = {0:"RANGE",1:"WEAK TREND",2:"STRONG TREND",3:"BREAKOUT",4:"FAKE BREAKOUT",
          5:"HIGH VOL",6:"LOW VOL",7:"NEWS VOL",8:"LIQUIDITY HUNT"}

# Default terminal Files path (where the EA writes).
DEFAULT_CSV = (r"C:\Users\ziaal\AppData\Roaming\MetaQuotes\Terminal"
               r"\D0E8209F77C8CF37AD8BF550E51FF075\MQL5\Files\GoldBrainAI_Training.csv")

HDR = PatternFill("solid", fgColor="1F2A44")
HDRF = Font(bold=True, color="FFFFFF")
WIN = PatternFill("solid", fgColor="C6EFCE")
LOSS = PatternFill("solid", fgColor="FFC7CE")
TITLE = Font(bold=True, size=14, color="1F2A44")
KEY = Font(bold=True)
THIN = Border(*[Side(style="thin", color="D9D9D9")]*4)
CENTER = Alignment(horizontal="center")


def _f(v, d=0.0):
    try: return float(v)
    except: return d


def load(csv_path, date_filter, take_all):
    rows = []
    if not os.path.exists(csv_path):
        return rows
    with open(csv_path, newline="") as fh:
        for r in csv.DictReader(fh):
            if not r.get("open_time"):
                continue
            day = r["open_time"].split(" ")[0]           # "2026.08.25"
            if not take_all and date_filter and day != date_filter:
                continue
            rows.append(r)
    return rows


def build(rows, out_path, day_label):
    wb = openpyxl.Workbook()

    # ---------- Summary ----------
    s = wb.active
    s.title = "Summary"
    s["A1"] = "Gold Brain AI PRO - Daily Report"; s["A1"].font = TITLE
    s["A2"] = f"XAUUSD   {day_label}"; s["A2"].font = KEY

    n = len(rows)
    profits = [_f(r["profit"]) for r in rows]
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p <= 0]
    gross_w = sum(wins); gross_l = sum(losses); net = sum(profits)
    pf = (gross_w / abs(gross_l)) if gross_l < 0 else (gross_w if gross_w>0 else 0)
    avg_r = (sum(_f(r["r_multiple"]) for r in rows)/n) if n else 0
    winrate = (100.0*len(wins)/n) if n else 0
    expect = (net/n) if n else 0
    buys = [r for r in rows if _f(r["dir"])>0]; sells = [r for r in rows if _f(r["dir"])<0]

    stats = [
        ("Total trades", n),
        ("Wins / Losses", f"{len(wins)} / {len(losses)}"),
        ("Win rate", f"{winrate:.1f}%"),
        ("Net P/L", round(net,2)),
        ("Gross profit", round(gross_w,2)),
        ("Gross loss", round(gross_l,2)),
        ("Profit factor", round(pf,2)),
        ("Expectancy / trade", round(expect,2)),
        ("Average R", round(avg_r,2)),
        ("Best trade", round(max(profits),2) if profits else 0),
        ("Worst trade", round(min(profits),2) if profits else 0),
        ("BUY trades / P&L", f"{len(buys)} / {round(sum(_f(r['profit']) for r in buys),2)}"),
        ("SELL trades / P&L", f"{len(sells)} / {round(sum(_f(r['profit']) for r in sells),2)}"),
    ]
    row = 4
    for k, v in stats:
        s.cell(row, 1, k).font = KEY
        c = s.cell(row, 2, v)
        if k in ("Net P/L","Best trade","Worst trade") and isinstance(v,(int,float)):
            c.font = Font(bold=True, color=("1B7A34" if v>=0 else "B21F2D"))
        row += 1

    def breakdown(title, keyfn, start_row):
        s.cell(start_row, 4, title).font = KEY
        agg = {}
        for r in rows:
            k = keyfn(r); a = agg.setdefault(k, [0,0.0])
            a[0]+=1; a[1]+=_f(r["profit"])
        s.cell(start_row+1,4,"Group").font=HDRF; s.cell(start_row+1,4).fill=HDR
        s.cell(start_row+1,5,"Trades").font=HDRF; s.cell(start_row+1,5).fill=HDR
        s.cell(start_row+1,6,"P&L").font=HDRF; s.cell(start_row+1,6).fill=HDR
        rr = start_row+2
        for k,(cnt,pl) in sorted(agg.items()):
            s.cell(rr,4,k); s.cell(rr,5,cnt); s.cell(rr,6,round(pl,2)); rr+=1
        return rr+1

    nr = breakdown("By Regime", lambda r: REGIME.get(int(_f(r["regime"])),"?"), 4)
    breakdown("By Session", lambda r: r.get("session","?"), nr)

    s.column_dimensions["A"].width = 20; s.column_dimensions["B"].width = 16
    s.column_dimensions["D"].width = 18; s.column_dimensions["E"].width = 10
    s.column_dimensions["F"].width = 12

    # ---------- Trades ----------
    t = wb.create_sheet("Trades")
    cols = ["Open time","Dir","Lots","Entry","Exit","SL","TP","Profit","R",
            "Result","Reason","Regime","Session","Conf","AI p(up)","Bars"]
    for i,c in enumerate(cols,1):
        cell=t.cell(1,i,c); cell.font=HDRF; cell.fill=HDR; cell.alignment=CENTER
    for ri,r in enumerate(rows,2):
        p=_f(r["profit"]); win=p>0
        vals=[r["open_time"], "SELL" if _f(r["dir"])<0 else "BUY", _f(r["lots"]),
              _f(r["entry"]), _f(r["exit"]), _f(r["sl"]), _f(r["tp"]), round(p,2),
              round(_f(r["r_multiple"]),2), "WIN" if win else "LOSS",
              r.get("exit_reason",""), REGIME.get(int(_f(r["regime"])),"?"),
              r.get("session",""), round(_f(r["conf"]),1), round(_f(r["ai_pup"]),2),
              int(_f(r["bars_held"]))]
        for ci,v in enumerate(vals,1):
            cell=t.cell(ri,ci,v); cell.border=THIN
            if cols[ci-1] in ("Profit","Result","R"):
                cell.fill = WIN if win else LOSS
    widths=[17,6,7,10,10,10,10,10,7,8,16,14,15,7,9,6]
    for i,w in enumerate(widths,1): t.column_dimensions[get_column_letter(i)].width=w
    t.freeze_panes="A2"

    wb.save(out_path)
    return net, n, winrate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--date", default=None, help="YYYY.MM.DD (default: today)")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    # 'today' from the CSV's own latest date if available, else system date.
    day = a.date
    if not day and not a.all:
        # Date.now is fine here (standalone script, not a workflow).
        day = datetime.now().strftime("%Y.%m.%d")

    rows = load(a.csv, day, a.all)
    label = "ALL HISTORY" if a.all else day
    out = a.out or os.path.join(os.path.dirname(a.csv) if os.path.dirname(a.csv) else ".",
                                f"GoldBrainAI_Report_{'ALL' if a.all else day}.xlsx")
    net, n, wr = build(rows, out, label)
    print(f"[report] {label}: {n} trades, net {net:.2f}, win {wr:.1f}%  -> {out}")


if __name__ == "__main__":
    main()
