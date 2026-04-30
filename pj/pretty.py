from __future__ import annotations

"""Human-readable table rendering for --pretty output."""

import re
import sys
from datetime import datetime, timezone

_STATE_COLORS = {"active": "32", "stale": "33", "dormant": "31", "blocked": "1;31", "archived": "2"}
_PRI_COLORS = {"high": "31", "medium": "33", "low": "2"}


def _use_color() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    if not _use_color():
        return text
    return f"\033[{code}m{text}\033[0m"


def _color_state(s: str) -> str:
    return _c(_STATE_COLORS.get(s, "0"), s) if s in _STATE_COLORS else s


def _color_pri(p: str) -> str:
    return _c(_PRI_COLORS.get(p, "0"), p) if p in _PRI_COLORS else p


def _highlight(text: str, query: str) -> str:
    """Dim the text but bold+yellow the search term."""
    if not query or not _use_color():
        return _c("2", text)
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    # Split on matches, dim non-match parts, bold+yellow match parts
    parts = pattern.split(text)
    matches = pattern.findall(text)
    result = []
    for i, part in enumerate(parts):
        result.append(_c("2", part))
        if i < len(matches):
            result.append(_c("1;33", matches[i]))
    return "".join(result)


def _color_score(v: float) -> str:
    code = "32" if v >= 0.7 else "33" if v >= 0.4 else "31"
    return _c(code, f"{v:.2f}")


def _pad(text: str, width: int) -> str:
    """Pad text to width, accounting for ANSI escape sequences."""
    visible_len = len(re.sub(r"\033\[[0-9;]*m", "", text))
    return text + " " * max(0, width - visible_len)


def _compact_tokens(n: int | None) -> str:
    """Format token count compactly: 1234 → 1.2k, 1234567 → 1.2M."""
    if n is None or n == 0:
        return ""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def _format_tokens(s: dict) -> str:
    """Compact token summary for a session."""
    total = s.get("total_tokens")
    if not total:
        return ""
    out = _compact_tokens(s.get("output_tokens"))
    cache = _compact_tokens(s.get("cache_read_tokens"))
    parts = [f"{_compact_tokens(total)} tok"]
    if out:
        parts.append(f"{out} out")
    if cache:
        parts.append(f"{cache} cache")
    return " ".join(parts)


def _format_activity(s: dict) -> str:
    """Compact activity summary: turns and tool calls."""
    user = s.get("user_messages")
    tools = s.get("tool_calls")
    parts = []
    if user:
        parts.append(f"{user} turns")
    if tools:
        parts.append(f"{tools} tools")
    return " ".join(parts) if parts else ""


def _format_duration(secs: float | None) -> str:
    """Format seconds into a compact duration string."""
    if secs is None or secs < 0:
        return ""
    if secs < 60:
        return f"{int(secs)}s"
    mins = secs / 60
    if mins < 60:
        return f"{int(mins)}m"
    hrs = mins / 60
    if hrs < 24:
        m = int(mins % 60)
        return f"{int(hrs)}h{m:02d}m" if m else f"{int(hrs)}h"
    days = hrs / 24
    return f"{int(days)}d"


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


