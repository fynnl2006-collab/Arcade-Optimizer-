"""Perfect-Run-Planer: Mit welchen Schritten schafft man Stufe G bis Tag D am wahrscheinlichsten?

Vorgehen:
1. Schritte bilden: Ein Schritt umfasst alle Spiele mit Anstoß von a bis b. Nach einem gewonnenen Schritt ist
   die nächste Stufe frühestens `gap` (2 h 05 min, Regel laut Fynn) nach dem letzten Anstoß b stellbar.
   Von mehreren Enden b mit gleichem frühesten Folge-Start (und gleichem Tag) bleibt nur das späteste – es
   enthält mehr Spiele und verbaut nichts.
2. Schritte bewerten: Für jeden Schritt und jedes Stufenziel rechnet der normale Optimierer (live.suggest) die
   besten 1–4 Aufstellungen und die Chance, dass mindestens eine das Ziel schafft. Das läuft parallel auf allen
   Prozessorkernen (Fork: die großen Szenario-Tabellen werden nicht kopiert).
3. Plan finden: Rückwärtsrechnung (dynamische Programmierung) über Zustände (Zeitpunkt, Stufe, Leben). In jedem
   Zustand: warten oder einen Schritt mit m Leben spielen. Gewonnen -> nächste Stufe mit 4 Leben, verloren ->
   gleiche Stufe mit den übrigen Leben, alle Leben weg -> zurück auf Stufe 1.

Annahmen: Schritte sind unabhängig (verschiedene Spiele), laufende Aufstellungen sind nicht eingerechnet,
fragliche Spieler werden nicht eingeplant.
"""
import bisect
import datetime as dt
import multiprocessing as mp
import os
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

MAX_SPAN = dt.timedelta(hours=50)  # ein Schritt umfasst höchstens gut zwei Tage
MAX_LIVES = 4
_CTX = None  # Eingaben für die Rechen-Prozesse (per Fork geerbt)


def _job(args):
    """Beste 1–4 Aufstellungen für einen Schritt und ein Stufenziel."""
    li, T = args
    c = _CTX
    usable = c["leg_keys"][li]
    free = {k: (v if k in usable else 0) for k, v in c["card_free"].items()}
    sg = c["suggest"](c["raw_opt"], c["raw_eval"], c["st_opt"], c["st_eval"], c["bonus"], c["items"], T, free,
                      c["n_opt"], c["n_eval"], with_alts=False)
    lus = sg["lineups"]
    pa = [l["p_any_so_far"] for l in lus][:MAX_LIVES]
    pa += [pa[-1] if pa else 0.0] * (MAX_LIVES - len(pa))  # mehr Leben als mögliche Aufstellungen bringen nichts
    return li, T, pa, [{"players": l["players"], "p": l["p"]} for l in lus]


