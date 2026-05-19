from __future__ import annotations

"""Unified search across project metadata and session content.

Searches project names, paths, notes, tags, session titles, and message content.
Results are deduplicated by project path and returned with match_fields
indicating what matched.
"""

import re
from datetime import datetime, timezone
from typing import Pattern

from . import discover
from .session_store import get_store

SEARCH_SORTS = {"newest", "oldest", "relevance"}
MATCH_MODES = {"any", "all"}


def search(
    query: str | list[str],
    limit: int = 20,
    sort: str = "newest",
    project: str | None = None,
    match: str = "any",
    regex: bool = False,
) -> list[dict]:
    """Search projects by keyword, optional project scope, and match mode."""
    if sort not in SEARCH_SORTS:
        raise ValueError(f"Unsupported search sort: {sort}")
    if match not in MATCH_MODES:
        raise ValueError(f"Unsupported match mode: {match}")

    terms = _normalize_terms(query)
    if not terms:
        return []
    patterns = _compile_patterns(terms) if regex else None

    all_projects, _ = discover.discover(limit=9999)
    scoped_projects = _filter_projects(all_projects, project)
    if project and not scoped_projects:
        return []
    allowed_paths = {p.get("path", "") for p in scoped_projects}

    matches: list[dict] = []
    seen_paths: set[str] = set()

    # Phase 1: metadata matches (name, path, note, tag)
    for p in scoped_projects:
        reasons: list[str] = []
        if _text_matches(p.get("name", ""), terms, match, patterns):
            reasons.append("name")
        if _text_matches(p.get("path", ""), terms, match, patterns):
            reasons.append("path")
        note = p.get("latest_note") or ""
        if note and _text_matches(note, terms, match, patterns):
            reasons.append("note")
        if _text_matches(" ".join(p.get("tags", [])), terms, match, patterns):
            reasons.append("tag")
        if reasons:
            matches.append({**p, "match_fields": reasons})
            seen_paths.add(p.get("path", ""))

    scan_sessions = bool(project) or regex or (len(terms) > 1 and match == "all")
    if scan_sessions:
        session_hits, content_hits = _scan_sessions(
            scoped_projects,
            terms,
            match=match,
            patterns=patterns,
            limit=limit,
            sort=sort,
        )
    else:
        session_hits = _backend_session_hits(
            terms,
            allowed_paths=allowed_paths,
            limit=limit,
            sort=sort,
            match=match,
            patterns=patterns,
        )
        content_hits = _backend_content_hits(
            terms,
            allowed_paths=allowed_paths,
            limit=limit,
            sort=sort,
            match=match,
            patterns=patterns,
        )

    _merge_session_hits(matches, seen_paths, scoped_projects, session_hits)
    _merge_content_hits(matches, seen_paths, scoped_projects, content_hits, terms, patterns)
    _dedupe_matching_sessions(matches)

    matches.sort(
        key=lambda m: _sort_key(m, terms, sort, patterns),
        reverse=(sort != "oldest"),
    )
    for m in matches:
        m["query_terms"] = terms
        m["match_mode"] = match
        m["regex"] = regex
    return matches[:limit]


def _normalize_terms(query: str | list[str]) -> list[str]:
    if isinstance(query, str):
        return [query] if query else []
    return [q for q in query if q]


def _compile_patterns(terms: list[str]) -> list[Pattern[str]]:
    try:
        return [re.compile(term, re.IGNORECASE) for term in terms]
    except re.error as exc:
        raise ValueError(f"Invalid search regex: {exc}") from exc


def _filter_projects(projects: list[dict], project: str | None) -> list[dict]:
    if not project:
        return projects
    needle = project.lower()
    return [
        p for p in projects
        if needle in p.get("name", "").lower()
        or needle in p.get("path", "").lower()
        or p.get("id", "").lower().startswith(needle)
    ]


