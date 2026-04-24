from __future__ import annotations

"""Human-readable table rendering for --pretty output."""

from datetime import datetime, timezone


def _relative_time(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        ts = datetime.fromisoformat(iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
        days = delta.days
        if days == 0:
            hours = delta.seconds // 3600
            if hours == 0:
                return f"{delta.seconds // 60}m ago"
            return f"{hours}h ago"
        if days == 1:
            return "yesterday"
        if days < 30:
            return f"{days}d ago"
        return f"{days // 30}mo ago"
    except (ValueError, TypeError):
        return "unknown"


def print_projects(projects: list[dict], total: int, offset: int, limit: int) -> None:
    if not projects:
        print("No projects found.")
        return

    cols = [
        ("ID", 8),
        ("STATE", 8),
        ("NAME", 28),
        ("AGENTS", 16),
        ("SESS", 4),
        ("PRI", 6),
        ("LAST ACTIVE", 11),
    ]

    header = "  ".join(h.ljust(w) for h, w in cols)
    print(header)
    print("-" * len(header))

    for p in projects:
        agents = ",".join(p.get("agents", []))
        row = [
            p.get("id", "")[:8].ljust(8),
            p.get("state", "").ljust(8),
            p.get("name", "")[:28].ljust(28),
            agents[:16].ljust(16),
            str(p.get("session_count", 0)).rjust(4),
            p.get("priority", "none")[:6].ljust(6),
            _relative_time(p.get("last_active")).ljust(11),
        ]
        print("  ".join(row))

    end = min(offset + len(projects), total)
    print(f"\n{offset + 1}-{end} of {total}")


def print_next(scored: list[dict]) -> None:
    if not scored:
        print("No actionable projects.")
        return

    cols = [
        ("#", 3),
        ("SCORE", 5),
        ("NAME", 28),
        ("STATE", 8),
        ("PRI", 6),
        ("REASON", 40),
    ]

    header = "  ".join(h.ljust(w) for h, w in cols)
    print(header)
    print("-" * len(header))

    for i, p in enumerate(scored, 1):
        row = [
            str(i).rjust(3),
            f"{p.get('score', 0):.2f}".rjust(5),
            p.get("name", "")[:28].ljust(28),
            p.get("state", "").ljust(8),
            p.get("priority", "none")[:6].ljust(6),
            p.get("reason", "")[:40].ljust(40),
        ]
        print("  ".join(row))


def print_search(results: list[dict], query: str) -> None:
    if not results:
        print(f'No results for "{query}".')
        return

    print(f'Search: "{query}" — {len(results)} result(s)\n')

    cols = [
        ("NAME", 28),
        ("STATE", 8),
        ("MATCH", 30),
    ]

    header = "  ".join(h.ljust(w) for h, w in cols)
    print(header)
    print("-" * len(header))

    for p in results:
        match = ", ".join(p.get("match_fields", []))
        row = [
            p.get("name", "")[:28].ljust(28),
            p.get("state", "")[:8].ljust(8),
            match[:30].ljust(30),
        ]
        print("  ".join(row))
