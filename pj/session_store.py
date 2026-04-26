"""Abstract session store interface.

Defines the contract between pj and whatever provides session data.
Implementations live in separate files (cass_facade.py, fs_store.py, etc.).
pj consumers import from here — never from a concrete backend directly.

To swap backends: change which implementation `get_store()` returns.
"""

from __future__ import annotations

from typing import Protocol


class SessionStore(Protocol):
    """Read-only session data provider.

    Every method returns plain dicts/lists — no backend types leak.
    All methods must be safe to call when the backend is unavailable
    (return empty results, not raise).
    """

    def available(self) -> bool:
        """Whether this backend has any data to offer."""
        ...

    def list_projects(self) -> list[dict]:
        """All projects with agent, session count, and last active.

        Returns list of:
            {"path": str, "agents": [str], "session_count": int, "last_active": str|None}
        """
        ...

    def recent_session_counts(self, days: int = 7) -> dict[str, int]:
        """Session count per workspace path in the last N days.

        Returns {"path": count, ...}
        """
        ...

    def project_sessions(self, workspace_path: str, limit: int = 50) -> list[dict]:
        """Sessions for a workspace, most recent first.

        Returns list of:
            {"session_id": str|int, "agent": str, "title": str|None,
             "started_at": str|None, "model": str|None,
             "duration_secs": float|None,
             "input_tokens": int|None, "output_tokens": int|None,
             "cache_read_tokens": int|None, "cache_creation_tokens": int|None,
             "total_tokens": int|None,
             "user_messages": int|None, "assistant_messages": int|None,
             "tool_calls": int|None, "api_calls": int|None}
        """
        ...

    def session_details(self, session_ids: list) -> dict:
        """Per-session metadata (versions, models).

        Returns {session_id: {"versions": [str], "models": [str]}}
        """
        ...

    def search_sessions(self, query: str, limit: int = 20) -> list[dict]:
        """Search session titles.

        Returns list of:
            {"session_id": str|int, "path": str, "agent": str,
             "title": str|None, "started_at": str|None}
        """
        ...

    def search_content(self, query: str, limit: int = 20) -> list[dict]:
        """Full-text search across message content.

        Returns list of:
            {"path": str, "session_id": str|int, "agent": str,
             "snippet": str, "started_at": str|None, "title": str|None,
             "match_type": str}
        """
        ...


# --- Store registry ---

_store: SessionStore | None = None


def get_store() -> SessionStore:
    """Return the active session store. Defaults to fs_store.

    Set PJ_BACKEND=cass to use the CASS backend instead.
    """
    global _store
    if _store is None:
        import os
        if os.environ.get("PJ_BACKEND") == "cass":
            from . import cass_facade
            _store = cass_facade  # type: ignore[assignment]
        else:
            from . import fs_store
            _store = fs_store  # type: ignore[assignment]
    return _store


def set_store(store: SessionStore) -> None:
    """Swap the session store (for testing or backend migration)."""
    global _store
    _store = store
