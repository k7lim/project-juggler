from __future__ import annotations

"""Cache project_index.json validated by CASS DB mtime + annotations mtime."""

import json
import os
from pathlib import Path

from . import cass_facade

CACHE_DIR = Path.home() / ".cache" / "pj"
CACHE_FILE = CACHE_DIR / "project_index.json"
ANNOTATIONS_PATH = Path.home() / ".local" / "share" / "pj" / "annotations.jsonl"


def _signatures() -> dict:
    sigs: dict[str, float] = {}
    db = cass_facade.db_path()
    if db:
        try:
            sigs["cass_db_mtime"] = os.stat(db).st_mtime
        except OSError:
            pass
    if ANNOTATIONS_PATH.exists():
        try:
            sigs["annotations_mtime"] = os.stat(ANNOTATIONS_PATH).st_mtime
        except OSError:
            pass
    return sigs


def load() -> list[dict] | None:
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE) as f:
            cached = json.load(f)
        if cached.get("signatures") != _signatures():
            return None
        return cached.get("projects")
    except (json.JSONDecodeError, OSError, KeyError):
        return None


def save(projects: list[dict]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump({"signatures": _signatures(), "projects": projects}, f)
