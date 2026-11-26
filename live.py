"""Live-Lauf des Set-Optimierers (Fußball): holt den aktuellen Stand, rechnet und schreibt data/output/result.json.

Ablauf:
 1. Board, Stufe, gestellte Aufstellungen und kommende Spiele aus der Sorare-API holen.
 2. Prognose-Schnappschuss speichern (für spätere ehrliche Tests).
 3. Modell auf alle bisherigen Spiele anpassen: vor der Startelf -> Form-Modell, danach -> Noten-Modell.
 4. Für jedes Stufenziel die 3 besten Aufstellungen + Alternativen je Position berechnen.
 5. Gestellte Aufstellungen: Live-Chance und Tauschvorschläge (nur solange änderbar).
 6. Meldungen für die Glocke (Ausfälle, nicht in der Startelf, starker Prognose-Abfall).
Texte werden als Schlüssel + Werte gespeichert; die Seite setzt sie auf Deutsch oder Englisch zusammen.
"""
import datetime as dt
import gzip
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import numpy as np
from scipy.stats import norm

import fetch_games
import model as M
import perfect_run
import sorare

BASE = os.path.dirname(os.path.abspath(__file__))
USER = "schattus"
OUT_DIR = os.path.join(BASE, "data", "output")
SNAP_DIR = os.path.join(BASE, "data", "snapshots")
WINDOW_DAYS = 5
LINEUP_KNOWN_MIN = 19   # ab so vielen Spielern mit Note A-E gilt die Startelf als bekannt
SLOT_NAMES = ["GK", "DEF", "MID", "FWD", "EXTRA"]
MAX_LINEUPS = 4        # so viele Aufstellungen wie Leben je Stufe; die Seite zeigt so viele wie freie Leben
NEXT_STEP_GAP = dt.timedelta(hours=2, minutes=5)  # Anstoß letztes Spiel -> nächste Stufe/Neustart stellbar (laut Fynn)

Q_BOARD = '''query($u:String!){ user(slug:$u){ setBoard(mode: CAREER, sport: FOOTBALL) { ... on CareerBoard {
  configSlug steps { level target state ... on CareerStep { id maxLineupsCount lockedLineupsCount
  pendingLineupsCount remainingLineupsCount } } } } } }'''
Q_LINEUPS = '''query($u:String!,$id:String!){ user(slug:$u){ step(id:$id){ ... on CareerStep { myTask {
  ... on ThresholdPickerTask { taskLineups { id aasmState score updatable taskAppearances { index captain bonus
  score(withBonus: true) scoreStatus lockedAt anyCard { slug } anyPlayer { slug displayName } game { id date } } } } } } } } }'''
Q_SET = '''{ cardSet { currentSet(sport: FOOTBALL) { slug name startDate endDate } nextSet(sport: FOOTBALL) { slug name startDate } } }'''
Q_CARDS = '''query($u:String!,$after:String){ user(slug:$u){ cards(first: 40, after:$after, sport: FOOTBALL, rarities:[common]) {
  nodes { slug cardEditionName cardSet { slug } skinEdition { bonus skinName variantName } anyPositions anyPlayer { slug displayName } }
  pageInfo { hasNextPage endCursor } } } }'''


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------- Abruf
def fetch_cards(set_slug, max_age_h=24):
    """Alle Karten des aktiven Sets (einmal pro Tag neu)."""
    # Abrufzeit steht in der Datei selbst (nicht Datei-Zeitstempel: GitHub setzt den beim Auschecken neu)
    path = os.path.join(BASE, "data", "cards_all.json")
    cards, fetched = None, 0.0
    if os.path.exists(path):
        with open(path) as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            cards, fetched = saved["cards"], dt.datetime.fromisoformat(saved["fetched"]).timestamp()
        else:  # altes Format: nur Liste
            cards, fetched = saved, os.path.getmtime(path)
    fresh = cards is not None and (now_utc().timestamp() - fetched) < max_age_h * 3600
    # neues Set gestartet, aber noch keine Karte davon gespeichert -> sofort neu laden
    if fresh and not any((c.get("cardSet") or {}).get("slug") == set_slug for c in cards):
        fresh = max_age_h >= 10 ** 6  # offline nie neu laden
    if not fresh:
        cards, after = [], None
        while True:
            d = sorare.query(Q_CARDS, {"u": USER, "after": after}, cache=False)
            c = d["user"]["cards"]
            cards += c["nodes"]
            if not c["pageInfo"]["hasNextPage"]:
                break
            after = c["pageInfo"]["endCursor"]
        with open(path, "w") as f:
            json.dump({"fetched": now_utc().isoformat(), "cards": cards}, f)
    by_player = defaultdict(list)
    for c in cards:
        if (c.get("cardSet") or {}).get("slug") != set_slug:
            continue
        by_player[c["anyPlayer"]["slug"]].append({
            "card": c["slug"], "name": c["anyPlayer"]["displayName"], "pos": c["anyPositions"][0],
            "bonus": round(((c.get("skinEdition") or {}).get("bonus") or 1.0) - 1.0, 4),
            "edition": (c.get("skinEdition") or {}).get("variantName") or (c.get("skinEdition") or {}).get("skinName") or "Standard"})
    for p in by_player:
        by_player[p].sort(key=lambda c: -c["bonus"])  # beste Karte zuerst
    return by_player


