"""Opt-in SQLite session index backend (PJ_BACKEND=index).

Implements the SessionStore protocol (session_store.py). It must give the
same results as fs_store, and fs_store results whenever the index cannot
answer. See docs/plans/fs-index.md.

refresh() builds the index file. search_content and search_sessions answer
unchanged files from it and parse only the dirty set (new or changed
files); the other calls still delegate to fs_store.
"""
from __future__ import annotations

import fcntl
import functools
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from . import fs_store, paths
from .parsers import base

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
            counts = _refresh_locked(paths.index_path())
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


def _refresh_locked(index: Path) -> dict:
    conn = _open_current(index)
    if conn is None:
        return _full_build(index)
    try:
        with conn:
            return _sync(conn)
    finally:
        conn.close()


def _open_current(index: Path) -> sqlite3.Connection | None:
    """Open the index for update, or None when it is missing, corrupt or outdated."""
    if not index.exists():
        return None
    conn = sqlite3.connect(str(index))
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.DatabaseError:
        row = None
    if row != (SCHEMA_VERSION,):
        conn.close()
        return None
    return conn


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
    """Reparse files whose (size, mtime_ns) changed; drop files that are gone."""
    indexed = {path: (file_id, sig, pos) for path, file_id, *sig, pos in conn.execute(
        "SELECT path, id, size, mtime_ns, list_pos FROM files")}
    changed, moved, present = [], [], set()
    for path, parser, pos in _scan():
        try:
            st = os.stat(path)
        except OSError:
            continue
        present.add(path)
        old = indexed.get(path)
        if old is None or old[1] != [st.st_size, st.st_mtime_ns]:
            changed.append((path, parser, pos, st))
        elif old[2] != pos:
            moved.append((pos, old[0]))
    stale = [indexed[p][0] for p in indexed if p not in present]
    replaced = [indexed[c[0]][0] for c in changed if c[0] in indexed]
    _delete_files(conn, stale + replaced)
    conn.executemany("UPDATE files SET list_pos = ? WHERE id = ?", moved)
    for path, parser, pos, st in changed:
        _index_file(conn, path, parser, pos, st)
    return {"added": len(changed) - len(replaced), "updated": len(replaced),
            "removed": len(stale), "rebuilt": False}


def _delete_files(conn: sqlite3.Connection, file_ids: list[int]) -> None:
    """One pass over messages (file_id is UNINDEXED, so each delete scans)."""
    if not file_ids:
        return
    marks = ",".join("?" * len(file_ids))
    conn.execute(f"DELETE FROM messages WHERE file_id IN ({marks})", file_ids)
    conn.execute(f"DELETE FROM files WHERE id IN ({marks})", file_ids)


def _index_file(conn: sqlite3.Connection, path: str, parser, pos: int,
                st: os.stat_result) -> None:
    _insert_file(conn, path, parser, pos, st,
                 parser.parse_metadata(path), parser.parse_session(path))


def _insert_file(conn: sqlite3.Connection, path: str, parser, pos: int,
                 st: os.stat_result, meta, session) -> None:
    """Store one file's parse results; st must be taken before parsing."""
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


# --- Index search ---
# Parity by construction: fs_store's own loops (search_candidates) run over
# stored rows for unchanged files and over a fresh parse for dirty ones.

_warned = False  # the fallback warning is printed once per process


def _with_index(search, fallback):
    """search(conn) on the index; fallback() when the index cannot answer."""
    index = paths.index_path()
    if not index.exists():
        return _fall_back(fallback, "no index file; run fs_index.refresh()")
    try:
        conn = sqlite3.connect(str(index))
        try:
            return search(conn)
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:  # corrupt file, missing FTS5, ...
        return _fall_back(fallback, str(exc))


def _fall_back(fallback, reason: str):
    global _warned
    if not _warned:
        _warned = True
        print(f"pj: session index unavailable ({reason}); reading session files",
              file=sys.stderr)
    return fallback()


def _indexed_rows(conn: sqlite3.Connection) -> dict:
    conn.row_factory = sqlite3.Row
    return {row["path"]: row for row in conn.execute("SELECT * FROM files")}


class _Dirty(NamedTuple):
    """A listed file the index cannot answer for. st is None if stat failed."""
    path: str
    parser: object
    pos: int
    st: os.stat_result | None


def _listed(rows: dict) -> list:
    """[(path, parser, row, dirty)] in fs_store's list_sessions order.

    row is the stored row when the file is unchanged; otherwise row is None
    and dirty holds the stat taken before any parse, for write-back.
    Deleted files are not listed, so their rows never reach a result.
    """
    listed = []
    for root, parser in fs_store._configured_roots():
        for pos, path in enumerate(parser.list_sessions(root)):
            st = _stat(path)
            row = _clean_row(rows.get(path), parser, st)
            dirty = None if row is not None else _Dirty(path, parser, pos, st)
            listed.append((path, parser, row, dirty))
    return listed


def _stat(path: str) -> os.stat_result | None:
    try:
        return os.stat(path)
    except OSError:
        return None


def _clean_row(row, parser, st: os.stat_result | None):
    if row is None or st is None or row["agent"] != parser.agent_slug:
        return None
    if (st.st_size, st.st_mtime_ns) != (row["size"], row["mtime_ns"]):
        return None
    return row


