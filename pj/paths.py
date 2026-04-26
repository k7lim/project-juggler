from __future__ import annotations

"""Centralized path configuration. Supports PJ_DATA_DIR override for testing."""

import os
from pathlib import Path


def data_dir() -> Path:
    override = os.environ.get("PJ_DATA_DIR")
    if override:
        return Path(override)
    return Path.home() / ".local" / "share" / "pj"


def annotations_path() -> Path:
    return data_dir() / "annotations.jsonl"


def cache_dir() -> Path:
    override = os.environ.get("PJ_DATA_DIR")
    if override:
        return Path(override) / "cache"
    return Path.home() / ".cache" / "pj"


def cache_file() -> Path:
    return cache_dir() / "project_index.json"
