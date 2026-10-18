"""Opt-in SQLite session index backend (PJ_BACKEND=index).

Implements the SessionStore protocol (session_store.py). It must give the
same results as fs_store, and fs_store results whenever the index cannot
answer. See docs/plans/fs-index.md.

Phase 1: there is no index yet, so every method delegates to fs_store.
"""
from __future__ import annotations

from . import fs_store


def cache_signatures() -> dict[str, float]:
    """pj.cache uses this (via hasattr) to invalidate the discover cache."""
    return fs_store.cache_signatures()


# --- SessionStore interface ---

def available() -> bool:
    return fs_store.available()


def list_projects(detail: bool = False) -> list[dict]:
    return fs_store.list_projects(detail=detail)


def recent_session_counts(days: int = 7) -> dict[str, int]:
    return fs_store.recent_session_counts(days=days)


def project_sessions(workspace_path: str, limit: int = 50) -> list[dict]:
    return fs_store.project_sessions(workspace_path, limit=limit)


def sessions_by_project(workspace_paths: list[str]) -> dict[str, list[dict]]:
    return fs_store.sessions_by_project(workspace_paths)


def session_details(session_ids: list) -> dict:
    return fs_store.session_details(session_ids)


def search_sessions(query: str, limit: int = 20, sort: str = "newest") -> list[dict]:
    return fs_store.search_sessions(query, limit=limit, sort=sort)


def search_content(query: str, limit: int = 20, sort: str = "newest") -> list[dict]:
    return fs_store.search_content(query, limit=limit, sort=sort)


def get_session(
    session_id: str,
    *,
    all_branches: bool = False,
    include_tools: bool = True,
    roles: set[str] | None = None,
) -> dict | None:
    return fs_store.get_session(session_id, all_branches=all_branches,
                                include_tools=include_tools, roles=roles)


def load_session_file(source_path: str, agent: str) -> dict | None:
    return fs_store.load_session_file(source_path, agent)


def candidate_sessions(terms: list[str], match: str,
                       workspace_paths: list[str]) -> set[str] | None:
    return fs_store.candidate_sessions(terms, match, workspace_paths)
