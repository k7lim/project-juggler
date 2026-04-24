from __future__ import annotations

"""Substring search across project names, notes, tags, and CASS session titles."""

from . import cass_facade, discover


def search(query: str, limit: int = 20) -> list[dict]:
    """Search projects by substring match across names, paths, notes, tags, and session titles."""
    query_lower = query.lower()
    all_projects, _ = discover.discover(limit=9999)

    matches = []
    seen_paths: set[str] = set()

    for p in all_projects:
        reasons = []
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

    session_hits = cass_facade.search_sessions(query, limit=limit)
    session_paths: dict[str, list[str]] = {}
    for s in session_hits:
        session_paths.setdefault(s["path"], []).append(s["title"] or "")

    for path, titles in session_paths.items():
        preview = titles[:3]
        if path in seen_paths:
            for m in matches:
                if m.get("path") == path:
                    m["match_fields"].append("session_title")
                    m["matching_titles"] = preview
                    break
        else:
            proj = next((p for p in all_projects if p.get("path") == path), None)
            if proj:
                matches.append({
                    **proj,
                    "match_fields": ["session_title"],
                    "matching_titles": preview,
                })
            else:
                matches.append({
                    "id": discover.project_id(path),
                    "name": path.rsplit("/", 1)[-1] if "/" in path else path,
                    "path": path,
                    "match_fields": ["session_title"],
                    "matching_titles": preview,
                })
            seen_paths.add(path)

    return matches[:limit]