def plan(ctx):
    global _CTX
    t_start = time.time()
    items, it_dt, gap, berlin = ctx["items"], ctx["it_dt"], ctx["gap"], ctx["berlin"]
    targets, t0 = ctx["targets"], ctx["start_time"]
    s0, l0 = ctx["start_stage"], ctx["start_lives"]
    n_st = len(targets)
    elig = [j for j, it in enumerate(items) if it_dt[j] >= t0 and not it.get("out") and not it.get("doubtful")]
    K = sorted({it_dt[j] for j in elig})
    if not K or not (1 <= s0 <= n_st) or l0 < 1:
        return None

    def nxt(b):
        k = bisect.bisect_left(K, b + gap)
        return K[k] if k < len(K) else None

    # 1. Schritte
    starts = sorted({K[0]} | {n for n in map(nxt, K) if n is not None})
    legs = []
    for a in starts:
        best = {}
        for b in K:
            if a <= b <= a + MAX_SPAN:
                key = (nxt(b), b.astimezone(berlin).date())
                if key not in best or b > best[key]:
                    best[key] = b
        legs += [(a, b) for b in sorted(best.values())]
    ctx["leg_keys"] = [{(items[j]["player"], items[j]["game"]) for j in elig if a <= it_dt[j] <= b} for a, b in legs]

    # 2. Schritte bewerten (parallel)
    _CTX = ctx
    jobs = [(li, T) for li in range(len(legs)) for T in sorted(set(targets))]
    res = {}
    workers = min(os.cpu_count() or 1, 8)
    try:
        if workers > 1:
            with ProcessPoolExecutor(workers, mp_context=mp.get_context("fork")) as ex:
                for li, T, pa, lus in ex.map(_job, jobs, chunksize=4):
                    res[(li, T)] = (pa, lus)
        else:
            for jb in jobs:
                li, T, pa, lus = _job(jb)
                res[(li, T)] = (pa, lus)
    finally:
        _CTX = None
    t_legs = time.time() - t_start

    # 3. Rückwärtsrechnung je Ziel-Stufe G und Tag D
    nS = len(starts)
    sidx = {a: i for i, a in enumerate(starts)}
    by_start = defaultdict(list)
    for li, (a, b) in enumerate(legs):
        n = nxt(b)
        by_start[sidx[a]].append((li, b, sidx[n] if n is not None else nS))
    plans, used = {}, {}

    for G in range(s0, n_st + 1):
        for D in ctx["days"]:
            dend = dt.datetime(D.year, D.month, D.day, 23, 59, 59, tzinfo=berlin)
            V = [[[0.0] * (MAX_LIVES + 1) for _ in range(G + 1)] for _ in range(nS + 1)]
            act = {}
            for i in range(nS - 1, -1, -1):
                for s in range(1, G + 1):
                    for lv in range(1, MAX_LIVES + 1):
                        best_v, best_a = V[i + 1][s][lv], None  # warten
                        for li, b, ni in by_start[i]:
                            if b > dend:
                                continue
                            pa = res[(li, targets[s - 1])][0]
                            win = 1.0 if s == G else V[ni][s + 1][MAX_LIVES]
                            for m in range(1, lv + 1):
                                lose = V[ni][s][lv - m] if lv - m > 0 else V[ni][1][MAX_LIVES]
                                v = pa[m - 1] * win + (1 - pa[m - 1]) * lose
                                if v > best_v + 1e-9:
                                    best_v, best_a = v, (li, m, ni)
                        V[i][s][lv] = best_v
                        act[(i, s, lv)] = best_a

            def next_action(i, s, lv):
                while i < nS:
                    a_ = act.get((i, s, lv))
                    if a_:
                        return a_
                    i += 1
                return None

            # Hauptlinie: immer den Gewinn-Fall weiterverfolgen
            steps, i, s, lv = [], 0, s0, l0
            while s <= G:
                a_ = next_action(i, s, lv)
                if not a_:
                    break
                li, m, ni = a_
                q = res[(li, targets[s - 1])][0][m - 1]
                if lv - m > 0:
                    r = next_action(ni, s, lv - m)
                    retry = {"leg": r[0], "m": r[1]} if r else None
                    if r:
                        used.setdefault((r[0], -1), 0)  # nur Zeitpunkt nötig
                else:
                    retry = {"reset": True}
                steps.append({"leg": li, "s": s, "m": m, "q": round(q, 4), "retry": retry})
                used[(li, s)] = max(used.get((li, s), 0), m)
                i, s, lv = ni, s + 1, MAX_LIVES
            plans[f"{G}_{D}"] = {"p": round(V[0][s0][l0], 4), "steps": steps if V[0][s0][l0] > 0 else []}

    leg_ids = {li for li, _ in used}
    out_legs = {}
    for li in leg_ids:
        a, b = legs[li]
        n = nxt(b)
        out_legs[str(li)] = {"a": a.isoformat(), "b": b.isoformat(), "next": n.isoformat() if n else None}
    out_lu = {}
    for (li, s), m in used.items():
        if s < 1:
            continue
        pa, lus = res[(li, targets[s - 1])]
        out_lu[f"{li}_{s}"] = {"pa": pa, "l": lus[:m]}
    return {"start": {"stage": s0, "lives": l0, "from": t0.isoformat()}, "targets": targets,
            "legs": out_legs, "lu": out_lu, "plans": plans,
            "stats": {"legs": len(legs), "jobs": len(jobs), "workers": workers,
                      "secs_legs": round(t_legs, 1), "secs_total": round(time.time() - t_start, 1)}}