def fetch_upcoming(days=WINDOW_DAYS):
    t0 = now_utc()
    games = []
    d = t0.date()
    while d < (t0 + dt.timedelta(days=days)).date():
        nxt = d + dt.timedelta(days=2)
        res = sorare.query(fetch_games.Q_LIST_SET, {"f": f"{d}T00:00:00Z", "t": f"{nxt}T00:00:00Z"}, cache=False)
        for cg in res["cardSet"]["currentSet"]["competitionGames"]:
            for g in cg["games"]:
                games.append(g["id"])
        d = nxt
    # mehrere Spiele gleichzeitig abfragen (das Rate-Limit in sorare.py gilt für alle Threads zusammen)
    ids = list(dict.fromkeys(games))
    with ThreadPoolExecutor(6 if sorare.APIKEY else 3) as ex:
        fetched = list(ex.map(lambda gid: sorare.query(fetch_games.Q_GAME, {"id": gid}, cache=False)["anyGame"], ids))
    return [g for g in fetched if g and g["statusTyped"] in ("scheduled", "playing", "played")
            and M.parse_date(g["date"]) > t0 - dt.timedelta(hours=3)]


Q_STATUS = '''query($s:[String!]){ players(slugs:$s){ slug ... on Player { playingStatus
  activeInjuries { kind expectedEndDate } } } }'''


def fetch_status(slugs, batch=90):
    """Verletzungen und Spielstatus aus der Sorare-API (erlaubt, kostenlos). 90 Spieler je Abfrage
    (Komplexitätsgrenze 500)."""
    out = {}
    slugs = sorted(slugs)
    for k in range(0, len(slugs), batch):
        d = sorare.query(Q_STATUS, {"s": slugs[k:k + batch]}, cache=False)
        for p in d["players"]:
            if p and p.get("slug"):
                out[p["slug"]] = {"status": p.get("playingStatus"),
                                  "injuries": p.get("activeInjuries") or []}
    return out


def save_snapshot(games, ts):
    os.makedirs(SNAP_DIR, exist_ok=True)
    snap = {"time": ts.isoformat(), "games": [{
        "id": g["id"], "date": g["date"], "status": g["statusTyped"],
        "players": [{"p": p["anyPlayer"]["slug"], "proj": p["projectedScore"],
                     "grade": (p.get("projection") or {}).get("grade")} for p in g["playerGameScores"]]}
        for g in games]}
    path = os.path.join(SNAP_DIR, ts.strftime("%Y%m%dT%H%M") + ".json.gz")  # gepackt: ca. 10x kleiner
    with gzip.open(path, "wt") as f:
        json.dump(snap, f)
    return path


def previous_snapshot(current_path):
    if not os.path.isdir(SNAP_DIR):
        return None
    current = os.path.basename(current_path).split(".")[0]
    files = sorted(f for f in os.listdir(SNAP_DIR) if f.endswith((".json", ".json.gz")) and f.split(".")[0] != current)
    if not files:
        return None
    path = os.path.join(SNAP_DIR, files[-1])
    with (gzip.open(path, "rt") if path.endswith(".gz") else open(path)) as f:
        return json.load(f)


# ---------------------------------------------------------------- Hilfen
def team_defense(rows_games):
    """Gegentore je Team in den letzten 6 Spielen (aus gespeicherten Spielen)."""
    conceded = defaultdict(list)
    for g in sorted(rows_games, key=lambda g: g["date"]):
        if g.get("homeScore") is None:
            continue
        conceded[g["homeTeam"]["slug"]].append(g["awayScore"])
        conceded[g["awayTeam"]["slug"]].append(g["homeScore"])
    return {t: float(np.mean(v[-6:])) for t, v in conceded.items() if v}


def team_form(played):
    """Form je Team aus den gespeicherten Spielen (nur Set-Wettbewerbe): Tore und Gegentore im Schnitt der letzten 6,
    Ergebnisse der letzten 5 (W/D/L; die Seite übersetzt in S/U/N)."""
    hist = defaultdict(list)
    for g in sorted(played, key=lambda g: g["date"]):
        if g.get("homeScore") is None:
            continue
        hist[g["homeTeam"]["slug"]].append((g["homeScore"], g["awayScore"]))
        hist[g["awayTeam"]["slug"]].append((g["awayScore"], g["homeScore"]))
    out = {}
    for t, v in hist.items():
        last = v[-6:]
        out[t] = {"gf": round(float(np.mean([x for x, _ in last])), 2), "ga": round(float(np.mean([y for _, y in last])), 2),
                  "res": "".join("W" if x > y else "D" if x == y else "L" for x, y in v[-5:])}
    return out


def team_odds(st):
    """Quoten eines Teams aus der Sorare-API: Sieg/Remis/Niederlage als Wahrscheinlichkeit (Basispunkte / 10.000),
    Zu null und 3+ Tore als implizite Wahrscheinlichkeit 1 / Dezimalquote (enthält etwas Buchmacher-Marge)."""
    st = st or {}

    def bp(k):
        return round(st[k] / 10000, 4) if st.get(k) is not None else None

    def inv(k):
        return round(min(0.99, 1 / st[k]), 4) if st.get(k) else None
    return {"win": bp("winOddsBasisPoints"), "draw": bp("drawOddsBasisPoints"), "lose": bp("loseOddsBasisPoints"),
            "cs": inv("cleanSheetOdds"), "g3": inv("threeGoalsOdds")}


def load_played_games():
    import glob
    res = []
    for p in glob.glob(os.path.join(BASE, "data", "games", "*.json")):
        with open(p) as f:
            g = json.load(f)
        if g and g.get("statusTyped") == "played":
            res.append(g)
    return res


