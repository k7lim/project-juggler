from __future__ import annotations

"""Append-only JSONL writer for project annotations.

Events are appended to ~/.local/share/pj/annotations.jsonl.
Current state is derived by replaying the log (see discover._read_annotations).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from . import discover as _discover

ANNOTATIONS_PATH = Path.home() / ".local" / "share" / "pj" / "annotations.jsonl"

VALID_PRIORITIES = ("high", "medium", "low", "none")


def _append(event: dict) -> None:
    ANNOTATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(ANNOTATIONS_PATH, "a") as f:
        f.write(json.dumps(event, default=str) + "\n")


def _base_event(project_path: str, event_type: str) -> dict:
    return {
        "type": event_type,
        "project_id": _discover.project_id(project_path),
        "project_path": project_path,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def note(project_path: str, text: str) -> dict:
    event = _base_event(project_path, "note")
    event["text"] = text
    _append(event)
    return event


def prioritize(project_path: str, level: str) -> dict:
    if level not in VALID_PRIORITIES:
        raise ValueError(f"Invalid priority {level!r}, must be one of {VALID_PRIORITIES}")
    event = _base_event(project_path, "priority")
    event["value"] = level
    _append(event)
    return event


def archive(project_path: str) -> dict:
    event = _base_event(project_path, "archive")
    _append(event)
    return event


def tag(project_path: str, tag_name: str) -> dict:
    event = _base_event(project_path, "tag")
    event["tag"] = tag_name
    _append(event)
    return event
