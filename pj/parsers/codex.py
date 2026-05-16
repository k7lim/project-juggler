"""Parser for Codex sessions (~/.codex/sessions/)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .base import NormalizedMessage, NormalizedSession

agent_slug = "codex"

_ENV_KEY = "CODEX_HOME"
_DEFAULT_ROOT = os.path.join(Path.home(), ".codex", "sessions")


def detect_roots() -> list[str]:
    roots = []
    env = os.environ.get(_ENV_KEY)
    if env:
        p = os.path.join(env, "sessions")
        if os.path.isdir(p):
            roots.append(p)
    default = _DEFAULT_ROOT
    if os.path.isdir(default) and default not in roots:
        roots.append(default)
    return roots


def list_sessions(root: str) -> list[str]:
    """Find all rollout-*.jsonl files recursively."""
    paths = []
    for dirpath, _, filenames in os.walk(root):
        for fname in filenames:
            if fname.startswith("rollout-") and fname.endswith(".jsonl"):
                paths.append(os.path.join(dirpath, fname))
    return paths


def _flatten_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "input_text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "output_text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "text":
                    parts.append(block.get("text", ""))
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


def _external_id(path: str, root: str) -> str:
    """Session ID from relative path without extension."""
    rel = os.path.relpath(path, root)
    return os.path.splitext(rel)[0]


def parse_session(path: str) -> NormalizedSession | None:
    try:
        with open(path) as f:
            lines = [l.strip() for l in f if l.strip()]
    except OSError:
        return None

    if not lines:
        return None

    workspace = None
    session_id = None
    model = None
    messages = []
    timestamps = []
    idx = 0

    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = obj.get("type", "")
        ts = _parse_timestamp(obj.get("timestamp"))
        if ts:
            timestamps.append(ts)

        if event_type == "session_meta":
            payload = obj.get("payload", {})
            session_id = payload.get("id")
            workspace = payload.get("cwd")
            continue

        if event_type == "turn_context":
            payload = obj.get("payload", {})
            if payload.get("cwd"):
                workspace = payload["cwd"]
            if payload.get("model"):
                model = payload["model"]
            continue

        if event_type == "response_item":
            payload = obj.get("payload", {})
            role = payload.get("role", "")
            if role in ("developer", "system"):
                continue
            if role == "user":
                content = _flatten_content(payload.get("content", ""))
                if not content.strip():
                    continue
                messages.append(NormalizedMessage(
                    idx=idx, role="user", content=content,
                    created_at=ts,
                ))
                idx += 1
            elif role == "assistant":
                content = _flatten_content(payload.get("content", ""))
                if not content.strip():
                    continue
                messages.append(NormalizedMessage(
                    idx=idx, role="assistant", content=content,
                    author=model, created_at=ts,
                ))
                idx += 1

        if event_type == "event_msg":
            payload = obj.get("payload", {})
            msg_type = payload.get("type", "")
            if msg_type == "user_message":
                content = payload.get("message", "")
                if content.strip():
                    messages.append(NormalizedMessage(
                        idx=idx, role="user", content=content,
                        created_at=ts,
                    ))
                    idx += 1
            elif msg_type == "agent_reasoning":
                pass  # skip reasoning events

    if not messages:
        return None

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
    """Fast metadata-only parse from first lines."""
    try:
        with open(path) as f:
            head_lines = [next(f, "").strip() for _ in range(30)]
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail_lines = f.read().decode("utf-8", errors="replace").splitlines()[-5:]
    except OSError:
        return None

    lines = [line for line in head_lines + tail_lines if line]
    if not lines:
        return None

    workspace = None
    session_id = None
    model = None
    title = None
    timestamps = []

    for line in head_lines:
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = obj.get("type", "")
        ts = _parse_timestamp(obj.get("timestamp"))
        if ts:
            timestamps.append(ts)

        if event_type == "session_meta":
            payload = obj.get("payload", {})
            session_id = payload.get("id")
            workspace = payload.get("cwd")
        elif event_type == "turn_context":
            payload = obj.get("payload", {})
            if payload.get("cwd"):
                workspace = payload["cwd"]
            if payload.get("model"):
                model = payload["model"]
        elif event_type == "response_item" and not title:
            payload = obj.get("payload", {})
            if payload.get("role") == "user":
                content = _flatten_content(payload.get("content", ""))
                first_line = content.split("\n", 1)[0].strip()
                if first_line:
                    title = first_line[:100]

    # Tail for end timestamp
    for line in tail_lines:
        if not line:
            continue
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
        messages=[],
    )
