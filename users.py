"""Welche Sorare-Manager gerechnet werden (users.txt) und wo ihre Daten liegen (data/users/<slug>/)."""
import os
import re

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT = "schattus"
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,60}")


def load_users():
    """Slugs aus users.txt: einer pro Zeile, # leitet einen Kommentar ein, Groß/klein egal."""
    path = os.path.join(BASE, "users.txt")
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.split("#", 1)[0].strip().lower()
                if s and SLUG_RE.fullmatch(s) and s not in out:
                    out.append(s)
    return out or [DEFAULT]


def user_dir(slug):
    if not SLUG_RE.fullmatch(slug):
        raise ValueError(f"ungültiger Managername: {slug!r}")
    d = os.path.join(BASE, "data", "users", slug)
    os.makedirs(d, exist_ok=True)
    return d
