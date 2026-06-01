from __future__ import annotations

"""Shared project session payload helpers for CLI and web surfaces."""

from . import discover
from . import resume
from .session_store import get_store


def resolve_project_detail(project_ref: str, limit: int) -> dict | None:
    """Resolve a project ref and return the same detail payload as `pj show`."""
    project = discover.resolve_project(project_ref)
    if project is None:
        return None
    return project_session_data(project, limit)


def project_session_data(project: dict, limit: int) -> dict:
    sessions = get_store().project_sessions(project["path"], limit=limit)
    resume_cmd = None
    if sessions:
        latest = sessions[0]
        resume_cmd = resume.full_resume_command(
            project["path"], latest["agent"], latest["session_id"],
        )
        details = get_store().session_details([s["session_id"] for s in sessions])
        for session in sessions:
            detail = details.get(session["session_id"], {})
            session["versions"] = detail.get("versions", [])
            session["models"] = detail.get("models", [])

    return {
        **project,
        "sessions": sessions,
        "resume_cmd": resume_cmd,
    }
