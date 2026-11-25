"""Prognosemodell und Aufstellungs-Optimierer für das Sorare-Set (Fußball).

Idee: Für jedes Spieler-Spiel wird eine Punkteverteilung simuliert.
  1. Spielt er überhaupt? -> Wahrscheinlichkeit aus der Sorare-Prognose-Note (A-F) und Position,
     geschätzt aus vergangenen Spielen.
  2. Wenn ja: Punkte = Sorare-Prognose x Verhältnis-Faktor. Der Faktor wird aus der Verteilung
     "echte Punkte / Prognose" vergangener Spiele gezogen (je Position und Note).
  3. Spieler desselben Teams im selben Spiel hängen zusammen (gemeinsamer Teamfaktor, Gauß-Copula).
Alle Spieler werden in denselben Szenarien simuliert, damit Aufstellungen fair verglichen werden.
"""
import datetime as dt
import glob
import json
import os

import numpy as np
from scipy.stats import norm

BASE = os.path.dirname(__file__)
POSITIONS = ["Goalkeeper", "Defender", "Midfielder", "Forward"]
GRADE_GROUPS = {"A": "AB", "B": "AB", "C": "C", "D": "D", "E": "EF", "F": "EF", None: "none"}
CAPTAIN_BONUS = 0.5
MAX_SCORE = 100.0


