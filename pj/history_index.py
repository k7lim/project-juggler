"""Private host-owned, explicitly refreshed SQLite history snapshot.

The HTTP request path only queries a previously committed generation. Neither
indexing nor querying imports discovery or writes source histories.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
from urllib.parse import quote

from .envelope import ok
from .parsers import claude_code, codex

SCHEMA_VERSION = "2"
MAX_LINE = 16 * 1024 * 1024
MAX_INDEX_FILE = 1024 * 1024 * 1024
MAX_INDEX_FILES = 100000
MAX_INDEX_ENTRIES = 500000
QUERY_SECONDS = 2


class IndexError(Exception):
    def __init__(self, message="history index unavailable", status=503):
        self.message = message
        self.status = status


def _fingerprint(config):
    return hashlib.sha256(json.dumps(config["entries"], sort_keys=True).encode()).hexdigest()


def _stat_value(info):
    return json.dumps([info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns], separators=(",", ":"))


def _db_path(config):
    from .history_service import _private_directory
    if config["audience"] != "private" or not config.get("index_dir"):
        raise IndexError()
    _private_directory(config["index_dir"])
    return Path(config["index_dir"]) / "history.sqlite3"


def _connect(path, write=False):
    if write and not path.exists():
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise IndexError()
    connection = sqlite3.connect("file:" + quote(str(path), safe="/") + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute("PRAGMA foreign_keys=ON")
    if write:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
    else:
        connection.execute("PRAGMA query_only=ON")
    return connection


def _files(config):
    entries = []
    visited = 0
    seen = set()
    for entry in config["entries"]:
        root = entry["path"]
        if str(Path(root).resolve()) != os.path.normpath(root):
            raise IndexError()
        if os.path.isfile(root):
            candidates = [root]
        elif os.path.isdir(root):
            candidates = []
            def failed(_error):
                raise IndexError()
            for directory, dirs, names in os.walk(root, followlinks=False, onerror=failed):
                visited += len(dirs) + len(names)
                if visited > MAX_INDEX_ENTRIES:
                    raise IndexError("history index limit exceeded", 413)
                dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(directory, d)))
                candidates.extend(os.path.join(directory, name) for name in sorted(names) if name.endswith(".jsonl"))
                if len(candidates) > MAX_INDEX_FILES:
                    raise IndexError("history index limit exceeded", 413)
        else:
            # Missing enrolled root is a configuration failure, never delete an
            # entire index due to a temporarily unavailable mounted volume.
            raise IndexError()
        for path in candidates:
            if path in seen:
                continue
            seen.add(path)
            entries.append({"path": path, "agent": entry["agent"]})
            if len(entries) > MAX_INDEX_FILES:
                raise IndexError("history index limit exceeded", 413)
    return entries


def _initialize(connection):
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS files (key TEXT PRIMARY KEY, path TEXT NOT NULL, agent TEXT NOT NULL, stamp TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sessions (
      key TEXT PRIMARY KEY, session_id TEXT NOT NULL, original_session_id TEXT NOT NULL, agent TEXT NOT NULL,
      workspace TEXT, title TEXT, started_at TEXT, ended_at TEXT, model TEXT,
      FOREIGN KEY(key) REFERENCES files(key) ON DELETE CASCADE);
    CREATE INDEX IF NOT EXISTS session_ids ON sessions(session_id);
    CREATE INDEX IF NOT EXISTS original_session_ids ON sessions(original_session_id);
    CREATE TABLE IF NOT EXISTS messages (
      key TEXT NOT NULL, seq INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
      no_tools TEXT NOT NULL, author TEXT, created_at INTEGER, uuid TEXT, parent_uuid TEXT,
      branch TEXT, PRIMARY KEY(key,seq), FOREIGN KEY(key) REFERENCES files(key) ON DELETE CASCADE);
    CREATE INDEX IF NOT EXISTS message_keys ON messages(key);
    CREATE VIRTUAL TABLE IF NOT EXISTS search_text USING fts5(key UNINDEXED, text, tokenize='trigram');
    """)


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat() if timestamp else None


