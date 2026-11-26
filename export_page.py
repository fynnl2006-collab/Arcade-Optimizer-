"""Erzeugt aus data/output/result.json die kompakten Daten für die Seite (data/output/page_data.json).
Nur Spieler, die in Vorschlägen, Alternativen, eigenen Aufstellungen oder der Spielerliste vorkommen."""
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
POS = {"Goalkeeper": "G", "Defender": "D", "Midfielder": "M", "Forward": "F"}


def main():
    with open(os.path.join(BASE, "data", "output", "result.json")) as f:
        r = json.load(f)
    with open(os.path.join(BASE, "i18n.json")) as f:
        i18n = json.load(f)
    items = r["items"]
    used = set()

    # Karten-Tabelle: jede Karte (Slug, Edition, Bonus) nur einmal, Vorschläge verweisen per Nummer darauf
    card_tab, card_ids = [], {}

    def cid(c):
        if c["slug"] not in card_ids:
            card_ids[c["slug"]] = len(card_tab)
            card_tab.append([c["slug"], c["ed"], c["b"]])
        return card_ids[c["slug"]]

    def pack_sugg(by_target):
        out = {}
        for T, by_day in by_target.items():
            out[T] = {}
            for d, sg in by_day.items():
                lus = []
                for l in sg["lineups"]:
                    for p in l["players"]:
                        used.add(p["item"])
                    lus.append({"pl": [[p["item"], p["slot"], 1 if p["captain"] else 0, cid(p["card"])]
                                       for p in l["players"]],
                                "p": l["p"], "pa": l["p_any_so_far"], "mean": l["mean"],
                                "p10": l["p10"], "p90": l["p90"], "lock": l["lock"], "last": l["last"]})
                alts = []
                for a in sg["alternatives"]:
                    for o in a["options"]:
                        used.add(o["item"])
                    alts.append({"slot": a["slot"], "opts": [[o["item"], o["p"], o["delta"]] for o in a["options"]]})
                out[T][d] = {"l": lus, "any": sg["p_any"], "alts": alts}
        return out

    sugg = {"0": pack_sugg(r["suggestions"]), "1": pack_sugg(r["suggestions_with_doubtful"])}
    # Variante "fragliche einplanen" = null, wenn sie gleich der Standardvariante ist (kein fraglicher Spieler im Fenster)
    for T, by_win in sugg["1"].items():
        for w, v in by_win.items():
            if v == sugg["0"].get(T, {}).get(w):
                by_win[w] = None

    my = []
    for l in r["my_lineups"]:
        pl = []
        for c in l["players"]:
            if "item" in c:
                used.add(c["item"])
            pl.append({"slot": c["slot"], "name": c["name"], "cap": c["captain"], "mult": c["mult"], "card": c.get("card"), "ed": c.get("edition"),
                       "status": c["status"], "score": c["score"], "date": c["date"], "item": c.get("item")})
        swaps = []
        for s in l["swaps"]:
            used.add(s["item"])
            swaps.append({"slot": s["slot"], "item": s["item"], "p": s["p"], "delta": s["delta"],
                          "out": next((c["name"] for c in l["players"] if c["player"] == s["out"]), s["out"])})
        my.append({"id": l["id"], "updatable": l["updatable"], "target": l["target"], "points": l["points"],
                   "need": l["need"], "p": l["p"], "impossible": l["impossible"], "pl": pl, "swaps": swaps})

    # Spielerliste: je Position die 25 besten (nach erwarteten Punkten) ab dem frühesten möglichen Start
    start = r["focus"].get("from", r["time"])
    top = {}
    for pos, short in POS.items():
        cand = [j for j, it in enumerate(items) if it["pos"] == pos and not it.get("out")
                and it["date"] >= start.replace("+00:00", "Z")[:19]]
        cand.sort(key=lambda j: -items[j]["mean"])
        top[short] = cand[:25]
        used.update(top[short])

    # Perfect Run: Aufstellungen kompakt (Karten-Nummern statt Karten-Daten), benutzte Spieler merken
    perfect = r.get("perfect")
    if perfect:
        lu = {}
        for key, v in perfect["lu"].items():
            ls = []
            for l in v["l"]:
                used.update(pl["item"] for pl in l["players"])
                ls.append({"p": l["p"], "pl": [[pl["item"], pl["slot"], 1 if pl["captain"] else 0, cid(pl["card"])]
                                               for pl in l["players"]]})
            lu[key] = {"pa": v["pa"], "l": ls}
        perfect = dict(perfect, lu=lu)

    # Top-Matchups: kommen schon kompakt aus live.py; nur die Karten je Team für die Seite mitnehmen
    matchups = r.get("matchups", [])
    for m in matchups:
        for side in ("home", "away"):
            used.update(m[side].get("cards") or [])

    packed = {}
    for j in sorted(used):
        it = items[j]
        packed[j] = {"n": it["name"], "pos": POS[it["pos"]], "ed": it["edition"], "b": it["bonus"],
                     "d": it["date"], "m": it["match"], "c": it["competition"], "mean": it["mean"],
                     "p10": it["p10"], "p90": it["p90"], "pp": it.get("p_play"), "p100": it["p100"],
                     "proj": it["proj"], "av": it["avail"], "why": it["why"], "nc": it["n_cards"],
                     "dbt": 1 if it.get("doubtful") else 0, "inj": it.get("injury")}
        if it.get("doubtful"):  # Werte für den Schalter "fragliche Spieler als fit einplanen"
            packed[j].update({"mf": it.get("mean_fit"), "ppf": it.get("p_play_fit"), "cf": it.get("p100_fit")})

    # Backtest-Kennzahlen (Modus "spät" und "früh") als Vertrauensanker, bis echte Trefferquote vorliegt
    bt = {}
    for mode in ("grade", "form"):
        fp = os.path.join(BASE, "data", f"backtest_result_{mode}.json")
        if os.path.exists(fp):
            with open(fp) as f:
                b = json.load(f)["aufstellungen"]
            bt[mode] = {"best": [b["1"]["mittl_vorhergesagt"], b["1"]["geschafft_anteil"]],
                        "any": [b["mind. eine von 3"]["mittl_vorhergesagt"], b["mind. eine von 3"]["geschafft_anteil"]],
                        "n": b["1"]["n"],
                        "by": {t: [v["vorhergesagt"], v["geschafft"]] for t, v in b["1"]["je_ziel"].items()}}
    page = {"time": r["time"], "backtest": bt, "set": r["set"], "board": r["board"], "focus": r["focus"],
            "targets": sorted(int(t) for t in r["suggestions"]), "days": r.get("days") or sorted(next(iter(r["suggestions"].values())).keys()),
            "matchups": matchups,
            "items": packed, "sugg": sugg, "cards": card_tab, "perfect": perfect, "my": my, "notes": r["notifications"], "top": top, "i18n": i18n}
    path = os.path.join(BASE, "data", "output", "page_data.json")
    with open(path, "w") as f:
        json.dump(page, f, ensure_ascii=False, separators=(",", ":"))
    print(path, os.path.getsize(path), "Bytes,", len(packed), "Spieler-Spiele")


if __name__ == "__main__":
    main()