def print_chat(data: dict) -> None:
    """Render a full session conversation as markdown-style output."""
    title = data.get("title") or "(untitled)"
    print(_c("1", f"# {title}"))
    parts = [f"Session: {data.get('session_id', '?')}"]
    agent = data.get("agent")
    if agent:
        parts.append(f"Agent: {agent}")
    model = data.get("model")
    if model:
        parts.append(f"Model: {_c('36', model)}")
    started = data.get("started_at")
    if started:
        parts.append(f"Started: {_relative_time(started)}")
    print("  ".join(parts))
    print()

    messages = data.get("messages", [])
    for msg in messages:
        role = msg.get("role", "?")
        branch = msg.get("branch")

        # Abandoned branches get dimmed with a marker
        if branch == "abandoned":
            print(_c("2;33", f"  ┆ [{role}] (abandoned branch)"))
            content = msg.get("content", "")
            for line in content.split("\n")[:5]:
                print(_c("2", f"  ┆ {line}"))
            if content.count("\n") > 5:
                print(_c("2", f"  ┆ ...({content.count(chr(10)) - 5} more lines)"))
            print()
            continue

        # Role header
        if role == "user":
            header = _c("1;34", "## User")
        elif role == "assistant":
            author = msg.get("author")
            author_str = f" ({_c('36', author)})" if author else ""
            header = _c("1;32", "## Assistant") + author_str
        else:
            header = _c("2", f"## {role}")

        # Timestamp
        ts = msg.get("created_at")
        if ts:
            dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            header += f"  {_c('2', dt.strftime('%H:%M:%S'))}"

        print(header)
        print(msg.get("content", ""))
        print()

    print(_c("2", f"--- {len(messages)} messages ---"))


def print_status(data: dict) -> None:
    print(f"Project: {_c('1', data.get('name', '?'))}")
    print(f"  Path:     {data.get('path', '?')}")
    print(f"  ID:       {data.get('id', '?')}")
    print(f"  State:    {_color_state(data.get('state', '?'))}")
    print(f"  Priority: {_color_pri(data.get('priority', 'none'))}")
    print(f"  Agents:   {', '.join(data.get('agents', []))}")
    print(f"  Sessions: {data.get('session_count', 0)}")
    print(f"  Active:   {_relative_time(data.get('last_active'))}")

    tags = data.get("tags", [])
    if tags:
        print(f"  Tags:     {', '.join(tags)}")

    note = data.get("latest_note")
    if note:
        print(f"  Note:     {note}")

    sessions = data.get("sessions", [])
    if sessions:
        print(f"\nRecent sessions ({len(sessions)}):")
        for s in sessions:
            agent = s.get("agent", "?")
            title = s.get("title") or "(untitled)"
            when = _relative_time(s.get("started_at"))
            sid = str(s.get("session_id", ""))[:12]
            model = s.get("model") or ""
            model_tag = f" {_c('36', model)}" if model else ""
            dur = _format_duration(s.get("duration_secs"))
            dur_tag = f" {_c('33', dur)}" if dur else ""
            print(f"  [{agent}] {title}  ({when}, {sid}){model_tag}{dur_tag}")
            # Show harness version and per-message models if available
            versions = s.get("versions", [])
            models = s.get("models", [])
            extras = []
            if versions:
                extras.append(f"v{','.join(versions)}")
            if models:
                # Only show if different from primary_model
                extra_models = [m for m in models if m != model]
                if extra_models:
                    extras.append(f"also: {', '.join(extra_models)}")
            # Token usage and activity stats
            tokens = _format_tokens(s)
            if tokens:
                extras.append(tokens)
            activity = _format_activity(s)
            if activity:
                extras.append(activity)
            if extras:
                print(f"    {_c('2', ' | '.join(extras))}")

    resume_cmd = data.get("resume_cmd")
    if resume_cmd:
        print(f"\nResume:\n  {resume_cmd}")


def _format_hours(secs: float | None) -> str:
    """Format total seconds as compact hours string."""
    if not secs or secs <= 0:
        return ""
    hrs = secs / 3600
    if hrs < 0.1:
        return f"{secs / 60:.0f}m"
    if hrs < 10:
        return f"{hrs:.1f}h"
    return f"{hrs:.0f}h"


def _short_models(models: list[str]) -> str:
    """Shorten model names: claude-opus-4-6 → opus, etc."""
    short = set()
    for m in models:
        if "opus" in m:
            short.add("opus")
        elif "sonnet" in m:
            short.add("sonnet")
        elif "haiku" in m:
            short.add("haiku")
        else:
            short.add(m.split("-")[1] if "-" in m else m[:8])
    return ",".join(sorted(short))


