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


def _message_text(obj) -> str | None:
    """The text parse_session keeps as a message for this event, else None."""
    event_type = obj.get("type", "")
    payload = obj.get("payload") or {}
    if event_type == "response_item" and payload.get("role") in ("user", "assistant"):
        content = _flatten_content(payload.get("content", ""))
    elif event_type == "event_msg" and payload.get("type") == "user_message":
        content = payload.get("message", "")
    else:
        return None
    return content if isinstance(content, str) and content.strip() else None


def _meta_timestamp(obj) -> int | None:
    """session_meta's payload.timestamp: when the session started.  The line
    timestamp can be later, when a resumed rollout rewrites the header."""
    return _parse_timestamp((obj.get("payload") or {}).get("timestamp"))


TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens",
              "reasoning_output_tokens", "total_tokens")
_TAIL_CHUNK = 64 * 1024
_TAIL_LIMIT = 1024 * 1024  # stop looking for a token_count this far from the end


def _total_token_usage(obj) -> dict | None:
    payload = obj.get("payload") or {}
    if obj.get("type") != "event_msg" or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    usage = info.get("total_token_usage") if isinstance(info, dict) else None
    if not isinstance(usage, dict):
        return None  # info is null on rate-limit-only counts
    return {k: usage[k] for k in TOKEN_KEYS if isinstance(usage.get(k), int)} or None


def _last_token_usage(f, size: int) -> dict | None:
    """total_token_usage of the last token_count with info: the session total.

    Codex logs a request's token_count after the tool output that request
    produced, so the last count can follow the last message, and a large tool
    output can follow the last count.  Reads backwards in chunks."""
    pos, buf = size, b""
    while pos > 0 and size - pos < _TAIL_LIMIT:
        step = min(_TAIL_CHUNK, pos)
        pos -= step
        f.seek(pos)
        buf = f.read(step) + buf
        lines = buf.split(b"\n")
        buf, complete = (lines[0], lines[1:]) if pos > 0 else (b"", lines)
        for line in reversed(complete):
            if b'"token_count"' not in line:
                continue
            try:
                usage = _total_token_usage(json.loads(line))
            except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                continue
            if usage:
                return usage
    return None


COUNT_KEYS = ("user_messages", "assistant_messages", "tool_calls", "api_calls")
_TOOL_CALL_TYPES = {"function_call", "custom_tool_call", "local_shell_call",
                    "web_search_call", "tool_search_call"}
# Codex sends these as role=user messages; they are context, not prompts.
_INJECTED_PREFIXES = ("<environment_context>", "<user_instructions>", "# AGENTS.md",
                      "<turn_aborted>", "<subagent_notification>", "<user_shell_command>")


def _counts(f) -> dict:
    """COUNT_KEYS over the whole rollout.  A request is a token_count with info;
    user_messages are typed prompts (response_item role=user, minus injected
    context).  Decodes only lines that can matter: about 5 ms per MB."""
    counts = dict.fromkeys(COUNT_KEYS, 0)
    for line in f:
        is_count = b'"token_count"' in line
        if not is_count and b'"response_item"' not in line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        payload = obj.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if obj.get("type") == "event_msg":
            if payload.get("type") == "token_count" and isinstance(payload.get("info"), dict):
                counts["api_calls"] += 1
        elif obj.get("type") == "response_item":
            kind = payload.get("type")
            if kind in _TOOL_CALL_TYPES:
                counts["tool_calls"] += 1
            elif kind == "message":
                text = _flatten_content(payload.get("content", "")).lstrip()
                if not text:
                    continue
                if payload.get("role") == "assistant":
                    counts["assistant_messages"] += 1
                elif payload.get("role") == "user" and not text.startswith(_INJECTED_PREFIXES):
                    counts["user_messages"] += 1
    return counts


def _has_message(f) -> bool:
    for line in f:
        if b'"response_item"' not in line and b'"user_message"' not in line:
            continue
        try:
            if _message_text(json.loads(line)) is not None:
                return True
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            continue
    return False


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
            started = _meta_timestamp(obj)
            if started:
                timestamps.append(started)
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
            whole_file_read = next(f, None) is None
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail_lines = f.read().decode("utf-8", errors="replace").splitlines()[-5:]
            tokens = _last_token_usage(f, size)
            f.seek(0)
            counts = _counts(f)
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
    has_message = False

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

        has_message = has_message or _message_text(obj) is not None
        if event_type == "session_meta":
            payload = obj.get("payload", {})
            session_id = payload.get("id")
            workspace = payload.get("cwd")
            started = _meta_timestamp(obj)
            if started:
                timestamps.append(started)
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
        has_message = has_message or _message_text(obj) is not None

    # parse_session returns None without messages; list exactly what opens.
    if not has_message and whole_file_read:
        return None
    if not has_message:
        try:
            with open(path, "rb") as f:
                if not _has_message(f):
                    return None
        except OSError:
            return None

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
        tokens=tokens,
        counts=counts,
    )
