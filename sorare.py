"""Kleiner Sorare-GraphQL-Client mit Rate-Limit und Datei-Cache.
Ohne API-Key max. 20 Abfragen/Minute; mit API-Key (Umgebungsvariable SORARE_API_KEY, z. B. als GitHub Secret) 200/Minute."""
import hashlib
import json
import os
import threading
import time
import urllib.request

ENDPOINT = "https://api.sorare.com/graphql"
CACHE_DIR = os.path.join(os.path.dirname(__file__), "data", "cache")
APIKEY = os.environ.get("SORARE_API_KEY") or None
MIN_INTERVAL = 0.35 if APIKEY else 3.2  # Sekunden zwischen Abfragen -> knapp 180 bzw. 19 pro Minute
_last_call = [0.0]
_lock = threading.Lock()  # mehrere Threads teilen sich das Rate-Limit


class SorareError(Exception):
    pass


def query(q, variables=None, cache=True, max_age=None):
    """Führt eine GraphQL-Abfrage aus. Mit cache=True wird das Ergebnis auf Platte gespeichert.
    max_age (Sekunden): ältere Cache-Einträge werden neu geladen."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = hashlib.sha1(json.dumps([q, variables], sort_keys=True).encode()).hexdigest()
    path = os.path.join(CACHE_DIR, key + ".json")
    if cache and os.path.exists(path):
        if max_age is None or time.time() - os.path.getmtime(path) < max_age:
            with open(path) as f:
                return json.load(f)
    for attempt in range(5):
        with _lock:
            wait = MIN_INTERVAL - (time.time() - _last_call[0])
            if wait > 0:
                time.sleep(wait)
            _last_call[0] = time.time()
        body = json.dumps({"query": q, "variables": variables or {}}).encode()
        headers = {"content-type": "application/json", "JWT-AUD": "set-optimizer"}
        if APIKEY:
            headers["APIKEY"] = APIKEY
        req = urllib.request.Request(ENDPOINT, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(30)
                continue
            raise SorareError(f"HTTP {e.code}: {e.read()[:300]!r}")
        if res.get("errors"):
            msg = json.dumps(res["errors"])[:600]
            if "rate" in msg.lower() or "throttl" in msg.lower():
                time.sleep(30)
                continue
            raise SorareError(msg)
        data = res["data"]
        if cache:
            with open(path, "w") as f:
                json.dump(data, f)
        return data
    raise SorareError("zu viele Wiederholungen")