def print_projects(projects: list[dict], total: int, offset: int, limit: int) -> None:
    if not projects:
        print("No projects found.")
        return

    has_detail = any("first_active" in p for p in projects)

    cols = [
        ("ID", 8),
        ("STATE", 8),
        ("NAME", 28),
        ("AGENTS", 16),
        ("SESS", 4),
        ("PRI", 6),
        ("LAST ACTIVE", 11),
    ]
    if has_detail:
        cols.extend([
            ("HOURS", 6),
            ("MODELS", 16),
            ("STARTED", 11),
        ])

    header = "  ".join(_c("1", h.ljust(w)) for h, w in cols)
    print(header)
    sep_len = sum(w for _, w in cols) + 2 * (len(cols) - 1)
    print("-" * sep_len)

    for p in projects:
        agents = ",".join(p.get("agents", []))
        state_str = p.get("state", "")
        pri_str = p.get("priority", "none")
        row = [
            _pad(p.get("id", "")[:8], 8),
            _pad(_color_state(state_str), 8),
            _pad(_c("1", p.get("name", "")[:28]), 28),
            _pad(agents[:16], 16),
            str(p.get("session_count", 0)).rjust(4),
            _pad(_color_pri(pri_str[:6]), 6),
            _relative_time(p.get("last_active")).ljust(11),
        ]
        if has_detail:
            row.extend([
                _format_hours(p.get("total_duration_secs")).rjust(6),
                _pad(_short_models(p.get("models", [])), 16),
                _relative_time(p.get("first_active")).ljust(11),
            ])
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

    header = "  ".join(_c("1", h.ljust(w)) for h, w in cols)
    sep_len = sum(w for _, w in cols) + 2 * (len(cols) - 1)
    print(header)
    print("-" * sep_len)

    for i, p in enumerate(scored, 1):
        score = p.get("score", 0)
        row = [
            str(i).rjust(3),
            _pad(_color_score(score), 5),
            _pad(_c("1", p.get("name", "")[:28]), 28),
            _pad(_color_state(p.get("state", "")), 8),
            _pad(_color_pri(p.get("priority", "none")[:6]), 6),
            p.get("reason", "")[:40].ljust(40),
        ]
        print("  ".join(row))


def print_search(results: list[dict], query: str) -> None:
    from . import resume as resume_mod

    if not results:
        print(f'No results for "{query}".')
        return

    print(f'Search: "{query}" — {len(results)} result(s)\n')

    cols = [
        ("NAME", 28),
        ("STATE", 8),
        ("MATCH", 30),
    ]

    header = "  ".join(_c("1", h.ljust(w)) for h, w in cols)
    sep_len = sum(w for _, w in cols) + 2 * (len(cols) - 1)
    print(header)
    print("-" * sep_len)

    for p in results:
        match = ", ".join(p.get("match_fields", []))
        row = [
            _pad(_c("1", p.get("name", "")[:28]), 28),
            _pad(_color_state(p.get("state", "")[:8]), 8),
            match[:30].ljust(30),
        ]
        print("  ".join(row))

        # Show matching sessions with resume commands
        sessions = p.get("matching_sessions", [])
        path = p.get("path", "")
        if sessions:
            for s in sessions[:3]:
                title = (s.get("title") or "(untitled)").replace("\n", " ")[:80]
                when = _relative_time(s.get("started_at"))
                sid = str(s.get("session_id", ""))[:12]
                agent = s.get("agent", "")
                cmd = resume_mod.full_resume_command(path, agent, s.get("session_id", ""))
                print(f"    {_highlight(title, query)}  {_c('2', f'({when}, {sid})')}")
                print(f"      {_c('36', cmd)}")
        else:
            # Fallback: show snippets/titles without session info
            snippets = p.get("snippets", [])
            if snippets:
                for snip in snippets[:2]:
                    display = snip.replace("\n", " ")[:200]
                    print(f"    {_highlight(display, query)}")
            titles = p.get("matching_titles", [])
            if titles:
                for t in titles[:2]:
                    display = t.replace("\n", " ")[:200] if t else ""
                    print(f"    {_highlight(display, query)}")
