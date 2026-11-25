"""Lädt die gespielten Spiele der letzten Tage nach data/games (Grundlage des Prognosemodells).
Für den Automatik-Lauf vor live.py. Holt nur Spiele, die noch fehlen oder deren Punkte noch nicht endgültig sind."""
import datetime as dt
import json
import os
import sys

import fetch_games
import sorare


def parse(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def done(path):
    """Spiel liegt vor, ist gespielt und alle Punkte sind endgültig."""
    if not os.path.exists(path):
        return False
    with open(path) as f:
        g = json.load(f)
    return bool(g) and g.get("statusTyped") == "played" and all(
        p.get("scoreStatus") not in ("REVIEWING", "PENDING") for p in g.get("playerGameScores", []))


def main(days=3):
    now = dt.datetime.now(dt.timezone.utc)
    day, end = (now - dt.timedelta(days=days)).date(), (now + dt.timedelta(days=1)).date()
    ids = []
    while day < end:
        nxt = min(day + dt.timedelta(days=2), end)
        d = sorare.query(fetch_games.Q_LIST_SET, {"f": f"{day}T00:00:00Z", "t": f"{nxt}T00:00:00Z"}, cache=False)
        for cg in d["cardSet"]["currentSet"]["competitionGames"]:  # nur Wettbewerbe des aktiven Sets
            ids += [g["id"] for g in cg["games"] if parse(g["date"]) < now - dt.timedelta(hours=2)]
        day = nxt
    os.makedirs(fetch_games.GAMES_DIR, exist_ok=True)
    new = 0
    for gid in dict.fromkeys(ids):
        path = os.path.join(fetch_games.GAMES_DIR, gid.replace(":", "_") + ".json")
        if done(path):
            continue
        try:
            g = sorare.query(fetch_games.Q_GAME, {"id": gid}, cache=False)["anyGame"]
        except Exception as e:  # einzelnes Spiel beim nächsten Lauf erneut versuchen
            print("Fehler", gid, str(e)[:200], flush=True)
            continue
        with open(path, "w") as f:
            json.dump(g, f)
        new += 1
    print(f"{len(set(ids))} Spiele geprüft, {new} neu oder aktualisiert", flush=True)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 3)