def _backend_session_hits(
    terms: list[str],
    *,
    allowed_paths: set[str],
    limit: int,
    sort: str,
    match: str,
    patterns: list[Pattern[str]] | None,
) -> list[dict]:
    hits_by_key: dict[tuple[str, str], dict] = {}
    backend_limit = max(limit * max(len(terms), 1) * 5, limit, 100)
    for term in terms:
        for hit in get_store().search_sessions(term, limit=backend_limit, sort=sort):
            path = hit.get("path", "")
            if path not in allowed_paths:
                continue
            title = hit.get("title") or ""
            if not _text_matches(title, terms, match, patterns):
                continue
            key = (path, str(hit.get("session_id", "")))
            existing = hits_by_key.get(key)
            match_count = _count_matches(title, terms, patterns)
            if existing:
                existing["match_count"] = max(existing.get("match_count", 0), match_count)
                continue
            hits_by_key[key] = {**hit, "match_count": match_count}
    return _sort_hits(list(hits_by_key.values()), sort)[:limit]


def _backend_content_hits(
    terms: list[str],
    *,
    allowed_paths: set[str],
    limit: int,
    sort: str,
    match: str,
    patterns: list[Pattern[str]] | None,
) -> list[dict]:
    hits_by_key: dict[tuple[str, str], dict] = {}
    backend_limit = max(limit * max(len(terms), 1) * 5, limit, 100)
    for term in terms:
        for hit in get_store().search_content(term, limit=backend_limit, sort=sort):
            path = hit.get("path", "")
            if path not in allowed_paths:
                continue
            haystack = " ".join([
                hit.get("title") or "",
                hit.get("snippet") or "",
            ])
            if not _text_matches(haystack, terms, match, patterns):
                continue
            key = (path, str(hit.get("session_id", "")))
            match_count = hit.get("match_count") or _count_matches(haystack, terms, patterns)
            existing = hits_by_key.get(key)
            if existing:
                existing["match_count"] = max(existing.get("match_count", 0), match_count)
                if hit.get("snippet") and hit["snippet"] not in existing.get("snippet", ""):
                    existing["snippet"] = f"{existing.get('snippet', '')}\n{hit['snippet']}".strip()
                continue
            hits_by_key[key] = {**hit, "match_count": match_count}
    return _sort_hits(list(hits_by_key.values()), sort)[:limit]


def _scan_sessions(
    projects: list[dict],
    terms: list[str],
    *,
    match: str,
    patterns: list[Pattern[str]] | None,
    limit: int,
    sort: str,
) -> tuple[list[dict], list[dict]]:
    title_hits: list[dict] = []
    content_hits: list[dict] = []
    summaries: list[tuple[float, dict, dict]] = []

    for project in projects:
        for summary in get_store().project_sessions(project.get("path", ""), limit=9999):
            summaries.append((_parse_iso(summary.get("started_at")), project, summary))

    summaries.sort(key=lambda item: item[0], reverse=(sort != "oldest"))

    for _, project, summary in summaries:
        title = summary.get("title") or ""
        full = get_store().get_session(str(summary.get("session_id", "")))
        messages = full.get("messages", []) if full else []
        content_texts = [m.get("content", "") for m in messages]
        combined = " ".join([title, *content_texts])
        if not _text_matches(combined, terms, match, patterns):
            continue

        title_count = _count_matches(title, terms, patterns)
        if title_count:
            title_hits.append({
                "session_id": summary.get("session_id", ""),
                "path": project.get("path", ""),
                "agent": summary.get("agent", ""),
                "title": title,
                "started_at": summary.get("started_at"),
                "match_count": title_count,
            })

        content_count = _count_matches(" ".join(content_texts), terms, patterns)
        if content_count:
            snippet = _first_matching_snippet(content_texts, terms, patterns)
            content_hits.append({
                "path": project.get("path", ""),
                "session_id": summary.get("session_id", ""),
                "agent": summary.get("agent", ""),
                "title": title,
                "snippet": snippet,
                "role": "",
                "started_at": summary.get("started_at"),
                "match_type": "scan",
                "match_count": content_count,
            })

        if sort != "relevance" and len(title_hits) + len(content_hits) >= limit:
            break

    return _sort_hits(title_hits, sort)[:limit], _sort_hits(content_hits, sort)[:limit]


