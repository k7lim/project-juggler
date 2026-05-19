from __future__ import annotations

"""Facade over CASS (coding_agent_session_search) data.

Queries CASS's SQLite database(s) directly (read-only) for complete workspace data.
Supports multiple CASS databases via PJ_CASS_DBS env var for dual-dotfile setups
(e.g. sandbox ~/.claude-yolobox indexed separately from host ~/.claude).

If CASS is replaced, only this file changes (engineering_core.md #4).
"""

import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def available() -> bool:
    return shutil.which("cass") is not None


def db_paths() -> list[Path]:
    """All CASS database paths that exist.

    Sources (checked in order, all that exist are returned):
    1. PJ_CASS_DBS env var — colon-separated list of .db paths
    2. CASS_DATA_DIR env var — single directory containing agent_search.db
    3. Platform-standard locations (macOS, Linux)
    """
    found: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path) -> None:
        resolved = str(p.resolve())
        if resolved not in seen and p.exists():
            seen.add(resolved)
            found.append(p)

    # Explicit multi-DB list
    multi = os.environ.get("PJ_CASS_DBS")
    if multi:
        for entry in multi.split(":"):
            entry = entry.strip()
            if entry:
                _add(Path(entry))

    # CASS_DATA_DIR
    env_dir = os.environ.get("CASS_DATA_DIR")
    if env_dir:
        _add(Path(env_dir) / "agent_search.db")

    # Platform defaults (macOS uses com. prefix in app support dir)
    _add(Path.home() / "Library" / "Application Support" / "com.coding-agent-search.coding-agent-search" / "agent_search.db")
    _add(Path.home() / "Library" / "Application Support" / "coding-agent-search" / "coding-agent-search" / "agent_search.db")
    _add(Path.home() / ".local" / "share" / "coding-agent-search" / "agent_search.db")

    return found


def db_path() -> Path | None:
    """Primary CASS database (first found). For backward compat."""
    paths = db_paths()
    return paths[0] if paths else None


def _query_all(sql: str, params: tuple = ()) -> list[tuple]:
    """Run a query against all CASS databases, concatenate results."""
    results: list[tuple] = []
    for db in db_paths():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            results.extend(conn.execute(sql, params).fetchall())
            conn.close()
        except (sqlite3.Error, OSError):
            continue
    return results


def list_projects(detail: bool = False) -> list[dict]:
    """All workspaces with agent, session count, and last active timestamp.

    Returns list of:
        {"path": str, "agents": [str], "session_count": int, "last_active": str|None}

    When detail=True, also includes:
        "first_active", "total_duration_secs", "models"
    """
    if detail:
        rows = _query_all(
            "SELECT w.path, COALESCE(a.slug, 'unknown'), COUNT(c.id), "
            "       MAX(c.started_at), MIN(c.started_at), "
            "       SUM(CASE WHEN c.ended_at IS NOT NULL AND c.started_at IS NOT NULL "
            "           THEN (c.ended_at - c.started_at) / 1000.0 ELSE 0 END), "
            "       GROUP_CONCAT(DISTINCT c.primary_model) "
            "FROM conversations c "
            "JOIN workspaces w ON c.workspace_id = w.id "
            "LEFT JOIN agents a ON c.agent_id = a.id "
            "GROUP BY w.path, a.slug "
            "ORDER BY MAX(c.started_at) DESC"
        )
    else:
        rows = _query_all(
            "SELECT w.path, COALESCE(a.slug, 'unknown'), COUNT(c.id), "
            "       MAX(c.started_at), NULL, NULL, NULL "
            "FROM conversations c "
            "JOIN workspaces w ON c.workspace_id = w.id "
            "LEFT JOIN agents a ON c.agent_id = a.id "
            "GROUP BY w.path, a.slug "
            "ORDER BY MAX(c.started_at) DESC"
        )

    grouped: dict[str, dict] = {}
    for path, agent, count, last_active_ms, first_ms, dur_secs, models_csv in rows:
        if path not in grouped:
            grouped[path] = {
                "path": path,
                "agents": [],
                "session_count": 0,
                "last_active": None,
            }
            if detail:
                grouped[path].update(
                    first_active=None, total_duration_secs=0.0, models=set(),
                )
        g = grouped[path]
        if agent not in g["agents"]:
            g["agents"].append(agent)
        g["session_count"] += count
        if last_active_ms:
            iso = datetime.fromtimestamp(last_active_ms / 1000, tz=timezone.utc).isoformat()
            if g["last_active"] is None or iso > g["last_active"]:
                g["last_active"] = iso
        if detail:
            if first_ms:
                first_iso = datetime.fromtimestamp(first_ms / 1000, tz=timezone.utc).isoformat()
                if g["first_active"] is None or first_iso < g["first_active"]:
                    g["first_active"] = first_iso
            if dur_secs:
                g["total_duration_secs"] += dur_secs
            if models_csv:
                for m in models_csv.split(","):
                    m = m.strip()
                    if m:
                        g["models"].add(m)

    if detail:
        for g in grouped.values():
            g["models"] = sorted(g["models"])

    return list(grouped.values())


