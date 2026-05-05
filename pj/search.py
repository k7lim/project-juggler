from __future__ import annotations

"""Unified search across project metadata and session content.

Searches (in order): project names, paths, notes, tags, session titles, and
message content (via CASS FTS5 or LIKE fallback). Results are deduplicated
by project path and returned with match_fields indicating what matched.
"""

from . import discover
from .session_store import get_store


def search(query: str, limit: int = 20) -> list[dict]:
    """Search projects by substring match across names, paths, notes, tags,
    session titles, and message content."""
    query_lower = query.lower()
    all_projects, _ = discover.discover(limit=9999)

    matches: list[dict] = []
    seen_paths: set[str] = set()

    # Phase 1: metadata matches (name, path, note, tag)
    for p in all_projects:
        reasons: list[str] = []
        if query_lower in p.get("name", "").lower():
            reasons.append("name")
        if query_lower in p.get("path", "").lower():
            reasons.append("path")
        note = p.get("latest_note") or ""
        if note and query_lower in note.lower():
            reasons.append("note")
        for tag in p.get("tags", []):
            if query_lower in tag.lower():
                reasons.append("tag")
                break
        if reasons:
            matches.append({**p, "match_fields": reasons})
            seen_paths.add(p.get("path", ""))

    # Phase 2: session title matches
    session_hits = get_store().search_sessions(query, limit=limit)
    session_paths: dict[str, list[dict]] = {}
    for s in session_hits:
        session_paths.setdefault(s["path"], []).append({
            "session_id": s["session_id"],
            "agent": s.get("agent", ""),
            "title": s.get("title") or "",
            "started_at": s.get("started_at"),
            "match_type": "title",
        })

    for path, sess_list in session_paths.items():
        preview = [s["title"] for s in sess_list[:3]]
        if path in seen_paths:
            for m in matches:
                if m.get("path") == path:
                    m["match_fields"].append("session_title")
                    m["matching_titles"] = preview
                    m.setdefault("matching_sessions", []).extend(sess_list)
                    break
        else:
            proj = next((p for p in all_projects if p.get("path") == path), None)
            if proj:
                matches.append({
                    **proj,
                    "match_fields": ["session_title"],
                    "matching_titles": preview,
                    "matching_sessions": list(sess_list),
                })
            else:
                matches.append({
                    "id": discover.project_id(path),
                    "name": path.rsplit("/", 1)[-1] if "/" in path else path,
                    "path": path,
                    "match_fields": ["session_title"],
                    "matching_titles": preview,
                    "matching_sessions": list(sess_list),
                })
            seen_paths.add(path)

    # Phase 3: message content matches (FTS5 or LIKE fallback)
    content_hits = get_store().search_content(query, limit=limit)
    seen_sessions: set[tuple[str, str]] = set()  # (path, session_id) dedup
    for hit in content_hits:
        path = hit["path"]
        sid = str(hit.get("session_id", ""))
        sess_entry = {
            "session_id": hit.get("session_id", ""),
            "agent": hit.get("agent", ""),
            "title": hit.get("title") or "",
            "started_at": hit.get("started_at"),
            "snippet": hit.get("snippet", ""),
            "match_type": "content",
        }
        if path in seen_paths:
            for m in matches:
                if m.get("path") == path:
                    if "content" not in m["match_fields"]:
                        m["match_fields"].append("content")
                    m.setdefault("snippets", []).append(hit["snippet"])
                    if (path, sid) not in seen_sessions:
                        m.setdefault("matching_sessions", []).append(sess_entry)
                        seen_sessions.add((path, sid))
                    break
        else:
            proj = next((p for p in all_projects if p.get("path") == path), None)
            if proj:
                matches.append({
                    **proj,
                    "match_fields": ["content"],
                    "snippets": [hit["snippet"]],
                    "matching_sessions": [sess_entry],
                })
            else:
                matches.append({
                    "id": discover.project_id(path),
                    "name": path.rsplit("/", 1)[-1] if "/" in path else path,
                    "path": path,
                    "match_fields": ["content"],
                    "snippets": [hit["snippet"]],
                    "matching_sessions": [sess_entry],
                })
            seen_sessions.add((path, sid))
            seen_paths.add(path)

    # Deduplicate matching_sessions by session_id within each result
    for m in matches:
        sessions = m.get("matching_sessions")
        if sessions:
            seen = set()
            deduped = []
            for s in sessions:
                sid = str(s.get("session_id", ""))
                if sid not in seen:
                    seen.add(sid)
                    deduped.append(s)
            m["matching_sessions"] = deduped

    return matches[:limit]
