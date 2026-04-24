from __future__ import annotations

"""Merge CASS session data + annotations into a unified project list."""

import hashlib
import json
from pathlib import Path

from . import cache, cass_facade, state

ANNOTATIONS_PATH = Path.home() / ".local" / "share" / "pj" / "annotations.jsonl"


def project_id(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()[:8]


def _read_annotations() -> dict[str, dict]:
    """Replay annotations.jsonl into per-project state keyed by project_id."""
    projects: dict[str, dict] = {}
    if not ANNOTATIONS_PATH.exists():
        return projects
    try:
        with open(ANNOTATIONS_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                pid = event.get("project_id", "")
                if not pid:
                    continue
                if pid not in projects:
                    projects[pid] = {"project_path": event.get("project_path")}
                ann = projects[pid]
                if event.get("project_path"):
                    ann["project_path"] = event["project_path"]
                etype = event.get("type")
                if etype == "priority":
                    ann["priority"] = event.get("value", "none")
                elif etype == "archive":
                    ann["archived"] = True
                elif etype == "unarchive":
                    ann["archived"] = False
                elif etype == "note":
                    ann.setdefault("notes", []).append(event.get("text", ""))
                elif etype == "tag":
                    ann.setdefault("tags", set()).add(event.get("tag", ""))
    except OSError:
        return projects
    for ann in projects.values():
        if "tags" in ann and isinstance(ann["tags"], set):
            ann["tags"] = sorted(ann["tags"])
    return projects


def _build_project(path: str, cass_data: dict | None, ann: dict) -> dict:
    pid = project_id(path)
    notes = ann.get("notes", [])
    latest_note = notes[-1] if notes else None
    blocked = bool(latest_note and latest_note.lower().startswith("blocked:"))
    archived = ann.get("archived", False)
    last_active = cass_data["last_active"] if cass_data else None

    return {
        "id": pid,
        "name": Path(path).name,
        "path": path,
        "agents": cass_data["agents"] if cass_data else [],
        "session_count": cass_data["session_count"] if cass_data else 0,
        "last_active": last_active,
        "state": state.derive(last_active, archived=archived, blocked=blocked),
        "priority": ann.get("priority", "none"),
        "tags": ann.get("tags", []),
        "latest_note": latest_note,
    }


def resolve_project(query: str) -> dict | None:
    """Fuzzy-match a project by name, path substring, or id prefix."""
    all_projects, _ = discover(limit=9999)
    if not all_projects:
        return None

    query_lower = query.lower()

    for p in all_projects:
        if p["id"].startswith(query_lower):
            return p

    for p in all_projects:
        if p["name"].lower() == query_lower:
            return p

    for p in all_projects:
        if p["path"].lower() == query_lower:
            return p

    matches = []
    for p in all_projects:
        if query_lower in p["name"].lower():
            matches.append(p)
    if len(matches) == 1:
        return matches[0]

    if not matches:
        for p in all_projects:
            if query_lower in p["path"].lower():
                matches.append(p)
        if len(matches) == 1:
            return matches[0]

    return None


def discover(
    state_filter: str | None = None,
    sort: str = "last-active",
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Discover projects from CASS + annotations. Returns (page, total)."""
    cached = cache.load()
    if cached is not None:
        projects = cached
    else:
        cass_projects = cass_facade.list_projects()
        annotations = _read_annotations()
        seen_paths: set[str] = set()
        projects = []

        for cp in cass_projects:
            path = cp["path"]
            seen_paths.add(path)
            pid = project_id(path)
            ann = annotations.pop(pid, {})
            projects.append(_build_project(path, cp, ann))

        for pid, ann in annotations.items():
            path = ann.get("project_path")
            if path and path not in seen_paths:
                projects.append(_build_project(path, None, ann))

        cache.save(projects)

    if state_filter:
        projects = [p for p in projects if p["state"] == state_filter]

    total = len(projects)

    if sort == "last-active":
        projects.sort(key=lambda p: p.get("last_active") or "", reverse=True)
    elif sort == "priority":
        order = {"high": 0, "medium": 1, "low": 2, "none": 3}
        projects.sort(key=lambda p: order.get(p.get("priority", "none"), 3))
    elif sort == "name":
        projects.sort(key=lambda p: p.get("name", "").lower())

    return projects[offset : offset + limit], total
