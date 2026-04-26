"""Parser for Claude Code sessions (~/.claude/projects/)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .base import NormalizedMessage, NormalizedSession

agent_slug = "claude_code"

_ENV_KEY = "CLAUDE_CONFIG_DIR"
_DEFAULT_ROOT = os.path.join(Path.home(), ".claude", "projects")


def detect_roots() -> list[str]:
    roots = []
    seen = set()

    def _add(p: str) -> None:
        real = os.path.realpath(p)
        if real not in seen and os.path.isdir(real):
            seen.add(real)
            roots.append(real)

    # Explicit env var
    env = os.environ.get(_ENV_KEY)
    if env:
        _add(os.path.join(env, "projects"))

    # Default ~/.claude/projects
    _add(_DEFAULT_ROOT)

    # Auto-discover ~/.claude-*/projects (e.g. .claude-yolobox)
    import glob
    for d in glob.glob(os.path.join(Path.home(), ".claude-*", "projects")):
        _add(d)

    return roots


def _project_dir_from_path(path: str) -> str:
    """Extract project directory name from a session file path.

    The project dir is always the component immediately after "projects/" in the path,
    regardless of how deep the file is nested (subagents, session UUID dirs, etc.).
    """
    parts = Path(path).parts
    for i, part in enumerate(parts):
        if part == "projects" and i + 1 < len(parts):
            return parts[i + 1]
    # Fallback for paths without "projects" ancestor
    return os.path.basename(os.path.dirname(path))


def _decode_dir_name(dirname: str) -> str:
    """Decode Claude Code's encoded project directory name to a workspace path.

    The encoding replaces / with - and strips the leading /.
    Ambiguity: hyphens in real dir names are indistinguishable from separators.
    We reconstruct as best we can — /Users/kevin/... is the common prefix.
    """
    return "/" + dirname.lstrip("-").replace("-", "/")


def list_sessions(root: str) -> list[str]:
    paths = []
    try:
        for project_dir in os.listdir(root):
            project_path = os.path.join(root, project_dir)
            if not os.path.isdir(project_path):
                continue
            for entry in os.listdir(project_path):
                entry_path = os.path.join(project_path, entry)
                if entry.endswith(".jsonl"):
                    paths.append(entry_path)
                elif os.path.isdir(entry_path):
                    # Session UUID subdirs and subagents/ within them
                    for sub in os.listdir(entry_path):
                        sub_path = os.path.join(entry_path, sub)
                        if sub.endswith(".jsonl"):
                            paths.append(sub_path)
                        elif sub == "subagents" and os.path.isdir(sub_path):
                            for sa in os.listdir(sub_path):
                                if sa.endswith(".jsonl"):
                                    paths.append(os.path.join(sub_path, sa))
    except OSError:
        pass
    return paths


def _flatten_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    parts.append(f"[Tool: {block.get('name', '?')}]")
                elif block.get("type") == "tool_result":
                    parts.append(f"[Tool Result]")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content) if content else ""


def _parse_timestamp(ts) -> int | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return int(ts) if ts > 1e12 else int(ts * 1000)
    if isinstance(ts, str):
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except (ValueError, TypeError):
            return None
    return None


def parse_session(path: str) -> NormalizedSession | None:
    try:
        with open(path) as f:
            lines = [l.strip() for l in f if l.strip()]
    except OSError:
        return None

    if not lines:
        return None

    # Derive workspace from parent directory name
    project_dir = _project_dir_from_path(path)
    workspace = _decode_dir_name(project_dir)

    session_id = None
    messages = []
    model = None
    timestamps = []
    idx = 0

    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = obj.get("type", "")
        if not session_id:
            session_id = obj.get("sessionId")

        # Use cwd if available (more accurate than dir name)
        cwd = obj.get("cwd")
        if cwd:
            workspace = cwd

        if msg_type not in ("user", "assistant"):
            continue

        msg = obj.get("message", {})
        if not isinstance(msg, dict):
            continue

        role = msg.get("role", msg_type)
        content = _flatten_content(msg.get("content", ""))
        if not content.strip():
            continue

        msg_model = msg.get("model")
        if msg_model:
            model = msg_model

        ts = _parse_timestamp(obj.get("timestamp"))
        if ts:
            timestamps.append(ts)

        messages.append(NormalizedMessage(
            idx=idx, role=role, content=content,
            author=msg_model, created_at=ts,
        ))
        idx += 1

    if not messages:
        return None

    # Title from first user message
    title = None
    for m in messages:
        if m.role == "user":
            first_line = m.content.split("\n", 1)[0].strip()
            title = first_line[:100] if first_line else None
            break

    session_id = session_id or os.path.splitext(os.path.basename(path))[0]

    return NormalizedSession(
        session_id=session_id,
        agent=agent_slug,
        source_path=path,
        workspace=workspace,
        title=title,
        started_at=min(timestamps) if timestamps else None,
        ended_at=max(timestamps) if timestamps else None,
        model=model,
        messages=messages,
    )


def parse_metadata(path: str) -> NormalizedSession | None:
    """Fast metadata-only parse: read first+last few lines."""
    try:
        with open(path) as f:
            lines = [l.strip() for l in f if l.strip()]
    except OSError:
        return None

    if not lines:
        return None

    project_dir = _project_dir_from_path(path)
    workspace = _decode_dir_name(project_dir)
    session_id = None
    model = None
    title = None
    timestamps = []

    for line in lines[:20]:  # scan head for metadata
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not session_id:
            session_id = obj.get("sessionId")
        cwd = obj.get("cwd")
        if cwd:
            workspace = cwd
        ts = _parse_timestamp(obj.get("timestamp"))
        if ts:
            timestamps.append(ts)
        msg_type = obj.get("type", "")
        if msg_type == "assistant":
            msg = obj.get("message", {})
            if isinstance(msg, dict) and msg.get("model"):
                model = msg["model"]
        if msg_type == "user" and not title:
            msg = obj.get("message", {})
            if isinstance(msg, dict):
                content = _flatten_content(msg.get("content", ""))
                first_line = content.split("\n", 1)[0].strip()
                if first_line:
                    title = first_line[:100]

    # Scan tail for end timestamp
    for line in lines[-5:]:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _parse_timestamp(obj.get("timestamp"))
        if ts:
            timestamps.append(ts)

    session_id = session_id or os.path.splitext(os.path.basename(path))[0]

    return NormalizedSession(
        session_id=session_id,
        agent=agent_slug,
        source_path=path,
        workspace=workspace,
        title=title,
        started_at=min(timestamps) if timestamps else None,
        ended_at=max(timestamps) if timestamps else None,
        model=model,
        messages=[],  # metadata only
    )
