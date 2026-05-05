"""Parser for Kimi CLI sessions (~/.kimi/sessions/<workspace_hash>/<session_id>/).

Layout per session:
    state.json     — high-level metadata (custom_title, archived flag, todos, ...)
    wire.jsonl     — protocol stream (TurnBegin, ContentPart, StatusUpdate, ...)
    context.jsonl  — flat role+content view used internally by kimi

The workspace path is not stored inside any session file; it lives in the
sibling kimi.json metadata at the parent of `sessions/`. Each work_dir entry
maps a path to an md5-hashed dir name (or `<kaos>_<md5>` for non-local kaos).
We build that map once per root and use it to resolve workspaces.
"""
from __future__ import annotations

import glob
import json
import os
from hashlib import md5
from pathlib import Path

from .base import NormalizedMessage, NormalizedSession

agent_slug = "kimi"

_DEFAULT_ROOT = os.path.join(Path.home(), ".kimi", "sessions")


def detect_roots() -> list[str]:
    roots: list[str] = []
    seen: set[str] = set()

    def _add(p: str) -> None:
        real = os.path.realpath(p)
        if real not in seen and os.path.isdir(real):
            seen.add(real)
            roots.append(real)

    _add(_DEFAULT_ROOT)
    # Auto-discover ~/.kimi-*/sessions (e.g. .kimi-yolobox)
    for d in glob.glob(os.path.join(Path.home(), ".kimi-*", "sessions")):
        _add(d)

    return roots