def _search_content(conn: sqlite3.Connection, query: str, limit: int, sort: str) -> list[dict]:
    listed = _listed(_indexed_rows(conn))
    hits = _matching_messages(conn, query)
    parsed: dict = {}
    candidates = []
    for path, parser, row, dirty in listed:
        if row is None:
            meta = parser.parse_metadata(path)
            candidates.append((fs_store._activity_ms(meta) if meta else 0,
                               functools.partial(_parse_and_keep, dirty, meta, parsed)))
        else:
            candidates.append((row["activity_ms"],
                               functools.partial(_stored_session, row, hits)))
    results = fs_store.search_candidates(candidates, query, limit=limit, sort=sort)
    # Reached only when the search finished: a parse that raised (even a
    # BaseException) skips write-back entirely.
    _write_back(list(parsed.values()))
    return results


def _parse_and_keep(dirty: _Dirty, meta, parsed: dict):
    """parse_session for a dirty file; keep the result for write-back.

    A None result is never kept: inside the pj query service a guarded
    parse returns None for files it skipped, and storing that would make
    the index trust an empty session. Such files stay dirty.
    """
    session = dirty.parser.parse_session(dirty.path)
    if session is not None and dirty.st is not None:
        parsed.setdefault(dirty.path, (dirty, meta, session))  # one row per path
    return session


def _search_sessions(conn: sqlite3.Connection, query: str, limit: int, sort: str) -> list[dict]:
    """Titles come from files.title; fs_store's own loop decides the matches."""
    metas = (parser.parse_metadata(path) if row is None else _stored_metadata(row)
             for path, parser, row, _ in _listed(_indexed_rows(conn)))
    return fs_store.search_titles(metas, query, limit=limit, sort=sort)


# --- Write-back: an optimization only; correctness never depends on it ---

def _write_back(parsed: list) -> None:
    """Store (dirty, meta, session) results in one transaction if the lock is free."""
    if not parsed:
        return
    old_umask = os.umask(0o077)  # anything created here stays owner-only
    try:
        with _try_write_lock(paths.index_path().with_suffix(".lock")) as locked:
            if locked:
                _write_back_locked(paths.index_path(), parsed)
    except (OSError, sqlite3.Error):
        pass  # the next search or refresh() will do it
    finally:
        os.umask(old_umask)


@contextmanager
def _try_write_lock(lock_path: Path):
    """Yield True holding the exclusive flock, or False at once if it is taken."""
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


def _write_back_locked(index: Path, parsed: list) -> None:
    # A fresh connection: the search's may point at an index refresh() replaced.
    conn = _open_current(index)
    if conn is None:
        return
    try:
        with conn:  # commits, or rolls back on any exception, BaseException included
            _replace_files(conn, parsed)
    finally:
        conn.close()


def _replace_files(conn: sqlite3.Connection, parsed: list) -> None:
    marks = ",".join("?" * len(parsed))
    old_ids = [file_id for (file_id,) in conn.execute(
        f"SELECT id FROM files WHERE path IN ({marks})", [d.path for d, _, _ in parsed])]
    _delete_files(conn, old_ids)
    for dirty, meta, session in parsed:
        _insert_file(conn, dirty.path, dirty.parser, dirty.pos, dirty.st, meta, session)


def _stored_metadata(row):
    """The parse_metadata result as stored, or None when it was None."""
    if row["session_id"] is None:
        return None
    return base.NormalizedSession(
        session_id=row["session_id"], agent=row["agent"], source_path=row["path"],
        workspace=row["workspace"], title=row["title"],
        started_at=row["started_at"], ended_at=row["ended_at"])


def _stored_session(row, hits: dict):
    """The stored session with only its candidate messages, or None if none match.

    Messages that cannot match count 0 in fs_store's loop, so leaving them
    out changes nothing; fs_store's own matching rechecks the rest.
    """
    messages = hits.get(row["id"])
    if not messages:
        return None
    return base.NormalizedSession(
        session_id=row["s_session_id"], agent=row["agent"], source_path=row["path"],
        workspace=row["s_workspace"], title=row["s_title"],
        started_at=row["s_started_at"], ended_at=row["s_ended_at"], messages=messages)


def _matching_messages(conn: sqlite3.Connection, query: str) -> dict:
    """{file_id: [NormalizedMessage in order]}: a superset of the messages that match."""
    if _trigram_safe(query):
        sql = "SELECT file_id, ord, role, content FROM messages WHERE messages MATCH ?"
        arg = '"' + query.replace('"', '""') + '"'
    else:
        conn.create_function("pj_count", 2, fs_store._count_occurrences, deterministic=True)
        sql = "SELECT file_id, ord, role, content FROM messages WHERE pj_count(content, ?) > 0"
        arg = query
    hits: dict = {}
    for file_id, ord_, role, content in conn.execute(sql, (arg,)):
        hits.setdefault(file_id, []).append(base.NormalizedMessage(idx=ord_, role=role, content=content))
    for messages in hits.values():
        messages.sort(key=lambda m: m.idx)
    return hits


def _trigram_safe(query: str) -> bool:
    """True when FTS5 trigram MATCH finds every message str.lower() matching finds.

    Not for terms under 3 characters (no trigram) or non-ASCII terms (SQLite
    folds case differently). One ASCII gap remains: "İ".lower() is "i" plus a
    combining dot, which SQLite does not fold, so "abi" matches "ABİ" only in
    Python. ASCII terms ending in "i" therefore take the exact path too.
    """
    return len(query) >= 3 and query.isascii() and not query.lower().endswith("i")


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
    return _with_index(
        lambda conn: _search_sessions(conn, query, limit, sort),
        lambda: fs_store.search_sessions(query, limit=limit, sort=sort))


def search_content(query: str, limit: int = 20, sort: str = "newest") -> list[dict]:
    return _with_index(
        lambda conn: _search_content(conn, query, limit, sort),
        lambda: fs_store.search_content(query, limit=limit, sort=sort))


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