def recent_session_counts(days: int = 7) -> dict[str, int]:
    """Count sessions per workspace started within the last N days."""
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    rows = _query_all(
        "SELECT w.path, COUNT(c.id) "
        "FROM conversations c "
        "JOIN workspaces w ON c.workspace_id = w.id "
        "WHERE c.started_at >= ? "
        "GROUP BY w.path",
        (cutoff_ms,),
    )
    # Merge counts across DBs for same workspace
    merged: dict[str, int] = {}
    for path, count in rows:
        merged[path] = merged.get(path, 0) + count
    return merged


def project_sessions(workspace_path: str, limit: int = 50) -> list[dict]:
    """All sessions for a specific workspace, most recent first."""
    rows = _query_all(
        "SELECT c.id, COALESCE(a.slug, 'unknown'), c.title, c.started_at, c.primary_model, "
        "       c.ended_at, c.total_input_tokens, c.total_output_tokens, "
        "       c.total_cache_read_tokens, c.total_cache_creation_tokens, "
        "       c.grand_total_tokens, c.user_message_count, c.assistant_message_count, "
        "       c.tool_call_count, c.api_call_count "
        "FROM conversations c "
        "JOIN workspaces w ON c.workspace_id = w.id "
        "LEFT JOIN agents a ON c.agent_id = a.id "
        "WHERE w.path = ? "
        "ORDER BY c.started_at DESC "
        "LIMIT ?",
        (workspace_path, limit),
    )

    results = []
    for (session_id, agent, title, started_at_ms, model, ended_at_ms,
         input_tok, output_tok, cache_read_tok, cache_create_tok,
         grand_total_tok, user_msgs, asst_msgs, tool_calls, api_calls) in rows:
        iso = None
        if started_at_ms:
            iso = datetime.fromtimestamp(started_at_ms / 1000, tz=timezone.utc).isoformat()
        duration_secs = None
        if started_at_ms and ended_at_ms:
            duration_secs = (ended_at_ms - started_at_ms) / 1000.0
        results.append({
            "session_id": session_id,
            "agent": agent,
            "title": title,
            "started_at": iso,
            "model": model,
            "duration_secs": duration_secs,
            "input_tokens": input_tok,
            "output_tokens": output_tok,
            "cache_read_tokens": cache_read_tok,
            "cache_creation_tokens": cache_create_tok,
            "total_tokens": grand_total_tok,
            "user_messages": user_msgs,
            "assistant_messages": asst_msgs,
            "tool_calls": tool_calls,
            "api_calls": api_calls,
        })
    # Re-sort across DBs and apply limit
    results.sort(key=lambda r: r["started_at"] or "", reverse=True)
    return results[:limit]


def session_details(session_ids: list[int]) -> dict[int, dict]:
    """Fetch harness version and per-message models for a set of sessions.

    Returns {session_id: {"versions": [str], "models": [str]}}.
    Versions come from messages.extra_json; models from messages.author.
    """
    if not session_ids:
        return {}

    placeholders = ",".join("?" for _ in session_ids)
    rows = _query_all(
        f"SELECT m.conversation_id, m.author, "
        f"       json_extract(m.extra_json, '$.version') "
        f"FROM messages m "
        f"WHERE m.conversation_id IN ({placeholders})",
        tuple(session_ids),
    )

    acc: dict[int, dict[str, set]] = {}
    for conv_id, author, version in rows:
        if conv_id not in acc:
            acc[conv_id] = {"versions": set(), "models": set()}
        if version:
            acc[conv_id]["versions"].add(version)
        if author and author not in ("reasoning", "user", "<synthetic>"):
            acc[conv_id]["models"].add(author)

    return {
        sid: {"versions": sorted(v["versions"]), "models": sorted(v["models"])}
        for sid, v in acc.items()
    }


def search_sessions(query: str, limit: int = 20, sort: str = "newest") -> list[dict]:
    """Search session titles for substring matches."""
    order = "ASC" if sort == "oldest" else "DESC"
    rows = _query_all(
        "SELECT c.id, w.path, COALESCE(a.slug, 'unknown'), c.title, c.started_at "
        "FROM conversations c "
        "JOIN workspaces w ON c.workspace_id = w.id "
        "LEFT JOIN agents a ON c.agent_id = a.id "
        "WHERE c.title LIKE ? "
        f"ORDER BY c.started_at {order} "
        "LIMIT ?",
        (f"%{query}%", limit),
    )

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
    if sort == "oldest":
        results.sort(key=lambda r: r["started_at"] or "")
    elif sort == "relevance":
        results.sort(
            key=lambda r: (
                _count_occurrences(r.get("title") or "", query),
                r["started_at"] or "",
            ),
            reverse=True,
        )
    else:
        results.sort(key=lambda r: r["started_at"] or "", reverse=True)
    return results[:limit]