def _merge_session_hits(
    matches: list[dict],
    seen_paths: set[str],
    projects: list[dict],
    session_hits: list[dict],
) -> None:
    session_paths: dict[str, list[dict]] = {}
    for s in session_hits:
        session_paths.setdefault(s["path"], []).append({
            "session_id": s["session_id"],
            "agent": s.get("agent", ""),
            "title": s.get("title") or "",
            "started_at": s.get("started_at"),
            "match_type": "title",
            "match_count": s.get("match_count") or 0,
        })

    for path, sess_list in session_paths.items():
        preview = [s["title"] for s in sess_list[:3]]
        existing = next((m for m in matches if m.get("path") == path), None)
        if existing:
            if "session_title" not in existing["match_fields"]:
                existing["match_fields"].append("session_title")
            existing["matching_titles"] = preview
            existing.setdefault("matching_sessions", []).extend(sess_list)
        else:
            proj = next((p for p in projects if p.get("path") == path), None)
            matches.append({
                **(proj or _project_stub(path)),
                "match_fields": ["session_title"],
                "matching_titles": preview,
                "matching_sessions": list(sess_list),
            })
            seen_paths.add(path)


def _merge_content_hits(
    matches: list[dict],
    seen_paths: set[str],
    projects: list[dict],
    content_hits: list[dict],
    terms: list[str],
    patterns: list[Pattern[str]] | None,
) -> None:
    seen_sessions: set[tuple[str, str]] = set()
    for hit in content_hits:
        path = hit["path"]
        sid = str(hit.get("session_id", ""))
        snippet = hit.get("snippet", "")
        match_count = hit.get("match_count") or _count_matches(snippet, terms, patterns)
        sess_entry = {
            "session_id": hit.get("session_id", ""),
            "agent": hit.get("agent", ""),
            "title": hit.get("title") or "",
            "started_at": hit.get("started_at"),
            "snippet": snippet,
            "match_type": "content",
            "match_count": match_count,
        }
        existing = next((m for m in matches if m.get("path") == path), None)
        if existing:
            if "content" not in existing["match_fields"]:
                existing["match_fields"].append("content")
            existing.setdefault("snippets", []).append(snippet)
            existing["content_match_count"] = existing.get("content_match_count", 0) + match_count
            if (path, sid) not in seen_sessions:
                existing.setdefault("matching_sessions", []).append(sess_entry)
                seen_sessions.add((path, sid))
        else:
            proj = next((p for p in projects if p.get("path") == path), None)
            matches.append({
                **(proj or _project_stub(path)),
                "match_fields": ["content"],
                "snippets": [snippet],
                "matching_sessions": [sess_entry],
                "content_match_count": match_count,
            })
            seen_sessions.add((path, sid))
            seen_paths.add(path)


def _project_stub(path: str) -> dict:
    return {
        "id": discover.project_id(path),
        "name": path.rsplit("/", 1)[-1] if "/" in path else path,
        "path": path,
    }


def _dedupe_matching_sessions(matches: list[dict]) -> None:
    for m in matches:
        sessions = m.get("matching_sessions")
        if not sessions:
            continue
        seen = set()
        deduped = []
        for s in sessions:
            sid = str(s.get("session_id", ""))
            if sid in seen:
                continue
            seen.add(sid)
            deduped.append(s)
        m["matching_sessions"] = deduped


def _sort_hits(hits: list[dict], sort: str) -> list[dict]:
    if sort == "oldest":
        return sorted(hits, key=lambda h: h.get("started_at") or "")
    if sort == "relevance":
        return sorted(
            hits,
            key=lambda h: (h.get("match_count") or 0, h.get("started_at") or ""),
            reverse=True,
        )
    return sorted(hits, key=lambda h: h.get("started_at") or "", reverse=True)


