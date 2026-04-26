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
    env = os.environ.get(_ENV_KEY)
    if env:
        p = os.path.join(env, "projects")
        if os.path.isdir(p):
            roots.append(p)
    default = _DEFAULT_ROOT
    if os.path.isdir(default) and default not in roots:
        roots.append(default)
    return roots


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
            for fname in os.listdir(project_path):
                if fname.endswith(".jsonl"):
                    paths.append(os.path.join(project_path, fname))
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
    project_dir = os.path.basename(os.path.dirname(path))
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

    project_dir = os.path.basename(os.path.dirname(path))
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
