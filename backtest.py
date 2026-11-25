"""Backtest des Set-Modells an allen Divination-Spieltagen seit 30.10.2026.

Teil A (Spieler): Wie gut treffen die vorhergesagten Wahrscheinlichkeiten P(Punkte >= x)?
Teil B (Aufstellungen): Für jeden Spieltag und jedes Stufenziel die 3 besten Aufstellungen aus Fynns
Karten berechnen (nur mit Daten VOR dem Tag) und mit dem echten Ergebnis vergleichen.
Vergleich: Aufstellung mit den meisten Sorare-Prognosepunkten."""
import datetime as dt
import json
import sys
from collections import defaultdict

import numpy as np

import model as M

SET_START = dt.date(2026, 10, 30)
TARGETS = [260, 300, 360, 400, 440, 480]
THRESH = [20, 40, 60, 80, 95]


def day_start(d):
    return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone(dt.timedelta(hours=1)))


def marginal_ge(mdl, r, x):
    """P(Punkte >= x) für ein Spieler-Spiel (ohne Simulation)."""
    gg = mdl.group(r)
    pp = mdl.p_play.get((r["pos"], gg), 0.7)
    hist = mdl.history.get(r["player"], [])
    if mdl.mode == "player" and len(hist) >= 8 and np.mean(hist[-30:]) > 0:
        h = np.array(hist[-30:])
        f = np.clip(r["proj"] / h.mean(), 0.6, 1.4) if r["proj"] > 0 else 1.0
        return pp * float(np.mean(h * f >= x))
    if r["proj"] <= 0:
        return 0.0
    rat = mdl.ratio_dist(r["pos"], gg, r["proj"])
    return pp * float(np.mean(np.minimum(r["proj"] * rat, M.MAX_SCORE) >= x))


def brier(pairs):
    p = np.array([a for a, _ in pairs]); y = np.array([b for _, b in pairs], dtype=float)
    return float(np.mean((p - y) ** 2)) if len(p) else float("nan")


def calib(pairs, bins=(0, .05, .1, .2, .3, .5, .7, 1.01)):
    out = []
    p = np.array([a for a, _ in pairs]); y = np.array([b for _, b in pairs], dtype=float)
    for lo, hi in zip(bins[:-1], bins[1:]):
        m = (p >= lo) & (p < hi)
        if m.sum():
            out.append({"von": lo, "bis": min(hi, 1), "n": int(m.sum()),
                        "vorhergesagt": round(float(p[m].mean()), 3), "eingetreten": round(float(y[m].mean()), 3)})
    return out