def _sort_key(
    result: dict,
    terms: list[str],
    sort: str,
    patterns: list[Pattern[str]] | None,
) -> tuple:
    if sort == "oldest":
        return (_oldest_match_ts(result), _relevance_score(result, terms, patterns), result.get("name", ""))
    if sort == "relevance":
        return (_relevance_score(result, terms, patterns), _newest_match_ts(result), result.get("name", ""))
    return (_newest_match_ts(result), _relevance_score(result, terms, patterns), result.get("name", ""))


def _relevance_score(
    result: dict,
    terms: list[str],
    patterns: list[Pattern[str]] | None,
) -> int:
    weights = {
        "content": 8,
        "session_title": 5,
        "tag": 4,
        "name": 3,
        "note": 2,
        "path": 1,
    }
    score = sum(weights.get(field, 1) for field in result.get("match_fields", []))
    score += _count_matches(result.get("name", ""), terms, patterns) * 3
    score += _count_matches(result.get("path", ""), terms, patterns)
    score += _count_matches(result.get("latest_note", "") or "", terms, patterns) * 2
    for tag in result.get("tags", []):
        score += _count_matches(tag, terms, patterns) * 4
    for title in result.get("matching_titles", []):
        score += _count_matches(title or "", terms, patterns) * 5
    for snippet in result.get("snippets", []):
        score += _count_matches(snippet or "", terms, patterns)
    score += int(result.get("content_match_count") or 0)
    for session in result.get("matching_sessions", []):
        score += int(session.get("match_count") or 0)
        score += _count_matches(session.get("title", "") or "", terms, patterns) * 3
    return score


def _newest_match_ts(result: dict) -> float:
    timestamps = _match_timestamps(result)
    return max(timestamps) if timestamps else 0.0


def _oldest_match_ts(result: dict) -> float:
    timestamps = _match_timestamps(result)
    return min(timestamps) if timestamps else 0.0


def _match_timestamps(result: dict) -> list[float]:
    timestamps: list[float] = []
    for session in result.get("matching_sessions", []):
        ts = _parse_iso(session.get("started_at"))
        if ts:
            timestamps.append(ts)
    ts = _parse_iso(result.get("last_active"))
    if ts:
        timestamps.append(ts)
    return timestamps


def _parse_iso(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return 0.0


def _text_matches(
    text: str,
    terms: list[str],
    match: str,
    patterns: list[Pattern[str]] | None,
) -> bool:
    if patterns is not None:
        checks = [bool(pattern.search(text)) for pattern in patterns]
    else:
        lower = text.lower()
        checks = [term.lower() in lower for term in terms]
    return all(checks) if match == "all" else any(checks)


def _count_matches(
    text: str,
    terms: list[str],
    patterns: list[Pattern[str]] | None,
) -> int:
    if patterns is not None:
        return sum(len(pattern.findall(text)) for pattern in patterns)
    lower = text.lower()
    return sum(lower.count(term.lower()) for term in terms if term)


def _first_matching_snippet(
    texts: list[str],
    terms: list[str],
    patterns: list[Pattern[str]] | None,
    context_chars: int = 120,
) -> str:
    for text in texts:
        idx, length = _first_match(text, terms, patterns)
        if idx == -1:
            continue
        start = max(0, idx - context_chars)
        end = min(len(text), idx + length + context_chars)
        snippet = text[start:end].strip()
        if start > 0:
            snippet = "..." + snippet
        if end < len(text):
            snippet += "..."
        return snippet
    return ""


def _first_match(
    text: str,
    terms: list[str],
    patterns: list[Pattern[str]] | None,
) -> tuple[int, int]:
    if patterns is not None:
        matches = [m for pattern in patterns if (m := pattern.search(text))]
        if not matches:
            return -1, 0
        match = min(matches, key=lambda m: m.start())
        return match.start(), max(1, match.end() - match.start())

    lower = text.lower()
    positions = [
        (idx, len(term))
        for term in terms
        if term
        for idx in [lower.find(term.lower())]
        if idx != -1
    ]
    return min(positions) if positions else (-1, 0)
