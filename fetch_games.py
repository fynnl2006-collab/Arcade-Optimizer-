"""Lädt alle Spiele der Set-Wettbewerbe in einem Zeitraum mit allen Spieler-Punkten und Sorare-Prognosen.
Aufruf: python3 fetch_games.py 2026-10-30 2026-11-25 [set|all]
Ergebnis: data/games/<id>.json (eine Datei pro Spiel), data/games_index_<von>_<bis>.json"""
import datetime as dt
import json
import os
import sys

import sorare

ELIGIBLE = {"bundesliga-de", "ligue-1-fr", "uefa-europa-league", "uefa-europa-conference-league",
            "premier-league-gb-eng", "laliga-es", "uefa-champions-league", "uefa-nations-league"}
GAMES_DIR = os.path.join(os.path.dirname(__file__), "data", "games")

Q_LIST_SET = '''query($f:ISO8601DateTime,$t:ISO8601DateTime){ cardSet { currentSet(sport: FOOTBALL) {
  competitionGames(fromDate:$f, toDate:$t) { competition { slug } games { id date } } } } }'''
Q_LIST_ALL = '''query($f:ISO8601DateTime,$t:ISO8601DateTime){ competitionGames(sport: FOOTBALL, fromDate:$f, toDate:$t) {
  competition { slug } games { id date } } }'''
Q_GAME = '''query($id:ID!){ anyGame(id:$id){ id date statusTyped competition { slug }
  homeTeam { slug name } awayTeam { slug name } homeScore awayScore
  playerGameScores { score scoreStatus projectedScore projection { grade reliabilityBasisPoints score }
    positionTyped anyPlayer { slug }
    anyPlayerGameStats { ... on PlayerGameStats { minsPlayed gameStarted fieldStatus anyTeam { slug } } } } } }'''


def list_games(d_from, d_to, mode):
    out = []
    day = d_from
    while day < d_to:
        nxt = min(day + dt.timedelta(days=2), d_to)
        v = {"f": day.strftime("%Y-%m-%dT00:00:00Z"), "t": nxt.strftime("%Y-%m-%dT00:00:00Z")}
        if mode == "set":
            d = sorare.query(Q_LIST_SET, v)
            cgs = d["cardSet"]["currentSet"]["competitionGames"]
        else:
            d = sorare.query(Q_LIST_ALL, v)
            cgs = d["competitionGames"]
        for cg in cgs:
            slug = cg["competition"]["slug"]
            if slug in ELIGIBLE:
                for g in cg["games"]:
                    out.append({"id": g["id"], "date": g["date"], "competition": slug})
        day = nxt
    # doppelte entfernen
    seen, res = set(), []
    for g in out:
        if g["id"] not in seen:
            seen.add(g["id"])
            res.append(g)
    return res


def fetch_game(gid, now):
    os.makedirs(GAMES_DIR, exist_ok=True)
    path = os.path.join(GAMES_DIR, gid.replace(":", "_") + ".json")
    if os.path.exists(path):
        with open(path) as f:
            g = json.load(f)
        if g.get("statusTyped") == "played":
            return g
    d = sorare.query(Q_GAME, {"id": gid}, cache=False)
    g = d["anyGame"]
    with open(path, "w") as f:
        json.dump(g, f)
    return g


def main():
    d_from = dt.date.fromisoformat(sys.argv[1])
    d_to = dt.date.fromisoformat(sys.argv[2])
    mode = sys.argv[3] if len(sys.argv) > 3 else "set"
    now = dt.datetime.now(dt.timezone.utc)
    games = list_games(d_from, d_to, mode)
    games = [g for g in games if dt.datetime.fromisoformat(g["date"].replace("Z", "+00:00")) < now]
    games.sort(key=lambda g: g["date"], reverse=True)
    idx_path = os.path.join(os.path.dirname(__file__), "data", f"games_index_{d_from}_{d_to}.json")
    with open(idx_path, "w") as f:
        json.dump(games, f)
    print(f"{len(games)} Spiele gefunden", flush=True)
    for i, g in enumerate(games):
        try:
            fetch_game(g["id"], now)
        except Exception as e:  # einzelnes Spiel überspringen, später erneut versuchen
            print("Fehler", g["id"], str(e)[:200], flush=True)
        if i % 25 == 0:
            print(f"{i + 1}/{len(games)}", flush=True)
    print("fertig", flush=True)


if __name__ == "__main__":
    main()