def _flatten(content, agent, tools=True):
    # The existing parsers remain the text normalization authority.
    return (claude_code._flatten_content_filtered(content, include_tools=tools)
            if agent == "claude_code" else codex._flatten_content(content))


def _ingest(connection, entry, key, handle):
    """Stream JSONL; store normalized messages without retaining transcript text."""
    path, agent = entry["path"], entry["agent"]
    session_id = None
    workspace = None
    if agent == "claude_code":
        workspace = claude_code._decode_dir_name(claude_code._project_dir_from_path(path))
    model = None
    timestamp_min = timestamp_max = None
    nodes = {}
    children = {}
    root_uuid = None
    seq = 0
    consumed = 0
    tree = False
    while True:
        raw = handle.readline(MAX_LINE + 1)
        if not raw:
            break
        consumed += len(raw)
        if len(raw) > MAX_LINE or consumed > MAX_INDEX_FILE:
            raise IndexError("history index limit exceeded", 413)
        try:
            event = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type", "")
        timestamp = (claude_code._parse_timestamp(event.get("timestamp")) if agent == "claude_code"
                     else codex._parse_timestamp(event.get("timestamp")))
        role, content, author, uuid, parent = "", None, None, None, None
        if agent == "claude_code":
            session_id = session_id or event.get("sessionId")
            if event.get("cwd"):
                workspace = claude_code._normalize_workspace(event["cwd"])
            uuid, parent = event.get("uuid"), event.get("parentUuid")
            if uuid:
                tree = True
                # Nodes include non-message events because they may link forks.
                nodes[uuid] = parent
                if parent:
                    children.setdefault(parent, []).append(uuid)
                elif root_uuid is None:
                    root_uuid = uuid
            message = event.get("message", {})
            if not isinstance(message, dict):
                continue
            if message.get("model"):
                model = message["model"]
            if kind in {"user", "assistant"}:
                role, content, author = message.get("role", kind), message.get("content", ""), message.get("model")
        else:
            if timestamp:
                timestamp_min = min(timestamp_min, timestamp) if timestamp_min is not None else timestamp
                timestamp_max = max(timestamp_max, timestamp) if timestamp_max is not None else timestamp
            payload = event.get("payload", {})
            if not isinstance(payload, dict):
                continue
            if kind == "session_meta":
                session_id, workspace = payload.get("id"), payload.get("cwd")
            elif kind == "turn_context":
                workspace = payload.get("cwd") or workspace
                model = payload.get("model") or model
            elif kind == "response_item" and payload.get("role") in {"user", "assistant"}:
                role, content = payload["role"], payload.get("content", "")
                author = model if role == "assistant" else None
            elif kind == "event_msg" and payload.get("type") == "user_message":
                role, content = "user", payload.get("message", "")
        if content is None:
            continue
        text = _flatten(content, agent)
        if not text.strip():
            continue
        plain = _flatten(content, agent, tools=False)
        connection.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                           (key, seq, role, text, plain, author, timestamp, uuid, parent))
        seq += 1
    active = None
    if tree:
        if root_uuid is None:
            root_uuid = next(iter(nodes))
        tip = root_uuid
        seen = set()
        while tip in children:
            if tip in seen:
                raise IndexError()
            seen.add(tip)
            tip = children[tip][-1]
        active, seen = set(), set()
        cursor = tip
        while cursor:
            if cursor in seen:
                raise IndexError()
            seen.add(cursor)
            active.add(cursor)
            cursor = nodes.get(cursor)
        # Legacy lines without UUIDs are ignored when a valid tree is present,
        # matching parse_session_tree. Retain abandoned branches explicitly.
        connection.execute("DELETE FROM messages WHERE key=? AND uuid IS NULL", (key,))
        connection.execute("UPDATE messages SET branch='abandoned' WHERE key=?", (key,))
        connection.executemany("UPDATE messages SET branch='active' WHERE key=? AND uuid=?", ((key, uuid) for uuid in active))
    clause = "key=? AND (branch IS NULL OR branch='active')"
    first = connection.execute("SELECT content FROM messages WHERE " + clause + " AND role='user' ORDER BY seq LIMIT 1", (key,)).fetchone()
    title = first[0].split("\n", 1)[0].strip()[:100] if first else None
    if agent == "claude_code":
        times = connection.execute("SELECT MIN(created_at),MAX(created_at) FROM messages WHERE " + clause, (key,)).fetchone()
        started, ended = times[0], times[1]
    else:
        started, ended = timestamp_min, timestamp_max
    session_id = session_id or Path(path).stem
    from .history_service import ID
    if not isinstance(session_id, str) or not ID.fullmatch(session_id):
        raise IndexError()
    if not connection.execute("SELECT 1 FROM messages WHERE key=? LIMIT 1", (key,)).fetchone():
        return
    opaque_id = "hist-" + key[:32]
    connection.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                       (key, opaque_id, session_id, agent, workspace, title, _iso(started), _iso(ended), model))
    # Only active-branch content participates in ordinary search.
    connection.execute("INSERT INTO search_text(rowid,key,text) SELECT rowid,key,content FROM messages WHERE " + clause, (key,))


