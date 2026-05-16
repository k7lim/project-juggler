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


def _flatten_content_filtered(content, *, include_tools: bool = True) -> str:
    """Flatten message content, optionally stripping tool blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") in ("tool_use", "tool_result"):
                    if include_tools:
                        if block.get("type") == "tool_use":
                            parts.append(f"[Tool: {block.get('name', '?')}]")
                        else:
                            parts.append("[Tool Result]")
                    # else: skip tool blocks entirely
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content) if content else ""


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
            head_lines = [next(f, "").strip() for _ in range(20)]
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

    project_dir = _project_dir_from_path(path)
    workspace = _decode_dir_name(project_dir)
    session_id = None
    model = None
    title = None
    timestamps = []

    for line in head_lines:  # scan head for metadata
        if not line:
            continue
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
        messages=[],  # metadata only
    )


def parse_session_tree(
    path: str,
    *,
    all_branches: bool = False,
    include_tools: bool = True,
    roles: set[str] | None = None,
) -> NormalizedSession | None:
    """Parse a session with tree-aware fork handling.

    Walks the uuid/parentUuid DAG to find the active branch (last child at
    each fork).  Returns only active-branch messages by default;
    ``all_branches=True`` includes abandoned forks annotated with
    ``branch="abandoned"``.
    """
    try:
        with open(path) as f:
            lines = [l.strip() for l in f if l.strip()]
    except OSError:
        return None

    if not lines:
        return None

    project_dir = _project_dir_from_path(path)
    workspace = _decode_dir_name(project_dir)

    # First pass: parse all lines into node dicts keyed by uuid
    nodes: dict[str, dict] = {}  # uuid -> node info
    children: dict[str, list[str]] = {}  # parent_uuid -> [child uuids]
    ordered_uuids: list[str] = []  # file order
    session_id = None
    model = None
    root_uuid = None

    for line_idx, line in enumerate(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        uuid = obj.get("uuid")
        if not uuid:
            continue

        if not session_id:
            session_id = obj.get("sessionId")
        cwd = obj.get("cwd")
        if cwd:
            workspace = cwd

        parent = obj.get("parentUuid")

        msg_type = obj.get("type", "")
        msg = obj.get("message", {})
        if not isinstance(msg, dict):
            msg = {}

        role = msg.get("role", msg_type)
        msg_model = msg.get("model")
        if msg_model:
            model = msg_model

        nodes[uuid] = {
            "uuid": uuid,
            "parent_uuid": parent,
            "line_idx": line_idx,
            "type": msg_type,
            "role": role,
            "content_raw": msg.get("content", ""),
            "author": msg_model,
            "timestamp": obj.get("timestamp"),
        }
        ordered_uuids.append(uuid)

        if parent:
            children.setdefault(parent, []).append(uuid)
        else:
            if root_uuid is None:
                root_uuid = uuid

    if not nodes:
        return None

    # Find root (first node without parent)
    if root_uuid is None:
        root_uuid = ordered_uuids[0]

    # Walk tree to find active tip: at each node, pick the last child (by file order)
    active_tip = root_uuid
    while active_tip in children:
        kids = children[active_tip]
        active_tip = kids[-1]  # last child = most recently written

    # Walk backward from tip to root to build the active set
    active_uuids: set[str] = set()
    cursor = active_tip
    while cursor:
        active_uuids.add(cursor)
        node = nodes.get(cursor)
        cursor = node["parent_uuid"] if node else None

    # Build messages
    messages = []
    timestamps = []
    title = None
    idx = 0

    for uuid in ordered_uuids:
        node = nodes[uuid]
        is_active = uuid in active_uuids

        if not all_branches and not is_active:
            continue

        msg_type = node["type"]
        if msg_type not in ("user", "assistant"):
            continue

        role = node["role"]
        if roles and role not in roles:
            continue

        content = _flatten_content_filtered(
            node["content_raw"], include_tools=include_tools,
        )
        if not content.strip():
            continue

        ts = _parse_timestamp(node["timestamp"])
        if ts:
            timestamps.append(ts)

        if title is None and role == "user":
            first_line = content.split("\n", 1)[0].strip()
            if first_line:
                title = first_line[:100]

        messages.append(NormalizedMessage(
            idx=idx,
            role=role,
            content=content,
            author=node["author"],
            created_at=ts,
            branch="active" if is_active else "abandoned",
            uuid=uuid,
            parent_uuid=node["parent_uuid"],
        ))
        idx += 1

    if not messages:
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
        messages=messages,
    )