def simulate_hybrid(models, items, n_scen, seed, doubtful_fit=False):
    """Wie SetModel.simulate, aber pro Spieler das passende Modell (vor/nach Startelf) mit gemeinsamen Teamfaktoren.
    doubtful_fit=True: fragliche Spieler werden wie fitte Spieler behandelt (Schalter "fragliche Spieler einplanen")."""
    rng = np.random.default_rng(seed)
    keys = sorted({(it["game"], it["team"]) for it in items})
    kidx = {k: i for i, k in enumerate(keys)}
    z = rng.standard_normal((n_scen, len(keys)))
    raw = np.zeros((n_scen, len(items)), dtype=np.float32)
    for j, it in enumerate(items):
        m = models[it["avail"]]
        gg = m.group(it)
        fit = doubtful_fit and it.get("doubtful")
        if fit and it["avail"] == "form":
            # "als fit einplanen": wie ein Stammspieler – die Verletzungspause soll die Form-Gruppe nicht drücken
            gg = "stamm"
        factor = 1.0 if fit else it.get("pp_factor", 1.0)
        pp = m.p_play.get((it["pos"], gg), 0.7) * factor
        u = norm.cdf(m.rho * z[:, kidx[(it["game"], it["team"])]] + np.sqrt(1 - m.rho ** 2) * rng.standard_normal(n_scen))
        plays = rng.random(n_scen) < pp
        rat = m.ratio_dist(it["pos"], gg, it["proj"])
        vals = m.adjust(it, it["proj"] * np.quantile(rat, u))
        raw[:, j] = np.where(plays, np.clip(vals, 0, M.MAX_SCORE), 0.0)
        if not doubtful_fit:
            it["p_play"] = round(float(pp), 3)
        elif fit:
            it["p_play_fit"] = round(float(pp), 3)
    return raw


def slot_ok(slot, pos):
    return pos in M.SLOTS[slot][1]


# ---------------------------------------------------------------- Optimierer mit Kartenregel
def col_stats(raw):
    """Mittelwert und 85-%-Quantil je Spieler-Spiel (Rohpunkte), einmal je Szenario-Matrix statt bei jeder Suche.
    Mit Bonus werden beide nur mit (1 + Bonus) multipliziert – ein fester Faktor verschiebt Mittelwert und Quantile gleich."""
    return raw.mean(0), np.quantile(raw, 0.85, axis=0)