def build(config):
    """Atomically replace changed/deleted files in an explicit snapshot refresh."""
    from .history_service import _open_regular
    path = _db_path(config)
    lock_path = path.with_suffix(".lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        connection = _connect(path, write=True)
        try:
            _initialize(connection)
            connection.execute("BEGIN IMMEDIATE")
            meta = dict(connection.execute("SELECT name,value FROM meta"))
            if meta.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
                raise IndexError()
            fingerprint = _fingerprint(config)
            if meta.get("corpus") not in {None, fingerprint}:
                raise IndexError("history index corpus mismatch")
            seen = set()
            changed = 0
            for entry in _files(config):
                key = hashlib.sha256((entry["agent"] + "\0" + entry["path"]).encode()).hexdigest()
                seen.add(key)
                with os.fdopen(_open_regular(entry["path"]), "rb") as handle:
                    initial = os.fstat(handle.fileno())
                    if initial.st_size > MAX_INDEX_FILE:
                        raise IndexError("history index limit exceeded", 413)
                    stamp = _stat_value(initial)
                    previous = connection.execute("SELECT stamp FROM files WHERE key=?", (key,)).fetchone()
                    if previous and previous[0] == stamp:
                        continue
                    connection.execute("DELETE FROM search_text WHERE rowid IN (SELECT rowid FROM messages WHERE key=?)", (key,))
                    connection.execute("DELETE FROM files WHERE key=?", (key,))
                    connection.execute("INSERT INTO files VALUES (?,?,?,?)", (key, entry["path"], entry["agent"], stamp))
                    _ingest(connection, entry, key, handle)
                    if _stat_value(os.fstat(handle.fileno())) != stamp:
                        raise IndexError("history changed during indexing")
                    changed += 1
            removed = 0
            for row in connection.execute("SELECT key FROM files").fetchall():
                if row[0] not in seen:
                    connection.execute("DELETE FROM search_text WHERE rowid IN (SELECT rowid FROM messages WHERE key=?)", (row[0],))
                    connection.execute("DELETE FROM files WHERE key=?", (row[0],))
                    removed += 1
            indexed_at = datetime.now(timezone.utc).isoformat()
            connection.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)",
                                   [("schema_version", SCHEMA_VERSION), ("corpus", fingerprint), ("indexed_at", indexed_at)])
            connection.commit()
            total = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            return {"files": len(seen), "sessions": total, "updated": changed,
                    "removed": removed, "indexed_at": indexed_at}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    finally:
        os.close(lock_fd)


def _session(row):
    return {name: row[name] for name in ("session_id", "original_session_id", "agent", "title", "started_at", "ended_at", "model")}


def _project(path):
    path = path or ""
    return {"id": hashlib.sha256(path.encode()).hexdigest()[:8], "name": path.rstrip("/").rsplit("/", 1)[-1], "path": path}


def _assert_current(connection, keys):
    from .history_service import _open_regular
    for key in set(keys):
        row = connection.execute("SELECT path,stamp FROM files WHERE key=?", (key,)).fetchone()
        try:
            with os.fdopen(_open_regular(row["path"]), "rb") as handle:
                if _stat_value(os.fstat(handle.fileno())) != row["stamp"]:
                    raise IndexError("history index stale")
        except OSError:
            raise IndexError("history index stale") from None