def main(rho=0.3, avail="grade", n_opt=15000, n_eval=30000, max_days=None):
    rows = M.load_rows()
    my = M.load_my_cards()
    days = sorted({r["day"] for r in rows if r["day"] >= SET_START})
    if max_days:
        days = days[:max_days]
    print(f"{len(rows)} Spieler-Spiele, {len(days)} Testtage, {len(my)} eigene Spieler", flush=True)

    player_pairs = defaultdict(list)  # (modell, schwelle) -> [(p, y)]
    lineup_log = []
    for d in days:
        train = [r for r in rows if r["date"] < day_start(d)]
        test = [r for r in rows if r["day"] == d]
        models = {m: M.SetModel(m, rho, avail).fit(train) for m in ("pooled", "player", "sorare")}
        # Teil A
        for r in test:
            for x in THRESH:
                y = r["score"] >= x
                for m, mdl in models.items():
                    player_pairs[(m, x)].append((marginal_ge(mdl, r, x), y))
        # Teil B: nur Fynns Spieler
        items = [r for r in test if r["player"] in my and r["pos"] == my[r["player"]]["pos"]]
        if len(items) < 8:
            continue
        bonus = np.array([my[r["player"]]["bonus"] for r in items])
        pos = [r["pos"] for r in items]
        actual = np.array([r["score"] for r in items])
        proj = [r["proj"] for r in items]
        mdl = models["pooled"]
        raw_opt = mdl.simulate(items, n_opt, seed=1).astype(np.float32)
        raw_eval = mdl.simulate(items, n_eval, seed=2).astype(np.float32)
        for T in TARGETS:
            lus = M.top_lineups(raw_opt, bonus, pos, T, k=3)
            any_tot = np.zeros(n_eval, dtype=bool)
            any_act = False
            for rank, lu in enumerate(lus):
                p_eval, tot = M.lineup_prob(raw_eval, bonus, lu["idx"], lu["captain"], T)
                any_tot |= tot >= T
                act = float(sum(actual[i] * (1 + bonus[i]) for i in lu["idx"]) + M.CAPTAIN_BONUS * actual[lu["captain"]])
                any_act |= act >= T
                lineup_log.append({"tag": str(d), "ziel": T, "rang": rank + 1, "p": round(p_eval, 4),
                                   "punkte": round(act, 1), "geschafft": act >= T,
                                   "spieler": [items[i]["player"] for i in lu["idx"]],
                                   "kapitaen": items[lu["captain"]]["player"]})
            if lus:
                lineup_log.append({"tag": str(d), "ziel": T, "rang": "mind. eine von 3",
                                   "p": round(float(any_tot.mean()), 4), "geschafft": bool(any_act)})
            # fairer Vergleich: nur Spieler, die laut Sorare-Note wahrscheinlich spielen (A-D)
            # bzw. im Modus 'form' nur Spieler, die zuletzt meist gespielt haben
            if avail == "grade":
                unsicher = frozenset(i for i, r in enumerate(items) if r["grade"] not in ("A", "B", "C", "D"))
            else:
                unsicher = frozenset(i for i, r in enumerate(items) if M.form_group(r) not in ("stamm", "rotation"))
            sm = M.sorare_max_lineup(proj, bonus, pos, exclude=unsicher)
            if sm:
                p_sm, _ = M.lineup_prob(raw_eval, bonus, sm["idx"], sm["captain"], T)
                act = float(sum(actual[i] * (1 + bonus[i]) for i in sm["idx"]) + M.CAPTAIN_BONUS * actual[sm["captain"]])
                lineup_log.append({"tag": str(d), "ziel": T, "rang": "sorare-max", "p": round(p_sm, 4),
                                   "punkte": round(act, 1), "geschafft": act >= T})
        print(d, "Kandidaten", len(items), flush=True)

    # Auswertung
    res = {"spieler": {}, "aufstellungen": {}}
    for (m, x), pairs in sorted(player_pairs.items()):
        res["spieler"][f"{m}_ab_{x}"] = {"brier": round(brier(pairs), 4), "n": len(pairs),
                                          "kalibrierung": calib(pairs)}
    for key in (1, 2, 3, "mind. eine von 3", "sorare-max"):
        sel = [l for l in lineup_log if l["rang"] == key]
        pairs = [(l["p"], l["geschafft"]) for l in sel]
        res["aufstellungen"][str(key)] = {
            "n": len(sel), "mittl_vorhergesagt": round(float(np.mean([p for p, _ in pairs])), 3) if pairs else None,
            "geschafft_anteil": round(float(np.mean([y for _, y in pairs])), 3) if pairs else None,
            "brier": round(brier(pairs), 4), "kalibrierung": calib(pairs)}
        per_t = {}
        for T in TARGETS:
            s2 = [l for l in sel if l["ziel"] == T]
            if s2:
                per_t[T] = {"n": len(s2), "vorhergesagt": round(float(np.mean([l["p"] for l in s2])), 3),
                            "geschafft": round(float(np.mean([l["geschafft"] for l in s2])), 3)}
        res["aufstellungen"][str(key)]["je_ziel"] = per_t
    with open(f"data/backtest_result_{avail}.json", "w") as f:
        json.dump(res, f, indent=1, default=str)
    with open(f"data/backtest_lineups_{avail}.json", "w") as f:
        json.dump(lineup_log, f, indent=1, default=str)
    print(json.dumps(res["aufstellungen"], indent=1, default=str)[:4000])


if __name__ == "__main__":
    avail = sys.argv[1] if len(sys.argv) > 1 else "grade"
    rho = float(sys.argv[2]) if len(sys.argv) > 2 else 0.3
    md = int(sys.argv[3]) if len(sys.argv) > 3 else None
    main(rho=rho, avail=avail, max_days=md)
