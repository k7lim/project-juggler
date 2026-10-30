"""Pi JSONL history, shared by native pi/pi-raw and sandbox launchers.

Search preserves the append-only history, including earlier branches and
messages retained on disk after compaction. Thinking and binary blocks are
omitted; extension state is not conversation content.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from .base import NormalizedMessage, NormalizedSession

agent_slug = "pi"


def detect_roots() -> list[str]:
    candidates = [Path.home() / ".pi/agent/sessions"]
    if directory := os.environ.get("PI_CODING_AGENT_DIR"):
        candidates.append(Path(directory).expanduser() / "sessions")
    if directory := os.environ.get("PI_CODING_AGENT_SESSION_DIR"):
        candidates.append(Path(directory).expanduser())
    return list(dict.fromkeys(str(p.resolve()) for p in candidates if p.is_dir()))


def list_sessions(root: str) -> list[str]:
    return sorted(str(p) for p in Path(root).rglob("*.jsonl") if p.is_file())


def _ms(value) -> int | None:
    try:
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif block.get("type") == "toolCall":
            parts.append(f"[Tool: {block.get('name', '?')}]")
    return "\n".join(parts)


def _parse(path: str, *, metadata_only: bool) -> NormalizedSession | None:
    session = None
    first_prompt = None
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue  # Includes a partially written final entry.
                if not isinstance(entry, dict):
                    continue
                kind = entry.get("type")
                ts = _ms(entry.get("timestamp"))
                if kind == "session" and session is None:
                    if not isinstance(entry.get("id"), str) or not entry["id"]:
                        return None
                    session = NormalizedSession(
                        session_id=entry["id"], agent=agent_slug, source_path=path,
                        workspace=entry.get("cwd"), started_at=ts, ended_at=ts,
                    )
                    continue
                if session is None:
                    continue
                if ts is not None:
                    session.started_at = min(session.started_at, ts) if session.started_at is not None else ts
                    session.ended_at = max(session.ended_at, ts) if session.ended_at is not None else ts
                if kind == "session_info":
                    session.title = entry.get("name") or None
                elif kind == "model_change":
                    session.model = entry.get("modelId") or session.model
                elif kind == "message":
                    msg = entry.get("message")
                    if not isinstance(msg, dict):
                        continue
                    role = msg.get("role")
                    role = {"toolResult": "tool", "custom": "system", "hookMessage": "system"}.get(role, role)
                    if role not in ("user", "assistant", "tool", "system"):
                        continue
                    if role == "assistant":
                        session.model = msg.get("model") or session.model
                    if metadata_only and (role != "user" or first_prompt):
                        continue
                    text = _text(msg.get("content"))
                    if not text.strip():
                        continue
                    if role == "user" and first_prompt is None:
                        first_prompt = text.strip().split("\n", 1)[0][:100]
                    if not metadata_only:
                        session.messages.append(NormalizedMessage(
                            idx=len(session.messages), role=role, content=text,
                            author=msg.get("model"), created_at=ts if ts is not None else _ms(msg.get("timestamp")),
                            uuid=entry.get("id"), parent_uuid=entry.get("parentId"),
                        ))
    except OSError:
        return None
    if session is not None:
        session.title = session.title or first_prompt
    return session


def parse_session(path: str) -> NormalizedSession | None:
    return _parse(path, metadata_only=False)


def parse_metadata(path: str) -> NormalizedSession | None:
    # Stream the full history to honor names/model changes appended at the end,
    # without retaining messages in memory.
    return _parse(path, metadata_only=True)
