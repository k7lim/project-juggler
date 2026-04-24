from __future__ import annotations

"""Scheduling heuristic for 'pj next' — scores projects by urgency and actionability."""

from datetime import datetime, timezone

from . import cass_facade

PRIORITY_SCORES = {"high": 1.0, "medium": 0.6, "low": 0.2, "none": 0.4}

W_PRIORITY = 0.35
W_RECENCY = 0.25
W_MOMENTUM = 0.20
W_STALENESS = 0.10
W_ACTIONABLE = 0.10


def _days_since(iso_ts: str | None) -> float:
    if not iso_ts:
        return 999.0
    try:
        dt = datetime.fromisoformat(iso_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
    except (ValueError, TypeError):
        return 999.0


def _priority_score(priority: str) -> float:
    return PRIORITY_SCORES.get(priority, 0.4)


def _recency_score(days: float) -> float:
    return 1.0 / (1.0 + days * 0.3)


def _staleness_score(days: float) -> float:
    return 0.8 if 3.0 <= days <= 7.0 else 0.0


def _actionable_note(project: dict) -> float:
    note = project.get("latest_note")
    if not note:
        return 0.0
    if note.lower().startswith("blocked:"):
        return 0.0
    return 1.0


def _reason(factors: dict, project: dict) -> str:
    parts = []
    pri = project.get("priority", "none")
    if pri in ("high", "medium"):
        parts.append(f"{pri} priority")
    if factors["staleness"] > 0:
        parts.append("needs attention soon")
    if factors["momentum"] > 0.5:
        parts.append("recent momentum")
    if factors["actionable"] > 0:
        parts.append("has next step")
    if factors["recency"] > 0.7:
        parts.append("recently active")
    return "; ".join(parts) if parts else "baseline score"


def score_projects(
    projects: list[dict],
    recent_counts: dict[str, int] | None = None,
) -> list[dict]:
    """Score and rank projects for 'next' recommendation.

    Excludes archived and blocked projects. Returns projects sorted by score descending.
    """
    if recent_counts is None:
        recent_counts = cass_facade.recent_session_counts(days=7)
    max_recent = max(recent_counts.values(), default=1) or 1

    scored = []
    for p in projects:
        if p.get("state") in ("archived", "blocked"):
            continue

        days = _days_since(p.get("last_active"))

        f_priority = _priority_score(p.get("priority", "none"))
        f_recency = _recency_score(days)
        f_momentum = recent_counts.get(p.get("path", ""), 0) / max_recent
        f_staleness = _staleness_score(days)
        f_actionable = _actionable_note(p)

        total = (
            W_PRIORITY * f_priority
            + W_RECENCY * f_recency
            + W_MOMENTUM * f_momentum
            + W_STALENESS * f_staleness
            + W_ACTIONABLE * f_actionable
        )

        factors = {
            "priority": round(f_priority, 3),
            "recency": round(f_recency, 3),
            "momentum": round(f_momentum, 3),
            "staleness": round(f_staleness, 3),
            "actionable": round(f_actionable, 3),
        }

        scored.append({
            **p,
            "score": round(total, 3),
            "factors": factors,
            "reason": _reason(factors, p),
        })

    scored.sort(key=lambda p: p["score"], reverse=True)
    return scored
