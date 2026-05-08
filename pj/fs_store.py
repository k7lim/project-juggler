"""Filesystem-first session store.

Reads agent session files directly — no index, no external dependency.
Implements the SessionStore protocol (session_store.py).

Auto-detects installed agents by scanning known dotfile locations.
Additional roots can be added via PJ_SOURCES env var.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .parsers import base
from .parsers import claude_code, codex, kimi

# Registry of available parsers
_PARSERS = [claude_code, codex, kimi]


def _configured_roots() -> list[tuple[str, object]]:
    """Return (root_path, parser_module) pairs from auto-detection + env config.

    PJ_SOURCES format: agent:path:agent:path:...
    Example: claude:~/.claude-yolobox/projects:hermes:~/.pj/mirrors/m1/.hermes/sessions
    """
    pairs: list[tuple[str, object]] = []
    seen: set[str] = set()

    def _add(root: str, parser) -> None:
        resolved = os.path.realpath(os.path.expanduser(root))
        if resolved not in seen and os.path.isdir(resolved):
            seen.add(resolved)
            pairs.append((resolved, parser))

    # Env var overrides / additions
    sources = os.environ.get("PJ_SOURCES", "")
    if sources:
        parts = sources.split(":")
        slug_to_parser = {p.agent_slug: p for p in _PARSERS}
        # Accept short aliases
        slug_to_parser.setdefault("claude", slug_to_parser.get("claude_code"))  # type: ignore[arg-type]
        for i in range(0, len(parts) - 1, 2):
            agent_name = parts[i].strip()
            path = parts[i + 1].strip()
            parser = slug_to_parser.get(agent_name)
            if parser and path:
                _add(path, parser)

    # Auto-detect from defaults
    for parser in _PARSERS:
        for root in parser.detect_roots():
            _add(root, parser)

    return pairs


def _all_sessions_metadata() -> list[base.NormalizedSession]:
    """Scan all roots, parse metadata only (fast)."""
    sessions = []
    for root, parser in _configured_roots():
        for path in parser.list_sessions(root):
            meta = parser.parse_metadata(path)
            if meta:
                sessions.append(meta)
    return sessions


def _group_by_workspace(
    sessions: list[base.NormalizedSession], detail: bool = False,
) -> dict[str, dict]:
    """Group sessions into project dicts matching SessionStore.list_projects() shape."""
    grouped: dict[str, dict] = {}
    for s in sessions:
        ws = s.workspace or "(no workspace)"
        if ws not in grouped:
            grouped[ws] = {
                "path": ws,
                "agents": [],
                "session_count": 0,
                "last_active": None,
            }
            if detail:
                grouped[ws].update(
                    first_active=None, total_duration_secs=0.0, models=set(),
                )
        g = grouped[ws]
        if s.agent not in g["agents"]:
            g["agents"].append(s.agent)
        g["session_count"] += 1
        if s.started_at:
            iso = datetime.fromtimestamp(s.started_at / 1000, tz=timezone.utc).isoformat()
            if g["last_active"] is None or iso > g["last_active"]:
                g["last_active"] = iso
            if detail:
                if g["first_active"] is None or iso < g["first_active"]:
                    g["first_active"] = iso
        if detail:
            if s.started_at and s.ended_at:
                g["total_duration_secs"] += (s.ended_at - s.started_at) / 1000.0
            if s.model:
                g["models"].add(s.model)
    if detail:
        for g in grouped.values():
            g["models"] = sorted(g["models"])
    return grouped


def cache_signatures() -> dict[str, float]:
    """Return session collection stats for cache invalidation.

    Session files are usually nested below the detected roots. A running
    session appends to an existing JSONL file, which does not necessarily
    update the top-level root directory mtime, so include cheap aggregate
    stats from the actual session files.
    """
    sigs: dict[str, int | float] = {}
    for root, parser in _configured_roots():
        prefix = f"{getattr(parser, 'agent_slug', 'agent')}:{root}"
        try:
            sigs[f"root_mtime:{prefix}"] = os.stat(root).st_mtime_ns
        except OSError:
            pass
        session_count = 0
        newest_mtime = 0
        total_size = 0
        for session_path in parser.list_sessions(root):
            session_count += 1
            for sig_path in _signature_paths(session_path):
                try:
                    st = os.stat(sig_path)
                except OSError:
                    continue
                newest_mtime = max(newest_mtime, st.st_mtime_ns)
                total_size += st.st_size
        sigs[f"session_count:{prefix}"] = session_count
        sigs[f"session_newest_mtime:{prefix}"] = newest_mtime
        sigs[f"session_total_size:{prefix}"] = total_size
    return sigs


def _signature_paths(session_path: str) -> list[str]:
    """Files whose stats represent a parser session path."""
    if os.path.isfile(session_path):
        return [session_path]
    if not os.path.isdir(session_path):
        return []

    files: list[str] = []
    for filename in ("state.json", "wire.jsonl", "context.jsonl"):
        path = os.path.join(session_path, filename)
        if os.path.isfile(path):
            files.append(path)
    return files


# --- SessionStore interface ---

def available() -> bool:
    return bool(_configured_roots())


def list_projects(detail: bool = False) -> list[dict]:
    sessions = _all_sessions_metadata()
    grouped = _group_by_workspace(sessions, detail=detail)
    return sorted(grouped.values(), key=lambda p: p["last_active"] or "", reverse=True)


def recent_session_counts(days: int = 7) -> dict[str, int]:
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    sessions = _all_sessions_metadata()
    counts: dict[str, int] = {}
    for s in sessions:
        if s.started_at and s.started_at >= cutoff_ms:
            ws = s.workspace or "(no workspace)"
            counts[ws] = counts.get(ws, 0) + 1
    return counts


def project_sessions(workspace_path: str, limit: int = 50) -> list[dict]:
    results = []
    for root, parser in _configured_roots():
        for path in parser.list_sessions(root):
            meta = parser.parse_metadata(path)
            if not meta or meta.workspace != workspace_path:
                continue
            duration_secs = None
            if meta.started_at and meta.ended_at:
                duration_secs = (meta.ended_at - meta.started_at) / 1000.0
            iso = None
            if meta.started_at:
                iso = datetime.fromtimestamp(meta.started_at / 1000, tz=timezone.utc).isoformat()
            results.append({
                "session_id": meta.session_id,
                "agent": meta.agent,
                "title": meta.title,
                "started_at": iso,
                "model": meta.model,
                "duration_secs": duration_secs,
                "input_tokens": None,
                "output_tokens": None,
                "cache_read_tokens": None,
                "cache_creation_tokens": None,
                "total_tokens": None,
                "user_messages": None,
                "assistant_messages": None,
                "tool_calls": None,
                "api_calls": None,
            })
    results.sort(key=lambda r: r["started_at"] or "", reverse=True)
    return results[:limit]


def session_details(session_ids: list) -> dict:
    # FS store doesn't track per-message models separately
    return {}


def search_sessions(query: str, limit: int = 20) -> list[dict]:
    """Search session titles for substring matches."""
    query_lower = query.lower()
    results = []
    for root, parser in _configured_roots():
        for path in parser.list_sessions(root):
            meta = parser.parse_metadata(path)
            if not meta or not meta.title:
                continue
            if query_lower in meta.title.lower():
                iso = None
                if meta.started_at:
                    iso = datetime.fromtimestamp(meta.started_at / 1000, tz=timezone.utc).isoformat()
                results.append({
                    "session_id": meta.session_id,
                    "path": meta.workspace or "",
                    "agent": meta.agent,
                    "title": meta.title,
                    "started_at": iso,
                })
    results.sort(key=lambda r: r["started_at"] or "", reverse=True)
    return results[:limit]


def search_content(query: str, limit: int = 20) -> list[dict]:
    """Search message content via substring match (no index)."""
    query_lower = query.lower()
    results: list[dict] = []
    seen_sessions: set[str] = set()

    for root, parser in _configured_roots():
        for path in parser.list_sessions(root):
            session = parser.parse_session(path)
            if not session:
                continue

            for msg in session.messages:
                if query_lower in msg.content.lower():
                    key = f"{session.agent}:{session.session_id}"
                    if key in seen_sessions:
                        break
                    seen_sessions.add(key)

                    snippet = _extract_snippet(msg.content, query)
                    iso = None
                    if session.started_at:
                        iso = datetime.fromtimestamp(
                            session.started_at / 1000, tz=timezone.utc
                        ).isoformat()

                    results.append({
                        "path": session.workspace or "",
                        "session_id": session.session_id,
                        "agent": session.agent,
                        "title": session.title,
                        "snippet": snippet,
                        "role": msg.role,
                        "started_at": iso,
                        "match_type": "grep",
                    })
                    break  # one hit per session

            if len(results) >= limit:
                break
        if len(results) >= limit:
            break

    results.sort(key=lambda r: r["started_at"] or "", reverse=True)
    return results[:limit]


def get_session(
    session_id: str,
    *,
    all_branches: bool = False,
    include_tools: bool = True,
    roles: set[str] | None = None,
) -> dict | None:
    """Load a full session with messages by session_id (or prefix)."""
    from datetime import datetime, timezone

    for root, parser in _configured_roots():
        for path in parser.list_sessions(root):
            # Fast path: check if session_id appears in filename
            basename = os.path.splitext(os.path.basename(path))[0]
            if not basename.startswith(session_id) and session_id not in basename:
                continue

            # Parse with tree-walking for claude_code, plain parse for others
            if parser.agent_slug == "claude_code":
                session = parser.parse_session_tree(
                    path,
                    all_branches=all_branches,
                    include_tools=include_tools,
                    roles=roles,
                )
            else:
                session = parser.parse_session(path)

            if not session:
                continue

            # Verify match: check both sessionId and filename (subagent files
            # have the parent's sessionId, so filename is the real identifier)
            sid_match = session.session_id.startswith(session_id) or session_id in session.session_id
            file_match = basename.startswith(session_id) or session_id in basename
            if not sid_match and not file_match:
                continue

            return _session_to_dict(session)

    # Slower fallback: scan metadata for sessionId match (e.g. Claude Code
    # files where filename != sessionId)
    for root, parser in _configured_roots():
        for path in parser.list_sessions(root):
            meta = parser.parse_metadata(path)
            if not meta:
                continue
            if not meta.session_id.startswith(session_id) and session_id not in meta.session_id:
                continue

            if parser.agent_slug == "claude_code":
                session = parser.parse_session_tree(
                    path,
                    all_branches=all_branches,
                    include_tools=include_tools,
                    roles=roles,
                )
            else:
                session = parser.parse_session(path)

            if session:
                return _session_to_dict(session)

    return None


def _session_to_dict(session: base.NormalizedSession) -> dict:
    """Convert NormalizedSession to a plain dict for the envelope."""
    started_iso = None
    if session.started_at:
        started_iso = datetime.fromtimestamp(
            session.started_at / 1000, tz=timezone.utc,
        ).isoformat()
    ended_iso = None
    if session.ended_at:
        ended_iso = datetime.fromtimestamp(
            session.ended_at / 1000, tz=timezone.utc,
        ).isoformat()

    messages = []
    for m in session.messages:
        messages.append({
            "idx": m.idx,
            "role": m.role,
            "content": m.content,
            "author": m.author,
            "created_at": m.created_at,
            "branch": m.branch,
            "uuid": m.uuid,
            "parent_uuid": m.parent_uuid,
        })

    return {
        "session_id": session.session_id,
        "agent": session.agent,
        "source_path": session.source_path,
        "workspace": session.workspace,
        "title": session.title,
        "started_at": started_iso,
        "ended_at": ended_iso,
        "model": session.model,
        "messages": messages,
    }


def _extract_snippet(content: str, query: str, context_chars: int = 120) -> str:
    lower = content.lower()
    query_lower = query.lower()
    idx = lower.find(query_lower)
    if idx == -1:
        return content[:context_chars * 2].strip()

    start = max(0, idx - context_chars)
    end = min(len(content), idx + len(query) + context_chars)
    snippet = content[start:end].strip()

    if start > 0:
        snippet = "..." + snippet
    if end < len(content):
        snippet = snippet + "..."
    return snippet