def _load_workspace_map(sessions_root: str) -> dict[str, str]:
    """Build {dir_basename → workspace_path} from the sibling kimi.json.

    For local-kaos work_dirs, dir_basename is md5(path). For non-local kaos,
    the basename is `<kaos>_<md5>`. We mirror both forms so lookups work.
    """
    parent = os.path.dirname(sessions_root)
    meta_path = os.path.join(parent, "kimi.json")
    mapping: dict[str, str] = {}
    try:
        with open(meta_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return mapping
    for wd in data.get("work_dirs", []) or []:
        path = wd.get("path")
        kaos = wd.get("kaos", "local")
        if not path:
            continue
        path_md5 = md5(path.encode("utf-8")).hexdigest()
        mapping[path_md5] = path
        # Non-local kaos uses prefixed basename in the filesystem.
        mapping[f"{kaos}_{path_md5}"] = path
    return mapping


def list_sessions(root: str) -> list[str]:
    """Return absolute paths to per-session directories under root.

    A session is the inner UUID directory containing state.json + wire.jsonl.
    We yield directory paths (not files) — parse_* below expect a session dir.
    """
    paths: list[str] = []
    try:
        for ws_hash in os.listdir(root):
            ws_dir = os.path.join(root, ws_hash)
            if not os.path.isdir(ws_dir):
                continue
            for sid in os.listdir(ws_dir):
                session_dir = os.path.join(ws_dir, sid)
                if not os.path.isdir(session_dir):
                    continue
                if os.path.exists(os.path.join(session_dir, "state.json")):
                    paths.append(session_dir)
    except OSError:
        pass
    return paths


def _read_state(session_dir: str) -> dict:
    try:
        with open(os.path.join(session_dir, "state.json")) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _wire_path(session_dir: str) -> str:
    return os.path.join(session_dir, "wire.jsonl")


def _ms(ts) -> int | None:
    """wire.jsonl uses float seconds-since-epoch. Convert to ms."""
    if ts is None:
        return None
    try:
        return int(float(ts) * 1000)
    except (TypeError, ValueError):
        return None


def _extract_user_text(payload: dict) -> str:
    parts: list[str] = []
    for item in payload.get("user_input", []) or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text", ""))
    return "\n".join(p for p in parts if p)


def _resolve_workspace(session_dir: str, ws_map: dict[str, str]) -> str | None:
    # session_dir = .../sessions/<ws_hash>/<session_id>
    ws_hash = os.path.basename(os.path.dirname(session_dir))
    return ws_map.get(ws_hash)


def parse_session(path: str) -> NormalizedSession | None:
    """`path` is a session directory."""
    if not os.path.isdir(path):
        return None
    state = _read_state(path)
    if not state:
        return None

    sessions_root = os.path.dirname(os.path.dirname(path))
    ws_map = _load_workspace_map(sessions_root)
    workspace = _resolve_workspace(path, ws_map)
    session_id = os.path.basename(path)

    messages: list[NormalizedMessage] = []
    timestamps: list[int] = []
    pending_assistant: list[str] = []
    pending_assistant_ts: int | None = None
    idx = 0

    def _flush_assistant():
        nonlocal idx, pending_assistant, pending_assistant_ts
        if pending_assistant:
            text = "\n".join(pending_assistant).strip()
            if text:
                messages.append(NormalizedMessage(
                    idx=idx, role="assistant", content=text,
                    author=None, created_at=pending_assistant_ts,
                ))
                idx += 1
            pending_assistant = []
            pending_assistant_ts = None

    try:
        with open(_wire_path(path)) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts_ms = _ms(obj.get("timestamp"))
                if ts_ms:
                    timestamps.append(ts_ms)

                msg = obj.get("message") or {}
                mtype = msg.get("type")
                payload = msg.get("payload") or {}

                if mtype == "TurnBegin":
                    _flush_assistant()
                    text = _extract_user_text(payload)
                    if text.strip():
                        messages.append(NormalizedMessage(
                            idx=idx, role="user", content=text,
                            author=None, created_at=ts_ms,
                        ))
                        idx += 1
                elif mtype == "ContentPart":
                    # payload.type ∈ {"text", "think", "tool_use", "tool_result", ...}
                    ptype = payload.get("type")
                    if ptype == "text":
                        text = payload.get("text", "")
                        if text:
                            pending_assistant.append(text)
                            if pending_assistant_ts is None:
                                pending_assistant_ts = ts_ms
                    elif ptype == "tool_use":
                        name = payload.get("name") or payload.get("tool") or "?"
                        pending_assistant.append(f"[Tool: {name}]")
                    elif ptype == "tool_result":
                        pending_assistant.append("[Tool Result]")
                    # think parts are model thinking; skip from message stream
                elif mtype == "TurnEnd":
                    _flush_assistant()
    except OSError:
        return None

    _flush_assistant()

    if not messages and not timestamps:
        return None

    title = state.get("custom_title") or None
    if not title:
        for m in messages:
            if m.role == "user":
                first = m.content.split("\n", 1)[0].strip()
                title = first[:100] if first else None
                break

    return NormalizedSession(
        session_id=session_id,
        agent=agent_slug,
        source_path=path,
        workspace=workspace,
        title=title,
        started_at=min(timestamps) if timestamps else None,
        ended_at=max(timestamps) if timestamps else None,
        model=None,
        messages=messages,
    )


def parse_metadata(path: str) -> NormalizedSession | None:
    """Fast metadata-only parse — title from state.json, timestamps from wire head/tail."""
    if not os.path.isdir(path):
        return None
    state = _read_state(path)
    if not state:
        return None

    sessions_root = os.path.dirname(os.path.dirname(path))
    ws_map = _load_workspace_map(sessions_root)
    workspace = _resolve_workspace(path, ws_map)
    session_id = os.path.basename(path)

    title = state.get("custom_title") or None
    timestamps: list[int] = []

    wire = _wire_path(path)
    head_lines: list[str] = []
    tail_lines: list[str] = []
    try:
        with open(wire) as f:
            head_lines = [next(f, "").strip() for _ in range(20)]
        # Cheap tail: read last ~4KB.
        with open(wire, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail_lines = f.read().decode("utf-8", errors="replace").splitlines()[-5:]
    except OSError:
        return None

    for line in head_lines + tail_lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _ms(obj.get("timestamp"))
        if ts:
            timestamps.append(ts)
        if not title:
            msg = obj.get("message") or {}
            if msg.get("type") == "TurnBegin":
                t = _extract_user_text(msg.get("payload") or {})
                first = t.split("\n", 1)[0].strip()
                if first:
                    title = first[:100]

    return NormalizedSession(
        session_id=session_id,
        agent=agent_slug,
        source_path=path,
        workspace=workspace,
        title=title,
        started_at=min(timestamps) if timestamps else None,
        ended_at=max(timestamps) if timestamps else None,
        model=None,
        messages=[],
    )
