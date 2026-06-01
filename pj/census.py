from __future__ import annotations

"""Census data shape for the live dashboard."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import discover


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _timestamp(value: str | None) -> float:
    parsed = _parse_iso(value)
    return parsed.timestamp() if parsed else 0.0


def _compact_datetime(value: str | None) -> str:
    parsed = _parse_iso(value)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed else ""


def _date(value: str | None) -> str:
    parsed = _parse_iso(value)
    return parsed.strftime("%Y-%m-%d") if parsed else ""


def _category(path: str) -> str:
    lowered = path.lower()

    def has_segment(name: str) -> bool:
        return lowered.endswith(f"/{name}") or f"/{name}/" in lowered

    if has_segment("sandbox") or lowered.startswith("/workspace/sandbox"):
        return "sandbox"
    if has_segment("teaching"):
        return "teaching"
    if has_segment("research"):
        return "research"
    if has_segment("projects"):
        return "projects"
    if has_segment("external"):
        return "external"
    if has_segment("host"):
        return "host"
    return "other"


def _origin(path: str) -> str:
    if path.startswith("/workspace/"):
        return "yolobox"
    return "mac"


def _models(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    return ", ".join(str(item) for item in value)


def normalize_project(project: dict) -> dict:
    path = project.get("path") or ""
    duration_secs = project.get("total_duration_secs") or 0
    path_obj = Path(path)

    return {
        "id": project.get("id", ""),
        "name": project.get("name") or Path(path).name or path,
        "path": path,
        "category": _category(path),
        "origin": _origin(path),
        "state": project.get("state", "dormant"),
        "sessions": project.get("session_count", 0),
        "agents": _models(project.get("agents")),
        "last_active": _compact_datetime(project.get("last_active")),
        "last_active_ts": _timestamp(project.get("last_active")),
        "first_session": _date(project.get("first_active")),
        "first_session_ts": _timestamp(project.get("first_active")),
        "duration_hrs": round(float(duration_secs) / 3600, 1),
        "models": _models(project.get("models")),
        "beads": 0,
        "has_beads": (path_obj / ".beads").is_dir(),
        "has_git": (path_obj / ".git").exists(),
        "priority": project.get("priority", "none"),
        "tags": ", ".join(project.get("tags", [])),
        "note": project.get("latest_note") or "",
        "web_hint": project.get("web_hint"),
    }


def normalize_projects(projects: list[dict]) -> list[dict]:
    return [normalize_project(project) for project in projects]


def summarize(rows: list[dict], *, total: int | None = None) -> dict:
    state_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    origin_counts: dict[str, int] = {}
    for row in rows:
        state_counts[row["state"]] = state_counts.get(row["state"], 0) + 1
        category_counts[row["category"]] = category_counts.get(row["category"], 0) + 1
        origin_counts[row["origin"]] = origin_counts.get(row["origin"], 0) + 1

    return {
        "total": total if total is not None else len(rows),
        "returned": len(rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_total": sum(int(row.get("sessions") or 0) for row in rows),
        "duration_hrs_total": round(sum(float(row.get("duration_hrs") or 0) for row in rows), 1),
        "state_counts": state_counts,
        "category_counts": category_counts,
        "origin_counts": origin_counts,
        "beads_count": sum(1 for row in rows if row.get("has_beads")),
        "git_count": sum(1 for row in rows if row.get("has_git")),
    }


def snapshot(limit: int = 10000) -> dict:
    projects, total = discover.discover(limit=limit, detail=True)
    rows = normalize_projects(projects)
    return {"rows": rows, "meta": summarize(rows, total=total)}
