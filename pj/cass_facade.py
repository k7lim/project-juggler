from __future__ import annotations

"""Facade over CASS (coding_agent_session_search) data.

Queries CASS's SQLite database directly (read-only) for complete workspace data.
cass stats --json only returns top 10 workspaces, which is insufficient.

If CASS is replaced, only this file changes (engineering_core.md #4).
"""

import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def available() -> bool:
    return shutil.which("cass") is not None


def db_path() -> Path | None:
    candidates = [
        Path.home() / "Library" / "Application Support" / "coding-agent-search" / "coding-agent-search" / "agent_search.db",
        Path.home() / ".local" / "share" / "coding-agent-search" / "agent_search.db",
    ]
    env_dir = __import__("os").environ.get("CASS_DATA_DIR")
    if env_dir:
        candidates.insert(0, Path(env_dir) / "agent_search.db")
    for p in candidates:
        if p.exists():
            return p
    return None


def list_projects() -> list[dict]:
    """All workspaces with agent, session count, and last active timestamp.

    Returns list of:
        {"path": str, "agents": [str], "session_count": int, "last_active": str|None}
    """
    db = db_path()
    if db is None:
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT w.path, COALESCE(a.slug, 'unknown'), COUNT(c.id), MAX(c.started_at) "
            "FROM conversations c "
            "JOIN workspaces w ON c.workspace_id = w.id "
            "LEFT JOIN agents a ON c.agent_id = a.id "
            "GROUP BY w.path, a.slug "
            "ORDER BY MAX(c.started_at) DESC"
        ).fetchall()
        conn.close()
    except (sqlite3.Error, OSError):
        return []

    grouped: dict[str, dict] = {}
    for path, agent, count, last_active_ms in rows:
        if path not in grouped:
            grouped[path] = {
                "path": path,
                "agents": [],
                "session_count": 0,
                "last_active": None,
            }
        g = grouped[path]
        if agent not in g["agents"]:
            g["agents"].append(agent)
        g["session_count"] += count
        if last_active_ms:
            iso = datetime.fromtimestamp(last_active_ms / 1000, tz=timezone.utc).isoformat()
            if g["last_active"] is None or iso > g["last_active"]:
                g["last_active"] = iso

    return list(grouped.values())


def recent_session_counts(days: int = 7) -> dict[str, int]:
    """Count sessions per workspace started within the last N days."""
    db = db_path()
    if db is None:
        return {}
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT w.path, COUNT(c.id) "
            "FROM conversations c "
            "JOIN workspaces w ON c.workspace_id = w.id "
            "WHERE c.started_at >= ? "
            "GROUP BY w.path",
            (cutoff_ms,),
        ).fetchall()
        conn.close()
    except (sqlite3.Error, OSError):
        return {}
    return {path: count for path, count in rows}


def search_sessions(query: str, limit: int = 20) -> list[dict]:
    """Search session titles for substring matches."""
    db = db_path()
    if db is None:
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT c.id, w.path, COALESCE(a.slug, 'unknown'), c.title, c.started_at "
            "FROM conversations c "
            "JOIN workspaces w ON c.workspace_id = w.id "
            "LEFT JOIN agents a ON c.agent_id = a.id "
            "WHERE c.title LIKE ? "
            "ORDER BY c.started_at DESC "
            "LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        conn.close()
    except (sqlite3.Error, OSError):
        return []

    results = []
    for session_id, path, agent, title, started_at_ms in rows:
        iso = None
        if started_at_ms:
            iso = datetime.fromtimestamp(started_at_ms / 1000, tz=timezone.utc).isoformat()
        results.append({
            "session_id": session_id,
            "path": path,
            "agent": agent,
            "title": title,
            "started_at": iso,
        })
    return results
