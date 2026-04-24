from __future__ import annotations

from datetime import datetime, timezone

ACTIVE_DAYS = 7
STALE_DAYS = 30


def derive(last_active: str | None, archived: bool = False, blocked: bool = False) -> str:
    """Derive project state from last_active timestamp and annotation flags.

    States: active (≤7d), stale (7-30d), dormant (30d+), archived, blocked.
    """
    if archived:
        return "archived"
    if blocked:
        return "blocked"
    if not last_active:
        return "dormant"
    try:
        ts = datetime.fromisoformat(last_active)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - ts).days
    except (ValueError, TypeError):
        return "dormant"
    if days <= ACTIVE_DAYS:
        return "active"
    if days <= STALE_DAYS:
        return "stale"
    return "dormant"