# ---------------------------------------------------------------- Daten
def parse_date(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_rows():
    """Alle gespeicherten Spiele -> eine Zeile pro Spieler und Spiel."""
    rows = []
    for path in glob.glob(os.path.join(BASE, "data", "games", "*.json")):
        with open(path) as f:
            g = json.load(f)
        if not g or g.get("statusTyped") != "played":
            continue
        gdate = parse_date(g["date"])
        for p in g["playerGameScores"]:
            st = p.get("anyPlayerGameStats") or {}
            proj = p.get("projection") or {}
            mins = st.get("minsPlayed") or 0
            rows.append({
                "player": p["anyPlayer"]["slug"],
                "team": (st.get("anyTeam") or {}).get("slug"),
                "game": g["id"],
                "date": gdate,
                "day": gdate.astimezone(dt.timezone(dt.timedelta(hours=1))).date(),
                "competition": g["competition"]["slug"],
                "pos": p["positionTyped"],
                "score": float(p["score"] or 0.0),
                "status": p["scoreStatus"],
                "proj": float(p["projectedScore"] or 0.0),
                "grade": proj.get("grade"),
                "played": mins > 0 or (p["score"] or 0) > 0,
                "started": bool(st.get("gameStarted")),
                "mins": mins,
            })
    rows.sort(key=lambda r: r["date"])
    add_form(rows)
    return rows


def add_form(rows):
    """Form vor dem Spiel (ohne Wissen über die Startelf): wie oft hat der Spieler in den letzten
    5 Spielen seines Teams gespielt (k5) bzw. von Beginn an gespielt (st5)?"""
    last = {}
    for r in rows:
        prev = last.get(r["player"], [])
        r["n5"] = len(prev)
        r["k5"] = sum(p["played"] for p in prev)
        r["st5"] = sum(p["started"] for p in prev)
        last[r["player"]] = (prev + [r])[-5:]


def form_group(r):
    if r.get("n5", 0) < 3:
        return "neu"
    if r["st5"] >= 4:
        return "stamm"      # Stammspieler
    if r["k5"] >= 3:
        return "rotation"
    if r["k5"] >= 1:
        return "selten"
    return "nie"


def load_my_cards(path=os.path.join(BASE, "data", "cards_all.json"), set_slug="divination"):
    """Beste Karte je Spieler (höchster Editionsbonus) aus Fynns Galerie für das aktive Set."""
    with open(path) as f:
        cards = json.load(f)
    best = {}
    for c in cards:
        if (c.get("cardSet") or {}).get("slug") != set_slug:
            continue
        slug = c["anyPlayer"]["slug"]
        bonus = ((c.get("skinEdition") or {}).get("bonus") or 1.0) - 1.0
        cur = best.get(slug)
        if cur is None or bonus > cur["bonus"]:
            best[slug] = {"player": slug, "name": c["anyPlayer"]["displayName"], "card": c["slug"],
                          "pos": c["anyPositions"][0], "bonus": round(bonus, 4),
                          "edition": (c.get("skinEdition") or {}).get("skinName") or "Standard"}
    return best


# ---------------------------------------------------------------- Modell
class SetModel:
    """mode = 'pooled'  : Verhältnis-Verteilung je Position und Note (alle Spieler gemeinsam)
       mode = 'player'  : eigene letzte Spiele des Spielers, auf die Prognose skaliert (alte Methode)
       mode = 'sorare'  : Vergleich: nur Prognose + Streuung je Position, Note wird ignoriert"""

    def __init__(self, mode="pooled", rho=0.3, avail="grade"):
        self.mode = mode
        self.rho = rho
        self.avail = avail  # 'grade' = Note kurz vor Anpfiff, 'form' = nur Form (vor der Startelf)

    def adjust(self, it, vals):
        """Kalibrierung: Mittelwert- und Streuungskorrektur der simulierten Punkte (Standard: keine)."""
        cal = getattr(self, "calib", None)
        if not cal:
            return vals
        m = cal.get("mean", 1.0)
        s = cal.get("spread", 1.0)
        center = it["proj"] * cal.get("center", 1.0)
        return (center + (vals - center) * s) * m

    def group(self, r):
        if self.avail == "form":
            return form_group(r)
        return GRADE_GROUPS.get(r.get("grade"), "none")

    def ratio_dist(self, pos, gg, proj):
        """Verteilung 'echte Punkte / Prognose' von Spielern mit ähnlicher Prognose (Fenster um proj),
        damit hohe und niedrige Prognosen nicht vermischt werden."""
        key = (pos, gg, int(round(proj)))
        if key in self._rcache:
            return self._rcache[key]
        res = None
        for k in ((pos, gg), (pos, "_alle")):
            pj, rt = self.ratios.get(k, (np.array([]), np.array([])))
            if len(pj) < 100:
                continue
            for w in (4, 6, 8, 12, 16, 25, 40):
                m = np.abs(pj - proj) <= w
                if m.sum() >= 200:
                    res = np.sort(rt[m])
                    break
            if res is not None:
                break
        if res is None:
            pj, rt = self.ratios.get((pos, "_alle"), (np.array([1.0]), np.array([1.0])))
            res = np.sort(rt) if len(rt) else np.array([1.0])
        self._rcache[key] = res
        return res

    def fit(self, train_rows):
        self.p_play = {}
        self.ratios = {}
        overall_play = np.mean([r["played"] for r in train_rows]) if train_rows else 0.7
        for pos in POSITIONS:
            pr = [r for r in train_rows if r["pos"] == pos]
            groups = {"neu", "stamm", "rotation", "selten", "nie"} if self.avail == "form" else set(GRADE_GROUPS.values())
            for gg in groups:
                sel = [r for r in pr if self.group(r) == gg]
                if self.mode == "sorare":
                    sel = pr
                n = len(sel)
                k = sum(r["played"] for r in sel)
                self.p_play[(pos, gg)] = (k + 5 * overall_play) / (n + 5) if n else overall_play
                pts = sorted((r["proj"], r["score"] / r["proj"]) for r in sel if r["played"] and r["proj"] >= 5)
                self.ratios[(pos, gg)] = (np.array([a for a, _ in pts]), np.array([b for _, b in pts]))
            pts = sorted((r["proj"], r["score"] / r["proj"]) for r in pr if r["played"] and r["proj"] >= 5)
            self.ratios[(pos, "_alle")] = (np.array([a for a, _ in pts]), np.array([b for _, b in pts]))
        self._rcache = {}
        # eigene Historie je Spieler (für mode 'player')
        self.history = {}
        for r in train_rows:
            if r["played"]:
                self.history.setdefault(r["player"], []).append(r["score"])
        return self

    def simulate(self, items, n_scen=20000, seed=0):
        """items: Liste von dicts mit player, team, game, pos, proj, grade.
        Rückgabe: Matrix roher Sorare-Punkte (n_scen x len(items))."""
        rng = np.random.default_rng(seed)
        n = len(items)
        keys = sorted({(it["game"], it["team"]) for it in items})
        kidx = {k: i for i, k in enumerate(keys)}
        z = rng.standard_normal((n_scen, len(keys)))
        raw = np.zeros((n_scen, n))
        for j, it in enumerate(items):
            gg = self.group(it)
            pp = self.p_play.get((it["pos"], gg), 0.7)
            eps = rng.standard_normal(n_scen)
            u = norm.cdf(self.rho * z[:, kidx[(it["game"], it["team"])]] + np.sqrt(1 - self.rho ** 2) * eps)
            plays = rng.random(n_scen) < pp
            hist = self.history.get(it["player"], [])
            if self.mode == "player" and len(hist) >= 8 and np.mean(hist[-30:]) > 0:
                h = np.sort(np.array(hist[-30:]))
                factor = np.clip(it["proj"] / np.mean(h), 0.6, 1.4) if it["proj"] > 0 else 1.0
                vals = np.quantile(h, u) * factor
            else:
                rat = self.ratio_dist(it["pos"], gg, it["proj"])
                vals = self.adjust(it, it["proj"] * np.quantile(rat, u))
            raw[:, j] = np.where(plays, np.clip(vals, 0, MAX_SCORE), 0.0)
        return raw


# ---------------------------------------------------------------- Optimierer
SLOTS = [("GK", {"Goalkeeper"}), ("DEF", {"Defender"}), ("MID", {"Midfielder"}),
         ("FWD", {"Forward"}), ("EXTRA", {"Defender", "Midfielder", "Forward"})]


def lineup_prob(raw, bonus, idx, captain, target):
    tot = sum(raw[:, i] * (1 + bonus[i]) for i in idx) + CAPTAIN_BONUS * raw[:, captain]
    return float(np.mean(tot >= target)), tot


def optimize(raw, bonus, pos, target, exclude=frozenset(), pool_size=30, rounds=8):
    """Sucht die Aufstellung mit der höchsten Chance, das Ziel zu schaffen (lokale Suche auf Szenarien).
    raw: Szenarien (n_scen x n); bonus: Editionsbonus je Spieler; pos: Position je Spieler."""
    n = raw.shape[1]
    counted = raw * (1 + bonus)[None, :]
    cap = CAPTAIN_BONUS * raw
    mean_c = counted.mean(0)
    q85 = np.quantile(counted, 0.85, axis=0)
    pools = []
    for _, allowed in SLOTS:
        el = [i for i in range(n) if pos[i] in allowed and i not in exclude]
        if not el:
            return None
        top = sorted(el, key=lambda i: -mean_c[i])[:pool_size]
        top += [i for i in sorted(el, key=lambda i: -q85[i])[:pool_size // 2] if i not in top]
        pools.append(top)

    def start(metric):
        chosen = []
        for s in range(5):
            cands = [i for i in pools[s] if i not in chosen]
            if not cands:
                return None
            chosen.append(max(cands, key=lambda i: metric[i]))
        return chosen

    best = (None, -1.0, None)
    for metric in (mean_c, q85):
        lu = start(metric)
        if lu is None:
            continue
        capt = max(lu, key=lambda i: mean_c[i])
        p, _ = lineup_prob(raw, bonus, lu, capt, target)
        for _ in range(rounds):
            improved = False
            for s in range(5):
                others = [lu[k] for k in range(5) if k != s]
                other_sum = counted[:, others].sum(1)
                cands = np.array([i for i in pools[s] if i not in others])
                if len(cands) == 0:
                    continue
                base = other_sum[:, None] + counted[:, cands]
                # Kapitän = neuer Kandidat
                best_p = np.mean(base + cap[:, cands] >= target, axis=0)
                best_cap = cands.copy()
                for o in others:  # Kapitän = einer der anderen
                    pr = np.mean(base + cap[:, [o]] >= target, axis=0)
                    better = pr > best_p
                    best_p = np.where(better, pr, best_p)
                    best_cap = np.where(better, o, best_cap)
                k = int(np.argmax(best_p))
                if best_p[k] > p + 1e-9:
                    lu[s] = int(cands[k])
                    capt = int(best_cap[k])
                    p = float(best_p[k])
                    improved = True
            if not improved:
                break
        if p > best[1]:
            best = (list(lu), p, capt)
    if best[0] is None:
        return None
    return {"idx": best[0], "captain": best[2], "p_opt": best[1]}


def top_lineups(raw, bonus, pos, target, k=3, blocked=frozenset()):
    """k beste Aufstellungen ohne gemeinsame Spieler."""
    used = set(blocked)
    res = []
    for _ in range(k):
        lu = optimize(raw, bonus, pos, target, exclude=frozenset(used))
        if lu is None:
            break
        res.append(lu)
        used |= set(lu["idx"])
    return res


def sorare_max_lineup(proj, bonus, pos, exclude=frozenset()):
    """Vergleich: Aufstellung mit den meisten prognostizierten Punkten laut Sorare."""
    n = len(proj)
    val = np.array(proj) * (1 + np.array(bonus))
    chosen = []
    for _, allowed in SLOTS:
        el = [i for i in range(n) if pos[i] in allowed and i not in chosen and i not in exclude]
        if not el:
            return None
        chosen.append(max(el, key=lambda i: val[i]))
    capt = max(chosen, key=lambda i: proj[i])
    return {"idx": chosen, "captain": capt}
