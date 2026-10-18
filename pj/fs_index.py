"""Opt-in SQLite session index backend (PJ_BACKEND=index).

Implements the SessionStore protocol (session_store.py). It must give the
same results as fs_store, and fs_store results whenever the index cannot
answer. See docs/plans/fs-index.md.

refresh() builds the index file; searches still delegate to fs_store.
"""
from __future__ import annotations

import fcntl
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import fs_store, paths

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE files(
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL, agent TEXT NOT NULL,
  size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, list_pos INTEGER NOT NULL,
  session_id TEXT, workspace TEXT, title TEXT,
  started_at INTEGER, ended_at INTEGER, activity_ms INTEGER NOT NULL,
  s_session_id TEXT, s_workspace TEXT, s_title TEXT,
  s_started_at INTEGER, s_ended_at INTEGER);
CREATE VIRTUAL TABLE messages USING fts5(
  content, role UNINDEXED, file_id UNINDEXED, ord UNINDEXED,
  tokenize = 'trigram');
"""


def cache_signatures() -> dict[str, float]:
    """pj.cache uses this (via hasattr) to invalidate the discover cache."""
    return fs_store.cache_signatures()


# --- Index build ---

def refresh() -> dict:
    """Bring the index file at paths.index_path() up to date with the session files."""
    start = time.monotonic()
    _require_fts5()
    old_umask = os.umask(0o077)  # process-global: always restore
    try:
        _prepare_data_dir()
        with _write_lock(paths.index_path().with_suffix(".lock")):
            counts = _full_build(paths.index_path())
    finally:
        os.umask(old_umask)
    counts["seconds"] = round(time.monotonic() - start, 3)
    return counts


def _require_fts5() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
    except sqlite3.OperationalError as exc:
        raise RuntimeError(f"pj index needs SQLite FTS5 with the trigram tokenizer: {exc}") from exc
    finally:
        conn.close()


def _prepare_data_dir() -> None:
    directory = paths.data_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)


@contextmanager
def _write_lock(lock_path: Path):
    """Single writer: wait for an exclusive flock on the .lock file."""
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock


def _full_build(index: Path) -> dict:
    """Build into <index>.tmp, then atomically replace the index."""
    tmp = index.with_name(index.name + ".tmp")
    _remove_db_files(tmp)
    conn = sqlite3.connect(str(tmp))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        with conn:
            conn.executemany("INSERT INTO meta VALUES (?, ?)", [
                ("schema_version", SCHEMA_VERSION),
                ("built_at", datetime.now(timezone.utc).isoformat()),
            ])
            counts = _sync(conn)
    finally:
        conn.close()
    _remove_db_files(index, keep_main=True)  # a stale -wal must not pair with the new file
    os.replace(tmp, index)
    counts["rebuilt"] = True
    return counts


def _remove_db_files(db: Path, keep_main: bool = False) -> None:
    suffixes = ("-wal", "-shm") if keep_main else ("", "-wal", "-shm")
    for suffix in suffixes:
        try:
            os.unlink(f"{db}{suffix}")
        except FileNotFoundError:
            pass


def _scan():
    """Yield (path, parser, list_pos) for every session file, first root wins."""
    seen: set[str] = set()
    for root, parser in fs_store._configured_roots():
        for pos, path in enumerate(parser.list_sessions(root)):
            if path not in seen:
                seen.add(path)
                yield path, parser, pos


def _sync(conn: sqlite3.Connection) -> dict:
    added = 0
    for path, parser, pos in _scan():
        try:
            st = os.stat(path)
        except OSError:
            continue
        _index_file(conn, path, parser, pos, st)
        added += 1
    return {"added": added, "updated": 0, "removed": 0, "rebuilt": False}


def _index_file(conn: sqlite3.Connection, path: str, parser, pos: int,
                st: os.stat_result) -> None:
    meta = parser.parse_metadata(path)
    session = parser.parse_session(path)
    cur = conn.execute(
        "INSERT INTO files(path, agent, size, mtime_ns, list_pos,"
        " session_id, workspace, title, started_at, ended_at, activity_ms,"
        " s_session_id, s_workspace, s_title, s_started_at, s_ended_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (path, parser.agent_slug, st.st_size, st.st_mtime_ns, pos,
         *_session_fields(meta),
         fs_store._activity_ms(meta) if meta else 0,
         *_session_fields(session)))
    if session:
        conn.executemany(
            "INSERT INTO messages(content, role, file_id, ord) VALUES (?, ?, ?, ?)",
            [(m.content, m.role, cur.lastrowid, i) for i, m in enumerate(session.messages)])


def _session_fields(session) -> tuple:
    """(session_id, workspace, title, started_at, ended_at), all None when absent."""
    if not session:
        return (None,) * 5
    return (session.session_id, session.workspace, session.title,
            session.started_at, session.ended_at)


# --- SessionStore interface ---

def available() -> bool:
    return fs_store.available()


def list_projects(detail: bool = False) -> list[dict]:
    return fs_store.list_projects(detail=detail)


def recent_session_counts(days: int = 7) -> dict[str, int]:
    return fs_store.recent_session_counts(days=days)


def project_sessions(workspace_path: str, limit: int = 50) -> list[dict]:
    return fs_store.project_sessions(workspace_path, limit=limit)


def sessions_by_project(workspace_paths: list[str]) -> dict[str, list[dict]]:
    return fs_store.sessions_by_project(workspace_paths)


def session_details(session_ids: list) -> dict:
    return fs_store.session_details(session_ids)


def search_sessions(query: str, limit: int = 20, sort: str = "newest") -> list[dict]:
    return fs_store.search_sessions(query, limit=limit, sort=sort)


def search_content(query: str, limit: int = 20, sort: str = "newest") -> list[dict]:
    return fs_store.search_content(query, limit=limit, sort=sort)


def get_session(
    session_id: str,
    *,
    all_branches: bool = False,
    include_tools: bool = True,
    roles: set[str] | None = None,
) -> dict | None:
    return fs_store.get_session(session_id, all_branches=all_branches,
                                include_tools=include_tools, roles=roles)


def load_session_file(source_path: str, agent: str) -> dict | None:
    return fs_store.load_session_file(source_path, agent)


def candidate_sessions(terms: list[str], match: str,
                       workspace_paths: list[str]) -> set[str] | None:
    return fs_store.candidate_sessions(terms, match, workspace_paths)