def _fts_available(db: Path) -> bool:
    """Check if FTS5 virtual table is usable in this DB."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM fts_messages LIMIT 1")
        conn.close()
        return True
    except (sqlite3.Error, OSError):
        return False


def search_content(query: str, limit: int = 20, sort: str = "newest") -> list[dict]:
    """Full-text search across session message content.

    Tries FTS5 first (fast), falls back to LIKE on messages.content (slow but works).
    Returns hits grouped by workspace path with snippet context.

    Returns list of:
        {"path": str, "session_id": int, "agent": str, "snippet": str,
         "started_at": str|None, "title": str|None, "match_type": "fts"|"like"}
    """
    results: list[dict] = []
    seen: set[tuple] = set()  # (db_path, message_id) dedup

    for db in db_paths():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except (sqlite3.Error, OSError):
            continue

        hits: list[tuple] = []
        match_type = "like"

        time_order = "ASC" if sort == "oldest" else "DESC"

        # Try FTS5 first
        try:
            hits = conn.execute(
                "SELECT m.id, m.conversation_id, m.content, m.role, "
                "       c.title, c.started_at, w.path, COALESCE(a.slug, 'unknown') "
                "FROM fts_messages f "
                "JOIN messages m ON f.rowid = m.id "
                "JOIN conversations c ON m.conversation_id = c.id "
                "JOIN workspaces w ON c.workspace_id = w.id "
                "LEFT JOIN agents a ON c.agent_id = a.id "
                "WHERE fts_messages MATCH ? "
                "ORDER BY rank "
                "LIMIT ?",
                (query, limit * 2),
            ).fetchall()
            match_type = "fts"
        except sqlite3.Error:
            pass

        # Fall back to LIKE on message content
        if not hits:
            try:
                hits = conn.execute(
                    "SELECT m.id, m.conversation_id, m.content, m.role, "
                    "       c.title, c.started_at, w.path, COALESCE(a.slug, 'unknown') "
                    "FROM messages m "
                    "JOIN conversations c ON m.conversation_id = c.id "
                    "JOIN workspaces w ON c.workspace_id = w.id "
                    "LEFT JOIN agents a ON c.agent_id = a.id "
                    "WHERE m.content LIKE ? "
                    f"ORDER BY c.started_at {time_order} "
                    "LIMIT ?",
                    (f"%{query}%", limit * 2),
                ).fetchall()
                match_type = "like"
            except sqlite3.Error:
                pass

        conn.close()

        for msg_id, conv_id, content, role, title, started_at_ms, path, agent in hits:
            key = (str(db), msg_id)
            if key in seen:
                continue
            seen.add(key)

            snippet = _extract_snippet(content or "", query, context_chars=120)
            iso = None
            if started_at_ms:
                iso = datetime.fromtimestamp(started_at_ms / 1000, tz=timezone.utc).isoformat()

            results.append({
                "path": path,
                "session_id": conv_id,
                "agent": agent,
                "title": title,
                "snippet": snippet,
                "role": role,
                "started_at": iso,
                "match_type": match_type,
                "match_count": _count_occurrences(content or "", query),
            })

    if sort == "oldest":
        results.sort(key=lambda r: r["started_at"] or "")
    elif sort == "relevance":
        results.sort(
            key=lambda r: (r.get("match_count") or 0, r["started_at"] or ""),
            reverse=True,
        )
    else:
        results.sort(key=lambda r: r["started_at"] or "", reverse=True)

    # Keep only the best hit per session (first = most recent message)
    seen_sessions: set[tuple] = set()
    deduped: list[dict] = []
    for r in results:
        key = (r["path"], r["session_id"])
        if key not in seen_sessions:
            seen_sessions.add(key)
            deduped.append(r)
    return deduped[:limit]


def get_session(
    session_id: str,
    *,
    all_branches: bool = False,
    include_tools: bool = True,
    roles: set[str] | None = None,
) -> dict | None:
    """CASS indexes metadata only; delegate to fs_store for full sessions."""
    from . import fs_store
    return fs_store.get_session(
        session_id,
        all_branches=all_branches,
        include_tools=include_tools,
        roles=roles,
    )


def _extract_snippet(content: str, query: str, context_chars: int = 120) -> str:
    """Extract a snippet around the first occurrence of query in content."""
    lower = content.lower()
    query_lower = query.lower()
    idx = lower.find(query_lower)
    if idx == -1:
        # FTS may match stemmed/tokenized forms; return start of content
        return content[:context_chars * 2].strip()

    start = max(0, idx - context_chars)
    end = min(len(content), idx + len(query) + context_chars)
    snippet = content[start:end].strip()

    if start > 0:
        snippet = "..." + snippet
    if end < len(content):
        snippet = snippet + "..."
    return snippet


def _count_occurrences(text: str, query: str) -> int:
    if not query:
        return 0
    return text.lower().count(query.lower())
