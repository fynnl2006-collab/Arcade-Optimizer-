"""Kalibrierung: Mittelwert-Faktor (m) und Streuungs-Faktor (s) so wählen, dass die vorhergesagten
Chancen der Backtest-Aufstellungen zu den echten Ergebnissen passen.
Ehrlich geprüft: Faktoren nur auf der ersten Hälfte der Spieltage wählen, auf der zweiten Hälfte testen."""
import datetime as dt
import json
import sys

import numpy as np

import backtest as B
import model as M


def logloss(p, y):
    p = np.clip(np.array(p), 1e-3, 1 - 1e-3); y = np.array(y, dtype=float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def main(avail="grade"):
    rows = M.load_rows()
    my = M.load_my_cards()
    log = [l for l in json.load(open(f"data/bt_v1/backtest_lineups_{avail}.json")) if l["rang"] in (1, 2, 3)]
    days = sorted({l["tag"] for l in log})
    half = days[: len(days) // 2]
    byday = {}
    for r in rows:
        byday.setdefault(str(r["day"]), {})[r["player"]] = r
    models = {}
    for d in days:
        dd = dt.date.fromisoformat(d)
        models[d] = M.SetModel("pooled", 0.3, avail).fit([r for r in rows if r["date"] < B.day_start(dd)])
    grid_m = [1.0, 1.03, 1.06, 1.09, 1.12]
    grid_s = [0.7, 0.8, 0.9, 1.0]
    res = []
    for m in grid_m:
        for s in grid_s:
            preds = {"fit": ([], []), "test": ([], [])}
            for l in log:
                mdl = models[l["tag"]]
                mdl.calib = {"mean": m, "spread": s}
                items = [byday[l["tag"]][p] for p in l["spieler"]]
                raw = mdl.simulate(items, 8000, seed=4)
                b = np.array([my[p]["bonus"] for p in l["spieler"]])
                ci = l["spieler"].index(l["kapitaen"])
                tot = (raw * (1 + b)).sum(1) + 0.5 * raw[:, ci]
                part = "fit" if l["tag"] in half else "test"
                preds[part][0].append(float(np.mean(tot >= l["ziel"])))
                preds[part][1].append(l["geschafft"])
            row = {"m": m, "s": s}
            for part in ("fit", "test"):
                p, y = preds[part]
                row[part] = {"logloss": round(logloss(p, y), 4), "brier": round(float(np.mean((np.array(p) - np.array(y, float)) ** 2)), 4),
                             "vorh": round(float(np.mean(p)), 3), "echt": round(float(np.mean(y)), 3)}
            res.append(row)
            print(row, flush=True)
    for m in models.values():
        m.calib = None
    best = min(res, key=lambda r: r["fit"]["logloss"])
    base = next(r for r in res if r["m"] == 1.0 and r["s"] == 1.0)
    print("OHNE:", base)
    print("BESTE (auf erster Hälfte gewählt):", best)
    json.dump({"avail": avail, "grid": res, "best": best, "base": base}, open(f"data/calibration_{avail}.json", "w"), indent=1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "grade")