def _message(row, index, include_tools=True):
    return {"idx": index, "role": row["role"], "content": row["content"] if include_tools else row["no_tools"],
            "author": row["author"], "created_at": row["created_at"], "uuid": row["uuid"],
            "parent_uuid": row["parent_uuid"], "branch": row["branch"]}


def query(config, request):
    try:
        connection = _connect(_db_path(config))
        deadline = time.monotonic() + QUERY_SECONDS
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        connection.create_function("lower", 1, lambda value: value.lower() if isinstance(value, str) else value, deterministic=True)
        try:
            connection.execute("BEGIN")
            catalog = dict(connection.execute("SELECT name,value FROM meta"))
            if catalog.get("schema_version") != SCHEMA_VERSION or catalog.get("corpus") != _fingerprint(config) or "indexed_at" not in catalog:
                raise IndexError()
            return _query(connection, config, request, catalog)
        finally:
            connection.close()
    except IndexError:
        raise
    except Exception:
        raise IndexError() from None


def _query(connection, config, request, catalog):
    route = request["route"]
    meta = {"source": "history", "audience": "private", "snapshot": True,
            "indexed_at": catalog["indexed_at"], "offset": request["offset"], "limit": request["limit"]}
    if route == "chat":
        rows = connection.execute("SELECT * FROM sessions WHERE session_id=? OR original_session_id=? LIMIT 2", (request["id"], request["id"])).fetchall()
        if len(rows) != 1:
            raise IndexError("not found", 404)
        row = rows[0]
        _assert_current(connection, [row["key"]])
        clause = "key=?"
        params = [row["key"]]
        if not request["all_branches"]:
            clause += " AND (branch IS NULL OR branch='active')"
        if "roles" in request:
            clause += " AND role IN (" + ",".join("?" for _ in request["roles"]) + ")"
            params.extend(request["roles"])
        if not request["include_tools"]:
            clause += " AND role!='tool' AND trim(no_tools)!=''"
        total = connection.execute("SELECT COUNT(*) FROM messages WHERE " + clause, params).fetchone()[0]
        skip = max(0, total - request["last"]) if "last" in request else 0
        total -= skip
        page_params = [*params, request["limit"], skip + request["offset"]]
        column = "content" if request["include_tools"] else "no_tools"
        size = connection.execute("SELECT coalesce(SUM(length(CAST(" + column + " AS BLOB))),0) FROM (SELECT " + column + " FROM messages WHERE " + clause + " ORDER BY seq LIMIT ? OFFSET ?)", page_params).fetchone()[0]
        if size > 1024 * 1024:
            raise IndexError("response limit exceeded", 413)
        rows = connection.execute("SELECT * FROM (SELECT *,row_number() OVER (ORDER BY seq)-1 AS display_idx FROM messages WHERE " + clause + ") ORDER BY seq LIMIT ? OFFSET ?", page_params).fetchall()
        file_row = connection.execute("SELECT path FROM files WHERE key=?", (row["key"],)).fetchone()
        result = {**_session(row), "workspace": row["workspace"], "source_path": file_row[0],
                  "messages": [_message(message, message["seq"] if message["branch"] is None else message["display_idx"], request["include_tools"]) for idx, message in enumerate(rows)]}
        meta["total"] = meta["total_messages"] = total
        return ok(result, **meta)
    clauses, params = [], []
    if request.get("project"):
        project = request["project"].lower()
        # ID prefix is a pure workspace-derived value, registered locally below.
        connection.create_function("project_id", 1, lambda value: _project(value)["id"], deterministic=True)
        clauses.append("(instr(lower(coalesce(workspace,'')),?)>0 OR substr(project_id(workspace),1,?)=?)")
        params.extend([project, len(project), project])
    if route == "search":
        terms = []
        for term in request["q"]:
            literal = term.lower()
            # Trigram MATCH accelerates literal terms >=3 characters. Very short
            # queries use bounded-time SQL over indexed text, never source scans.
            if len(literal) >= 3:
                content = "key IN (SELECT key FROM search_text WHERE search_text MATCH ?)"
                content_param = '"' + literal.replace('"', '""') + '"'
            else:
                content = "key IN (SELECT key FROM search_text WHERE instr(lower(text),?)>0)"
                content_param = literal
            terms.append("(instr(lower(coalesce(workspace,'')),?)>0 OR instr(lower(coalesce(title,'')),?)>0 OR " + content + ")")
            params.extend([literal, literal, content_param])
        clauses.append("(" + (" AND " if request["match"] == "all" else " OR ").join(terms) + ")")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    if route == "chats":
        total = connection.execute("SELECT COUNT(*) FROM sessions" + where, params).fetchone()[0]
        rows = connection.execute("SELECT * FROM sessions" + where + " ORDER BY coalesce(ended_at,started_at,'') DESC LIMIT ? OFFSET ?", [*params, request["limit"], request["offset"]]).fetchall()
        _assert_current(connection, [row["key"] for row in rows])
        results = [_session(row) for row in rows]
        workspaces = {row["workspace"] or "" for row in rows}
        if len(workspaces) == 1:
            meta["project"] = _project(next(iter(workspaces)))
    else:
        sort = request["sort"]
        order = "MIN(coalesce(ended_at,started_at,'')) ASC" if sort == "oldest" else "MAX(coalesce(ended_at,started_at,'')) DESC"
        if sort == "relevance":
            order = "COUNT(*) DESC, " + order
        total = connection.execute("SELECT COUNT(*) FROM (SELECT workspace FROM sessions" + where + " GROUP BY workspace)", params).fetchone()[0]
        groups = connection.execute("SELECT workspace,COUNT(*) AS matches FROM sessions" + where + " GROUP BY workspace ORDER BY " + order + " LIMIT ? OFFSET ?", [*params, request["limit"], request["offset"]]).fetchall()
        results = []
        for group in groups:
            group_where = where + (" AND " if where else " WHERE ") + "workspace IS ?"
            rows = connection.execute("SELECT * FROM sessions" + group_where + " ORDER BY coalesce(ended_at,started_at,'') DESC LIMIT 100", [*params, group["workspace"]]).fetchall()
            _assert_current(connection, [row["key"] for row in rows])
            project = _project(group["workspace"])
            result = {**project, "match_fields": [], "matching_sessions": [], "snippets": [],
                      "matching_session_count": group["matches"], "query_terms": request["q"], "match_mode": request["match"], "regex": False}
            for name, text in (("name", project["name"]), ("path", project["path"])):
                if any(t.lower() in text.lower() for t in request["q"]):
                    result["match_fields"].append(name)
            for row in rows:
                snippet = ""
                # Fetch at most one matching content message and return a small
                # excerpt. Never materialize all session messages during search.
                conditions = " OR ".join("instr(lower(content),?)>0" for _ in request["q"])
                hit = connection.execute("SELECT content FROM messages WHERE key=? AND (branch IS NULL OR branch='active') AND (" + conditions + ") ORDER BY seq LIMIT 1", [row["key"], *[t.lower() for t in request["q"]]]).fetchone()
                if hit:
                    positions = [hit[0].lower().find(t.lower()) for t in request["q"]]
                    start = min(p for p in positions if p >= 0)
                    snippet = hit[0][max(0, start - 120):start + 256]
                    if "content" not in result["match_fields"]:
                        result["match_fields"].append("content")
                if any(t.lower() in (row["title"] or "").lower() for t in request["q"]) and "session_title" not in result["match_fields"]:
                    result["match_fields"].append("session_title")
                result["matching_sessions"].append({**_session(row), "snippet": snippet, "match_type": "content" if hit else "title", "match_count": 1})
                if snippet and len(result["snippets"]) < 3:
                    result["snippets"].append(snippet)
            results.append(result)
        meta.update(query=request["q"], project=request.get("project"), sort=sort, match=request["match"], regex=False)
    meta["total"] = total
    return ok(results, **meta)