def optimize_cards(raw, bonus, items, target, card_free, stats, pool_size=30, rounds=8, prev_any=None):
    """Lokale Suche wie model.optimize, aber:
    - in einer Aufstellung jeder Spieler höchstens einmal,
    - ein Spieler-Spiel nur, wenn dafür noch eine freie Karte da ist (card_free[(player, game)] > 0).
    Gerechnet wird nur mit den Spalten der Vorauswahl (schnell, wenig Speicher)."""
    n = raw.shape[1]
    mean_c = stats[0] * (1 + bonus)
    q85 = stats[1] * (1 + bonus)
    if prev_any is None:
        prev_any = np.zeros(raw.shape[0], dtype=bool)
    pa = prev_any[:, None]
    avail = [i for i in range(n) if card_free.get((items[i]["player"], items[i]["game"]), 0) > 0]
    pools = []
    for s in range(5):
        el = [i for i in avail if slot_ok(s, items[i]["pos"])]
        if not el:
            return None
        top = sorted(el, key=lambda i: -mean_c[i])[:pool_size]
        top += [i for i in sorted(el, key=lambda i: -q85[i])[:pool_size // 2] if i not in top]
        pools.append(top)
    cols = sorted({i for p in pools for i in p})
    pos = {i: k for k, i in enumerate(cols)}
    counted = raw[:, cols] * (1 + bonus[cols])[None, :]
    cap = M.CAPTAIN_BONUS * raw[:, cols]

    starts = []
    for metric in (mean_c, q85):
        chosen = []
        for s in range(5):
            used = {items[i]["player"] for i in chosen}
            c = [i for i in pools[s] if items[i]["player"] not in used]
            if not c:
                return None
            chosen.append(max(c, key=lambda i: metric[i]))
        starts.append(chosen)
    best = (None, -1.0, None)
    for lu in starts:
        capt = max(lu, key=lambda i: mean_c[i])
        p = float(np.mean(prev_any | (counted[:, [pos[i] for i in lu]].sum(1) + cap[:, pos[capt]] >= target)))
        for _ in range(rounds):
            improved = False
            for s in range(5):
                others = [lu[k] for k in range(5) if k != s]
                taken = {items[i]["player"] for i in others}
                cands = [i for i in pools[s] if items[i]["player"] not in taken]
                if not cands:
                    continue
                cl = [pos[i] for i in cands]
                base = counted[:, [pos[i] for i in others]].sum(1)[:, None] + counted[:, cl]
                best_p = np.mean(pa | (base + cap[:, cl] >= target), axis=0)
                best_cap = np.array(cands)
                for o in others:
                    pr = np.mean(pa | (base + cap[:, [pos[o]]] >= target), axis=0)
                    better = pr > best_p
                    best_p = np.where(better, pr, best_p)
                    best_cap = np.where(better, o, best_cap)
                k = int(np.argmax(best_p))
                if best_p[k] > p + 1e-9:
                    lu[s], capt, p, improved = int(cands[k]), int(best_cap[k]), float(best_p[k]), True
            if not improved:
                break
        if p > best[1]:
            best = (list(lu), p, capt)
    return {"idx": best[0], "captain": best[2]}


def lineup_total(raw, bonus, idx, captain):
    return sum(raw[:, i] * (1 + bonus[i]) for i in idx) + M.CAPTAIN_BONUS * raw[:, captain]


def alternatives(raw_eval, bonus, items, lu, target, card_free, stats, k=2):
    """Je Position die k besten Ersatzspieler und wie sich die Chance dann ändert."""
    idx, capt = lu["idx"], lu["captain"]
    p0 = float(np.mean(lineup_total(raw_eval, bonus, idx, capt) >= target))
    mean_c = stats[0] * (1 + bonus)
    res = []
    for s in range(5):
        others = [idx[j] for j in range(5) if j != s]
        taken = {items[i]["player"] for i in others}
        cands = [i for i in range(len(items)) if slot_ok(s, items[i]["pos"]) and items[i]["player"] not in taken
                 and i != idx[s] and card_free.get((items[i]["player"], items[i]["game"]), 0) > 0]
        cands = sorted(cands, key=lambda i: -mean_c[i])[:40]
        opts = []
        if cands:
            rest = (raw_eval[:, others] * (1 + bonus[others])[None, :]).sum(1)
            tot = rest[:, None] + raw_eval[:, cands] * (1 + bonus[cands])[None, :]
            # war der Ersetzte Kapitän, übernimmt der Neue die Binde
            tot += M.CAPTAIN_BONUS * (raw_eval[:, [capt]] if capt != idx[s] else raw_eval[:, cands])
            ps = np.mean(tot >= target, axis=0)
            opts = [{"item": int(cands[j]), "p": round(float(ps[j]), 4), "delta": round(float(ps[j]) - p0, 4)}
                    for j in np.argsort(-ps, kind="stable")[:k]]
        res.append({"slot": SLOT_NAMES[s], "current": items[idx[s]]["player"], "options": opts})
    return res


# ---------------------------------------------------------------- Erklärungen (Schlüssel + Werte, Sprache macht die Seite)
def explain(it, defense):
    reasons = []
    if it.get("injury"):
        key = "doubtful" if it.get("doubtful") else "injured"
        return [{"key": key, "kind": it["injury"]["kind"], "until": it["injury"]["until"]}]
    if it.get("status") == "NOT_PLAYING":
        reasons.append({"key": "status_not_playing"})
    if it["avail"] == "grade":
        reasons.append({"key": "starter_confirmed"} if it.get("grade") in ("A", "B", "C", "D", "E")
                       else {"key": "not_in_lineup"})
    else:
        if it.get("st5", 0) >= 4:
            reasons.append({"key": "regular_starter", "n": it["st5"]})
        elif it.get("k5", 0) >= 3:
            reasons.append({"key": "rotation", "n": it["k5"]})
        elif it.get("n5", 0) >= 3:
            reasons.append({"key": "rarely_plays", "n": it["k5"]})
    opp = defense.get(it.get("opponent"))
    if opp is not None:
        if opp >= 1.8:
            reasons.append({"key": "opp_weak_def", "goals": round(opp, 1)})
        elif opp <= 0.8:
            reasons.append({"key": "opp_strong_def", "goals": round(opp, 1)})
    reasons.append({"key": "home" if it.get("home") else "away"})
    if it.get("avg5") is not None and it["proj"] > 0:
        if it["avg5"] >= it["proj"] * 1.15:
            reasons.append({"key": "good_form", "avg": round(it["avg5"], 1)})
        elif it["avg5"] <= it["proj"] * 0.8:
            reasons.append({"key": "weak_form", "avg": round(it["avg5"], 1)})
    return reasons[:3]


def suggest(raw_opt, raw_eval, st_opt, st_eval, bonus, items, T, sugg_free, n_opt, n_eval, with_alts=True):
    """Die bis zu MAX_LINEUPS besten Aufstellungen für ein Ziel mit den erlaubten Karten (sugg_free) + Alternativen."""
    free = dict(sugg_free)
    lus = []
    any_ok = np.zeros(n_eval, dtype=bool)
    any_opt = np.zeros(n_opt, dtype=bool)
    used = defaultdict(int)  # wie viele Kopien je (Spieler, Spiel) schon in Vorschlägen stecken
    for _ in range(MAX_LINEUPS):
        # Bonus der nächsten freien Kopie (zweite Karte kann schwächer sein als die beste)
        b_eff = np.array([it["free_cards"][min(used[(it["player"], it["game"])], len(it["free_cards"]) - 1)]["b"]
                          for it in items], dtype=np.float32)
        lu = optimize_cards(raw_opt, b_eff, items, T, free, st_opt, prev_any=any_opt)
        if lu is None:
            break
        any_opt |= lineup_total(raw_opt, b_eff, lu["idx"], lu["captain"]) >= T
        tot = lineup_total(raw_eval, b_eff, lu["idx"], lu["captain"])
        any_ok |= tot >= T
        players = []
        for s, i in enumerate(lu["idx"]):
            it = items[i]
            card = it["free_cards"][min(used[(it["player"], it["game"])], len(it["free_cards"]) - 1)]
            players.append({"item": i, "slot": SLOT_NAMES[s], "captain": i == lu["captain"], "card": card})
        for i in lu["idx"]:
            used[(items[i]["player"], items[i]["game"])] += 1
        lus.append({"players": players,
                    "p": round(float(np.mean(tot >= T)), 4), "p_any_so_far": round(float(any_ok.mean()), 4),
                    "mean": round(float(tot.mean()), 1), "p10": round(float(np.quantile(tot, 0.1)), 1),
                    "p90": round(float(np.quantile(tot, 0.9)), 1),
                    "lock": min(items[i]["date"] for i in lu["idx"]), "last": max(items[i]["date"] for i in lu["idx"]),
                    "_lu": lu})
        for i in lu["idx"]:
            free[(items[i]["player"], items[i]["game"])] -= 1
    alts = alternatives(raw_eval, bonus, items, lus[0]["_lu"], T, sugg_free, st_eval) if (lus and with_alts) else []
    for l in lus:
        l.pop("_lu")
    return {"lineups": lus, "p_any": round(float(any_ok.mean()), 4), "alternatives": alts}


# ---------------------------------------------------------------- Hauptlauf
def run(n_opt=15000, n_eval=30000, targets=None, offline=False):
    inputs_path = os.path.join(BASE, "data", "live_inputs.json")
    if offline:  # mit den zuletzt geholten Daten neu rechnen (ohne API)
        with open(inputs_path) as f:
            inp = json.load(f)
        ts = dt.datetime.fromisoformat(inp["time"])
        set_info, board, lineups, games = inp["set"], inp["board"], inp["lineups"], inp["games"]
        cards = fetch_cards(set_info["slug"], max_age_h=10 ** 6)
        steps = board["steps"]
        cur = next((s for s in steps if s["state"] != "CLAIMED"), None)
        snap_path = os.path.join(SNAP_DIR, ts.strftime("%Y%m%dT%H%M") + ".json.gz")
    else:
        ts = now_utc()
        cs = sorare.query(Q_SET, cache=False)["cardSet"]
        set_info = cs["currentSet"]
        cards = fetch_cards(set_info["slug"])
        board = sorare.query(Q_BOARD, {"u": USER}, cache=False)["user"]["setBoard"]
        steps = board["steps"]
        cur = next((s for s in steps if s["state"] != "CLAIMED"), None)
        lineups = []
        if cur:
            lineups = sorare.query(Q_LINEUPS, {"u": USER, "id": cur["id"]}, cache=False)["user"]["step"]["myTask"]["taskLineups"]
        games = fetch_upcoming()
        snap_path = save_snapshot(games, ts)
        with open(inputs_path, "w") as f:
            json.dump({"time": ts.isoformat(), "set": set_info, "board": board, "lineups": lineups, "games": games}, f)
    prev = previous_snapshot(snap_path)

    # Modelle auf allen bisherigen Spielen
    rows = M.load_rows()
    models = {a: M.SetModel("pooled", 0.3, a).fit(rows) for a in ("grade", "form")}
    # Kalibrierung aus calibrate.py (25.11.2026): Modus "spät" leicht anheben und enger machen,
    # Modus "früh" war auf der zweiten Testhälfte ohne Korrektur am besten.
    models["grade"].calib = {"mean": 1.03, "spread": 0.9}
    hist = defaultdict(list)
    for r in rows:
        hist[r["player"]].append(r)
    played = load_played_games()
    defense = team_defense(played)
    form = team_form(played)

    # Kandidaten: (Spieler, Spiel) aus Fynns Karten
    items = []
    for g in games:
        if g["statusTyped"] != "scheduled":
            continue
        n_known = sum(1 for p in g["playerGameScores"] if (p.get("projection") or {}).get("grade") in ("A", "B", "C", "D", "E"))
        # Sorare vergibt schon Tage vorher Noten nach einer erwarteten Startelf. Als "bekannt" gilt die
        # Startelf erst ab 80 Minuten vor Anpfiff (offizielle Aufstellungen kommen ca. 60 Minuten vorher).
        mins_to_kickoff = (M.parse_date(g["date"]) - ts).total_seconds() / 60
        known = n_known >= LINEUP_KNOWN_MIN and mins_to_kickoff <= 80
        for p in g["playerGameScores"]:
            slug = p["anyPlayer"]["slug"]
            if slug not in cards:
                continue
            team = ((p.get("anyPlayerGameStats") or {}).get("anyTeam") or {}).get("slug")
            home = team == g["homeTeam"]["slug"]
            prevs = hist.get(slug, [])[-5:]
            played = [x["score"] for x in prevs if x["played"]]
            it = {"player": slug, "name": cards[slug][0]["name"], "pos": cards[slug][0]["pos"],
                  "game": g["id"], "date": g["date"], "team": team, "home": home,
                  "opponent": g["awayTeam"]["slug"] if home else g["homeTeam"]["slug"],
                  "match": f'{g["homeTeam"]["name"]} – {g["awayTeam"]["name"]}', "competition": g["competition"]["slug"],
                  "proj": float(p["projectedScore"] or 0), "grade": (p.get("projection") or {}).get("grade"),
                  "avail": "grade" if known else "form",
                  "n5": len(prevs), "k5": sum(x["played"] for x in prevs), "st5": sum(x["started"] for x in prevs),
                  "avg5": float(np.mean(played)) if played else None,
                  "bonus": cards[slug][0]["bonus"], "edition": cards[slug][0]["edition"], "n_cards": len(cards[slug]),
                  "cards": [{"slug": c["card"], "ed": c["edition"], "b": c["bonus"]} for c in cards[slug]]}
            items.append(it)
    # Verletzungen und Spielstatus (Sorare-API) – online neu holen, offline aus dem Zwischenspeicher
    status_path = os.path.join(BASE, "data", "status.json")
    if offline and os.path.exists(status_path):
        with open(status_path) as f:
            status = json.load(f)
    elif not offline:
        status = fetch_status({it["player"] for it in items})
        with open(status_path, "w") as f:
            json.dump(status, f)
    else:
        status = {}
    for it in items:
        st = status.get(it["player"], {})
        it["status"] = st.get("status")
        gdate = M.parse_date(it["date"])
        inj = [i for i in st.get("injuries", [])
               if not i.get("expectedEndDate") or M.parse_date(i["expectedEndDate"]) >= gdate - dt.timedelta(days=1)]
        if inj:
            end = inj[0].get("expectedEndDate")
            it["injury"] = {"kind": inj[0].get("kind"), "until": end}
            if end and M.parse_date(end) <= gdate + dt.timedelta(days=7):
                # Rückkehr kurz vor/nach dem Spiel erwartet -> "fraglich": nur mit Schalter "an" vorschlagen
                it["doubtful"] = True
                it["pp_factor"] = 0.5   # grobe Annahme, noch nicht kalibriert
            else:
                it["out"] = True        # länger verletzt -> nie vorschlagen
                it["pp_factor"] = 0.0
        elif it["status"] == "NOT_PLAYING":
            it["pp_factor"] = 0.5     # Sorare stuft ihn als "spielt nicht" ein (grobe Annahme, noch nicht kalibriert)

    if not items:
        result = {"time": ts.isoformat(), "note": "keine kommenden Spiele"}
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(OUT_DIR, "result.json"), "w") as f:
            json.dump(result, f)
        return result

    bonus = np.array([it["bonus"] for it in items], dtype=np.float32)
    raw_opt = simulate_hybrid(models, items, n_opt, seed=11)
    raw_eval = simulate_hybrid(models, items, n_eval, seed=12)
    # Variante "fragliche Spieler einplanen": gleiche Szenarien, fragliche Spieler gelten als fit
    raw_opt_d = simulate_hybrid(models, items, n_opt, seed=11, doubtful_fit=True)
    raw_eval_d = simulate_hybrid(models, items, n_eval, seed=12, doubtful_fit=True)
    counted_eval = raw_eval * (1 + bonus)[None, :]
    for j, it in enumerate(items):
        it["mean"] = round(float(counted_eval[:, j].mean()), 1)
        it["p10"] = round(float(np.quantile(counted_eval[:, j], 0.1)), 1)
        it["p90"] = round(float(np.quantile(counted_eval[:, j], 0.9)), 1)
        it["p100"] = round(float(np.mean(raw_eval[:, j] >= 99.99)), 4)
        if it.get("doubtful"):  # Werte, wenn er als fit eingeplant wird (Schalter "fragliche Spieler")
            col = raw_eval_d[:, j] * (1 + bonus[j])
            it["mean_fit"] = round(float(col.mean()), 1)
            it["p100_fit"] = round(float(np.mean(raw_eval_d[:, j] >= 99.99)), 4)
        it["why"] = explain(it, defense)

    # Karten, die schon in gestellten Aufstellungen für ein Spiel stecken
    card_free = defaultdict(int)
    for it in items:
        card_free[(it["player"], it["game"])] = 0 if it.get("out") else it["n_cards"]
    doubtful_keys = {(it["player"], it["game"]) for it in items if it.get("doubtful")}
    used_cards = defaultdict(set)
    for lu in lineups:
        if lu["aasmState"] in ("FAILED", "SUCCEEDED", "CANCELLED", "COMPLETED"):
            continue
        for a in lu["taskAppearances"]:
            if a.get("game"):
                card_free[(a["anyPlayer"]["slug"], a["game"]["id"])] -= 1
                used_cards[(a["anyPlayer"]["slug"], a["game"]["id"])].add((a.get("anyCard") or {}).get("slug"))
    for it in items:
        it["free_cards"] = [c for c in it["cards"] if c["slug"] not in used_cards[(it["player"], it["game"])]] or it["cards"]

    # Stufenziele
    if targets is None:
        targets = sorted({s["target"] for s in steps})
    remaining = cur["remainingLineupsCount"] if cur else 0
    if cur and remaining > 0:
        focus_target = cur["target"]
        focus_note = {"key": "focus_current", "level": cur["level"] + 1, "lives": remaining}
    else:
        focus_target = steps[0]["target"]
        focus_note = {"key": "focus_after_reset", "level": 1}

    # Wenn auf der aktuellen Stufe keine Leben mehr frei sind, können neue Aufstellungen erst nach dem
    # letzten Spiel der laufenden Aufstellungen gestellt werden -> frühere Spiele nicht vorschlagen.
    earliest = ts
    if remaining == 0:
        last = [M.parse_date(a["game"]["date"]) for lu in lineups if lu["aasmState"] not in ("FAILED", "CANCELLED")
                for a in lu["taskAppearances"] if a.get("game")]
        if last:
            earliest = max(last) + NEXT_STEP_GAP
    focus_note["from"] = earliest.isoformat()

    # Zeitfenster "von–bis": Vorschläge für jedes Fenster aus Starttag und Endtag (deutsche Zeit, inkl. Sommerzeit)
    berlin = ZoneInfo("Europe/Berlin")
    it_dt = [M.parse_date(it["date"]) for it in items]
    days = sorted({d.astimezone(berlin).date() for d in it_dt if d >= earliest})

    def day_start(d):
        return dt.datetime(d.year, d.month, d.day, tzinfo=berlin)

    def day_end(d):
        return dt.datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=berlin)

    windows = [(f"{a}_{b}", max(earliest, day_start(a)), day_end(b)) for i, a in enumerate(days) for b in days[i:]]
    st_opt, st_eval = col_stats(raw_opt), col_stats(raw_eval)
    st_opt_d, st_eval_d = col_stats(raw_opt_d), col_stats(raw_eval_d)

    # Perfect Run: Plan über mehrere Stufen (perfect_run.py). Start = aktuelle Stufe mit freien Leben,
    # ohne freie Leben = Neustart auf Stufe 1 (wie der Fokus oben).
    order = sorted(steps, key=lambda st: st["level"])
    start_stage, start_lives = (cur["level"] + 1, remaining) if (cur and remaining > 0) else (1, MAX_LINEUPS)
    pr_ctx = {"items": items, "it_dt": it_dt, "card_free": card_free, "raw_opt": raw_opt, "raw_eval": raw_eval,
              "st_opt": st_opt, "st_eval": st_eval, "bonus": bonus, "n_opt": n_opt, "n_eval": n_eval,
              "suggest": suggest, "gap": NEXT_STEP_GAP, "berlin": berlin, "targets": [st["target"] for st in order],
              "start_time": earliest, "start_stage": start_stage, "start_lives": start_lives, "days": days}
    if os.environ.get("SETOPT_PR_DUMP"):  # nur zum Testen: Eingaben des Planers speichern und aufhören
        import pickle
        with open(os.environ["SETOPT_PR_DUMP"], "wb") as f:
            pickle.dump({k: v for k, v in pr_ctx.items() if k != "suggest"}, f, protocol=4)
        return None
    try:
        perfect = perfect_run.plan(pr_ctx)
        print("Perfect Run:", perfect and perfect["stats"], flush=True)
    except Exception as e:  # der Planer darf den restlichen Lauf nie verhindern
        print("Perfect Run Fehler:", repr(e)[:300], flush=True)
        perfect = None

    # Schalter "fragliche Spieler": "ohne" (Standard) schließt sie aus, "mit" lässt sie zu
    suggestions = {}
    variants = {}
    for T in targets:
        suggestions[str(T)] = {}
        variants[str(T)] = {}
        for key, w0, w1 in windows:
            usable_keys = {(it["player"], it["game"]) for it, d in zip(items, it_dt) if w0 <= d <= w1}
            free_with = {k: (v if k in usable_keys else 0) for k, v in card_free.items()}
            free_without = {k: (0 if k in doubtful_keys else v) for k, v in free_with.items()}
            suggestions[str(T)][key] = suggest(raw_opt, raw_eval, st_opt, st_eval, bonus, items, T, free_without, n_opt, n_eval)
            if usable_keys & doubtful_keys:
                variants[str(T)][key] = suggest(raw_opt_d, raw_eval_d, st_opt_d, st_eval_d, bonus, items, T, free_with,
                                                n_opt, n_eval)
            else:  # kein fraglicher Spieler im Fenster -> beide Varianten identisch (gleiche Szenarien)
                variants[str(T)][key] = suggestions[str(T)][key]

    # Top-Matchups: Quoten je Team, Form, Fynns stärkste Karten im Spiel
    by_team_game = defaultdict(list)
    for j, it in enumerate(items):
        if not it.get("out"):
            by_team_game[(it["game"], it["team"])].append(j)
    matchups = []
    for g in games:
        if g["statusTyped"] != "scheduled":
            continue
        sides = {side: team_odds(g.get(f"{side}Stats")) for side in ("home", "away")}
        for side, other in (("home", "away"), ("away", "home")):
            t = g[f"{side}Team"]
            o = sides[side]
            # erwartete Tore aus den Quoten: P(Team trifft nicht) = Zu-null-Chance des Gegners; Poisson -> λ = −ln P
            cs_opp = sides[other]["cs"]
            o["xg"] = round(float(-np.log(cs_opp)), 2) if cs_opp else None
            o.update({"slug": t["slug"], "name": t["name"], "form": form.get(t["slug"]),
                      "cards": sorted(by_team_game.get((g["id"], t["slug"]), []), key=lambda j: -items[j]["mean"])[:3]})
        matchups.append({"game": g["id"], "date": g["date"],
                         "comp": g["competition"].get("name") or g["competition"]["slug"],
                         "home": sides["home"], "away": sides["away"]})

    # Gestellte Aufstellungen: Live-Chance + Tauschvorschläge
    card_lookup = {c["card"]: c for lst in cards.values() for c in lst}
    item_index = {(it["player"], it["game"]): j for j, it in enumerate(items)}
    my_lineups = []
    for lu in lineups:
        if lu["aasmState"] in ("FAILED", "CANCELLED"):
            continue
        T = cur["target"]
        fixed, open_idx, open_mult, cards_l, missing = 0.0, [], [], [], []
        for a in sorted(lu["taskAppearances"], key=lambda a: a["index"]):
            key = (a["anyPlayer"]["slug"], (a.get("game") or {}).get("id"))
            entry = {"slot": SLOT_NAMES[a["index"]], "player": a["anyPlayer"]["slug"], "name": a["anyPlayer"]["displayName"],
                     "card": (a.get("anyCard") or {}).get("slug"),
                     "edition": card_lookup.get((a.get("anyCard") or {}).get("slug"), {}).get("edition"),
                     "captain": a["captain"], "mult": a["bonus"], "status": a["scoreStatus"],
                     "score": a["score"], "date": (a.get("game") or {}).get("date")}
            if a["scoreStatus"] in ("FINAL", "REVIEWING", "DID_NOT_PLAY"):
                fixed += a["score"]
            elif key in item_index:
                open_idx.append(item_index[key]); open_mult.append(a["bonus"])
                entry["item"] = item_index[key]
            else:
                missing.append(entry["name"])  # Spiel läuft gerade oder nicht im Fenster
                fixed += a["score"] or 0.0
            cards_l.append(entry)
        need = T - fixed
        if open_idx:
            tot = sum(raw_eval[:, j] * m for j, m in zip(open_idx, open_mult))
            p = float(np.mean(tot >= need))
        else:
            p = float(fixed >= T)
        max_possible = fixed + sum(M.MAX_SCORE * m for m in open_mult)
        entry_lu = {"id": lu["id"], "state": lu["aasmState"], "updatable": lu["updatable"], "target": T,
                    "points": round(fixed, 2), "need": round(max(need, 0), 2), "p": round(p, 4),
                    "impossible": max_possible < T, "players": cards_l, "running": missing, "swaps": []}
        # Tauschvorschläge nur, wenn noch änderbar und alle Spieler noch offen
        if lu["updatable"] and len(open_idx) == 5:
            capt_pos = next(k for k, c in enumerate(cards_l) if c["captain"])
            idx = [c["item"] for c in cards_l]
            free = dict(card_free)
            for i in idx:  # eigene Karten wieder freigeben
                free[(items[i]["player"], items[i]["game"])] += 1
            alts = alternatives(raw_eval, bonus, items, {"idx": idx, "captain": idx[capt_pos]}, T, free, st_eval, k=1)
            for a_ in alts:
                if a_["options"] and a_["options"][0]["delta"] >= 0.02:
                    entry_lu["swaps"].append({"slot": a_["slot"], "out": a_["current"], **a_["options"][0]})
        my_lineups.append(entry_lu)

    # Meldungen (Glocke)
    notes = []
    # beobachtet werden (Spieler, Spiel) aus änderbaren eigenen Aufstellungen und aus den Vorschlägen
    watch = {}
    for lu_i, l in enumerate(my_lineups):
        if l["updatable"]:
            for c in l["players"]:
                if "item" in c:
                    watch[(items[c["item"]]["player"], items[c["item"]]["game"])] = lu_i
    for T, by_day in suggestions.items():
        if int(T) == focus_target:
            for sg in by_day.values():
                for l in sg["lineups"]:
                    for pl in l["players"]:
                        watch.setdefault((items[pl["item"]]["player"], items[pl["item"]]["game"]), None)
    prev_map = {}
    if prev:
        for g in prev["games"]:
            for p in g["players"]:
                prev_map[(p["p"], g["id"])] = p
    for it in items:
        key = (it["player"], it["game"])
        if key not in watch:
            continue
        old = prev_map.get(key)
        if it.get("injury"):
            notes.append({"key": "note_doubtful" if it.get("doubtful") else "note_injured", "player": it["name"], "match": it["match"], "date": it["date"],
                          "kind": it["injury"]["kind"], "until": it["injury"]["until"], "lineup": watch[key]})
        elif it["avail"] == "grade" and it["grade"] not in ("A", "B", "C", "D", "E"):
            notes.append({"key": "note_not_starting", "player": it["name"], "match": it["match"], "date": it["date"],
                          "lineup": watch[key]})
        elif old and old.get("proj") and it["proj"] < 0.75 * old["proj"]:
            notes.append({"key": "note_proj_drop", "player": it["name"], "match": it["match"], "date": it["date"],
                          "old": round(old["proj"], 1), "new": round(it["proj"], 1), "lineup": watch[key]})
    notes_path = os.path.join(BASE, "data", "notifications.json")
    old_notes = json.load(open(notes_path)) if os.path.exists(notes_path) else []
    seen = {(n["key"], n["player"], n.get("date")) for n in old_notes}
    for n in notes:
        if (n["key"], n["player"], n.get("date")) not in seen:
            n["time"] = ts.isoformat()
            old_notes.append(n)
    with open(notes_path, "w") as f:
        json.dump(old_notes[-100:], f)

    result = {
        "time": ts.isoformat(), "set": set_info,
        "board": {"steps": [{"level": s["level"] + 1, "target": s["target"], "state": s["state"]} for s in steps],
                  "current": ({"level": cur["level"] + 1, "target": cur["target"], "remaining": remaining,
                               "pending": cur["pendingLineupsCount"], "locked": cur["lockedLineupsCount"]} if cur else None)},
        "focus": {"target": focus_target, **focus_note},
        "items": [{k: v for k, v in it.items() if k not in ("team", "opponent")} for it in items],
        "suggestions": suggestions, "suggestions_with_doubtful": variants,
        "my_lineups": my_lineups, "notifications": old_notes[-50:],
        "days": [str(d) for d in days], "matchups": matchups, "perfect": perfect,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "result.json"), "w") as f:
        json.dump(result, f)
    # Vorhersagen protokollieren, damit später die Trefferquote berechnet werden kann
    if not offline:
        with open(os.path.join(BASE, "data", "predictions_log.jsonl"), "a") as f:
            full = f"{days[0]}_{days[-1]}" if days else None  # Protokoll nur für das Fenster über alle Tage
            for T, by_win in suggestions.items():
                if full not in by_win:
                    continue
                for k, l in enumerate(by_win[full]["lineups"]):
                    f.write(json.dumps({"time": ts.isoformat(), "target": int(T), "window": full, "rank": k + 1, "p": l["p"],
                                        "players": [{"player": items[p["item"]]["player"], "game": items[p["item"]]["game"],
                                                     "mult": round(1 + items[p["item"]]["bonus"] + (0.5 if p["captain"] else 0), 3)}
                                                    for p in l["players"]]}) + "\n")
    return result


if __name__ == "__main__":
    r = run(offline="--offline" in sys.argv)
    print(json.dumps({k: r[k] for k in ("time", "board", "focus")}, indent=1, default=str))
