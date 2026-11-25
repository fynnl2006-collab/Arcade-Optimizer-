# Set-Optimierer

Berechnet für den Sorare-Set-Modus (Fußball) die Aufstellungen mit der höchsten Chance, das Stufenziel zu erreichen – mit der genauen Karte je Platz, Erklärungen (DE/EN), Meldungen bei Ausfällen, Live-Chancen laufender Aufstellungen und Trefferquote.

**Seite:** https://fynnl2006-collab.github.io/Arcade-Optimizer-/

Kein offizielles Sorare-Produkt. Daten: Sorare-API. Chancen sind Simulationen, keine Garantie.

## So läuft es
GitHub Actions (`.github/workflows/update.yml`) startet tagsüber alle 30 Minuten, nachts stündlich:
1. `update_games.py` – gespielte Spiele der letzten Tage nach `data/games` (Grundlage des Modells)
2. `live.py` – Galerie, Board, gestellte Aufstellungen, kommende Spiele und Verletzungen holen, simulieren, optimieren → `data/output/result.json`
3. `export_page.py` – kompakte Daten für die Seite → `data/output/page_data.json`
4. Neue Daten (Spiele, Schnappschüsse, Meldungen, Vorhersage-Protokoll) werden ins Repo geschrieben, die Seite (`site/index.html` + `page_data.json`) über GitHub Pages veröffentlicht.

## Einstellungen
- **Pages:** Settings → Pages → Source: „GitHub Actions“.
- **Optional, schneller:** Sorare-API-Key (sorare.com/settings/developer) als Secret `SORARE_API_KEY` unter Settings → Secrets and variables → Actions.
- Von Hand starten: Actions → „Set-Optimierer aktualisieren“ → Run workflow.

## Lokal
```
pip install -r requirements.txt
python update_games.py && python live.py && python export_page.py
cp data/output/page_data.json site/ && python -m http.server -d site
```
`python live.py --offline` rechnet mit den zuletzt geholten Daten neu.
