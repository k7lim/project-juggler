from __future__ import annotations

"""Cache project_index.json validated by CASS DB mtime + annotations mtime."""

import json
import os

from .paths import annotations_path, cache_dir, cache_file
from .session_store import get_store


def _signatures() -> dict:
    sigs: dict[str, float] = {}
    # Backend-specific cache key
    store = get_store()
    if hasattr(store, "cache_signatures"):
        sigs.update(store.cache_signatures())
    elif hasattr(store, "db_paths"):
        for db in store.db_paths():
            try:
                sigs[f"db_mtime:{db}"] = os.stat(db).st_mtime
            except OSError:
                pass
    elif hasattr(store, "db_path"):
        db = store.db_path()
        if db:
            try:
                sigs["db_mtime"] = os.stat(db).st_mtime
            except OSError:
                pass
    ann_path = annotations_path()
    if ann_path.exists():
        try:
            sigs["annotations_mtime"] = os.stat(ann_path).st_mtime
        except OSError:
            pass
    return sigs


def signatures() -> dict:
    """Return the current cache invalidation signatures."""
    return _signatures()


def load() -> list[dict] | None:
    cf = cache_file()
    if not cf.exists():
        return None
    try:
        with open(cf) as f:
            cached = json.load(f)
        if cached.get("signatures") != _signatures():
            return None
        return cached.get("projects")
    except (json.JSONDecodeError, OSError, KeyError):
        return None


def save(projects: list[dict]) -> None:
    cd = cache_dir()
    cd.mkdir(parents=True, exist_ok=True)
    with open(cd / "project_index.json", "w") as f:
        json.dump({"signatures": _signatures(), "projects": projects}, f)
