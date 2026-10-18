"""Opt-in SQLite session index backend (PJ_BACKEND=index).

Implements the SessionStore protocol (session_store.py). It must give the
same results as fs_store, and fs_store results whenever the index cannot
answer. See docs/plans/fs-index.md.

refresh() builds the index file. search_content, search_sessions and the
metadata calls (list_projects, recent_session_counts, sessions_by_project,
project_sessions) answer unchanged files from it and parse only the dirty
set (new or changed files); candidate_sessions narrows search's scan path
from it. The other calls still delegate to fs_store.
"""
from __future__ import annotations

import bisect
import fcntl
import functools
import os
import random
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from . import fs_store, paths
from .parsers import base

SCHEMA_VERSION = "2"

_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE files(
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL, agent TEXT NOT NULL,
  size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, list_pos INTEGER NOT NULL,
  session_id TEXT, workspace TEXT, title TEXT,
  started_at INTEGER, ended_at INTEGER, activity_ms INTEGER NOT NULL, model TEXT,
  s_session_id TEXT, s_workspace TEXT, s_title TEXT,
  s_started_at INTEGER, s_ended_at INTEGER,
  first_rowid INTEGER, last_rowid INTEGER);
CREATE VIRTUAL TABLE messages USING fts5(
  content, role UNINDEXED, file_id UNINDEXED, ord UNINDEXED,
  tokenize = 'trigram');
"""


def cache_signatures() -> dict[str, float]:
    """pj.cache uses this (via hasattr) to invalidate the discover cache."""
    return fs_store.cache_signatures()


# --- Index build ---

VERIFY_SAMPLE = 50


def refresh(verify: bool = False, sample_size: int = VERIFY_SAMPLE,
            rng: random.Random | None = None) -> dict:
    """Bring the index file at paths.index_path() up to date with the session files.

    verify=True also runs the divergence check (see _verify) on up to
    sample_size indexed files, under the same write lock.
    """
    start = time.monotonic()
    _require_fts5()
    old_umask = os.umask(0o077)  # process-global: always restore
    try:
        _prepare_data_dir()
        with _write_lock(paths.index_path().with_suffix(".lock")):
            counts = _refresh_locked(paths.index_path())
            if verify:
                counts.update(_verify_locked(paths.index_path(), sample_size,
                                             rng or random.Random()))
    finally:
        os.umask(old_umask)
    counts["seconds"] = round(time.monotonic() - start, 3)
    return counts


def status() -> dict:
    """Describe the index file without parsing sessions or creating any file."""
    index = paths.index_path()
    info = {"path": str(index), "exists": index.exists(), "bytes": None,
            "schema_version": None, "schema_matches": False,
            "indexed_files": None, "dirty_files": None, "built_at": None,
            "fts5": _fts5_available()}
    if not info["exists"]:
        return info
    info["bytes"] = index.stat().st_size
    # mode=rw never creates the file; mode=ro cannot open a WAL file without its -shm.
    conn = sqlite3.connect(index.as_uri() + "?mode=rw", uri=True)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        info["schema_version"] = meta.get("schema_version")
        info["built_at"] = meta.get("built_at")
        info["schema_matches"] = info["schema_version"] == SCHEMA_VERSION
        if info["schema_matches"]:
            listed = _listed(_indexed_rows(conn))
            info["indexed_files"] = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            info["dirty_files"] = sum(1 for _, _, _, dirty in listed if dirty is not None)
    except sqlite3.DatabaseError:
        pass  # corrupt: report what is known; refresh() rebuilds it
    finally:
        conn.close()
    return info


def _fts5_available() -> bool:
    try:
        _require_fts5()
    except RuntimeError:
        return False
    return True


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
        # WAL mode persists in the db header, so the replaced index opens in WAL.
        # TRUNCATE moves every page into <index>.tmp before close; the sidecars
        # (kept by builds with persistent WAL, e.g. macOS) then hold nothing.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    _remove_db_files(tmp, keep_main=True)
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
        st = _stat(path)
        if st is None:
            continue
        present.add(path)
        old = indexed.get(path)
        if old is None or old[1] != [st.size, st.mtime_ns]:
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
    """Delete each file's messages by its rowid range, then its files row."""
    if not file_ids:
        return
    marks = ",".join("?" * len(file_ids))
    ranges = conn.execute(f"SELECT first_rowid, last_rowid FROM files WHERE id IN ({marks})"
                          " AND first_rowid IS NOT NULL", file_ids).fetchall()
    conn.executemany("DELETE FROM messages WHERE rowid BETWEEN ? AND ?", ranges)
    conn.execute(f"DELETE FROM files WHERE id IN ({marks})", file_ids)


def _index_file(conn: sqlite3.Connection, path: str, parser, pos: int,
                st: _Sig) -> None:
    _insert_file(conn, path, parser, pos, st,
                 parser.parse_metadata(path), parser.parse_session(path))


def _insert_file(conn: sqlite3.Connection, path: str, parser, pos: int,
                 st: _Sig, meta, session) -> None:
    """Store one file's parse results; st must be taken before parsing."""
    messages = session.messages if session else []
    # A file's messages get explicit consecutive rowids above every existing
    # one, recorded as [first_rowid, last_rowid]. Only the flock holder
    # writes, inside one transaction, so nothing can interleave.
    first = last = None
    if messages:
        (top,) = conn.execute("SELECT coalesce((SELECT rowid FROM messages"
                              " ORDER BY rowid DESC LIMIT 1), 0)").fetchone()
        first, last = top + 1, top + len(messages)
    cur = conn.execute(
        "INSERT INTO files(path, agent, size, mtime_ns, list_pos,"
        " session_id, workspace, title, started_at, ended_at, activity_ms, model,"
        " s_session_id, s_workspace, s_title, s_started_at, s_ended_at,"
        " first_rowid, last_rowid)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (path, parser.agent_slug, st.size, st.mtime_ns, pos,
         *_fresh_fields(meta, session), first, last))
    conn.executemany(
        "INSERT INTO messages(rowid, content, role, file_id, ord) VALUES (?, ?, ?, ?, ?)",
        [(first + i, m.content, m.role, cur.lastrowid, i) for i, m in enumerate(messages)])


def _session_fields(session) -> tuple:
    """(session_id, workspace, title, started_at, ended_at), all None when absent."""
    if not session:
        return (None,) * 5
    return (session.session_id, session.workspace, session.title,
            session.started_at, session.ended_at)


# --- Divergence check ---
# Size plus mtime misses a file rewritten with the same size and mtime, so
# refresh(verify=True) compares a random sample with a fresh parse.
# "Mismatch" means the stored messages (content, role, order) or the stored
# session fields (metadata, s_* and activity_ms) differ.

def _verify_locked(index: Path, sample_size: int, rng: random.Random) -> dict:
    conn = _open_current(index)  # refresh just made it current
    if conn is None:
        return {"checked": 0, "mismatched": [], "repaired": 0}
    try:
        with conn:
            return _verify(conn, sample_size, rng)
    finally:
        conn.close()


def _verify(conn: sqlite3.Connection, sample_size: int, rng: random.Random) -> dict:
    """Re-index every sampled file whose stored copy differs from a fresh parse."""
    parsers = {parser.agent_slug: parser for _, parser in fs_store._configured_roots()}
    rows = [r for r in conn.execute(f"SELECT id, path, agent, list_pos, {_FIELDS} FROM files"
                                    " ORDER BY id") if r[2] in parsers]
    sample = rng.sample(rows, min(sample_size, len(rows)))
    stored = _stored_messages(conn, [r[0] for r in sample])
    checked, bad = 0, []
    for file_id, path, agent, pos, *fields in sample:
        st = _stat(path)  # taken before parsing, as _insert_file requires
        if st is None:
            continue  # gone since the sync; the next refresh drops it
        checked += 1
        parser = parsers[agent]
        meta, session = parser.parse_metadata(path), parser.parse_session(path)
        if (stored.get(file_id, []) != _fresh_messages(session)
                or tuple(fields) != _fresh_fields(meta, session)):
            bad.append((file_id, path, parser, pos, st, meta, session))
    _delete_files(conn, [b[0] for b in bad])
    for _, path, parser, pos, st, meta, session in bad:
        _insert_file(conn, path, parser, pos, st, meta, session)
    return {"checked": checked, "mismatched": sorted(b[1] for b in bad), "repaired": len(bad)}


_FIELDS = ("session_id, workspace, title, started_at, ended_at, activity_ms, model,"
           " s_session_id, s_workspace, s_title, s_started_at, s_ended_at")


def _fresh_fields(meta, session) -> tuple:
    """The _FIELDS values _insert_file would store for this parse."""
    return (*_session_fields(meta), fs_store._activity_ms(meta) if meta else 0,
            meta.model if meta else None, *_session_fields(session))


def _stored_messages(conn: sqlite3.Connection, file_ids: list[int]) -> dict:
    """{file_id: [(content, role, ord)] in order}, read by each file's rowid range."""
    if not file_ids:
        return {}
    marks = ",".join("?" * len(file_ids))
    stored: dict = {}
    for file_id, first, last in conn.execute(
            f"SELECT id, first_rowid, last_rowid FROM files WHERE id IN ({marks})"
            " AND first_rowid IS NOT NULL", file_ids).fetchall():
        stored[file_id] = conn.execute(
            "SELECT content, role, ord FROM messages WHERE rowid BETWEEN ? AND ?"
            " ORDER BY ord", (first, last)).fetchall()
    return stored


def _fresh_messages(session) -> list:
    if not session:
        return []
    return [(m.content, m.role, i) for i, m in enumerate(session.messages)]


# --- Index search ---
# Parity by construction: fs_store's own loops (search_candidates) run over
# stored rows for unchanged files and over a fresh parse for dirty ones.

_warned = False  # the fallback warning is printed once per process

# Above either bound the index is too stale to help: fs_store answers, so a
# burst of new history cannot make one search slower than fs_store's.
MAX_DIRTY_FILES = 200
MAX_DIRTY_BYTES = 500 * 1024 * 1024


class _TooStale(Exception):
    """The dirty set exceeds MAX_DIRTY_FILES or MAX_DIRTY_BYTES."""


def _with_index(search, fallback):
    """search(conn) on the index; fallback() when the index cannot answer."""
    conn = _open_current(paths.index_path())
    if conn is None:
        return _fall_back(fallback, "index missing, corrupt or outdated;"
                                    " run `pj index refresh`")
    try:
        try:
            return search(conn)
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:  # corrupt file, missing FTS5, ...
        return _fall_back(fallback, str(exc))
    except _TooStale as exc:
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


class _Sig(NamedTuple):
    """A session path's change signature; see _stat."""
    size: int
    mtime_ns: int


class _Dirty(NamedTuple):
    """A listed file the index cannot answer for. st is None if stat failed."""
    path: str
    parser: object
    pos: int
    st: _Sig | None


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


def _check_dirty_bound(listed: list) -> None:
    dirty = {d.path: d for _, _, _, d in listed if d is not None}
    size = sum(d.st.size for d in dirty.values() if d.st is not None)
    if len(dirty) > MAX_DIRTY_FILES or size > MAX_DIRTY_BYTES:
        raise _TooStale(f"{len(dirty)} changed files, {size} bytes;"
                        " run `pj index refresh`")


def _stat(path: str) -> _Sig | None:
    """Aggregate over fs_store._signature_paths, as fs_store.cache_signatures does.

    A directory session (kimi) is represented by the files inside it, whose
    growth does not change the directory's own stat. None if none can be stat'ed.
    """
    sts = []
    for sig_path in fs_store._signature_paths(path):
        try:
            sts.append(os.stat(sig_path))
        except OSError:
            continue
    if not sts:
        return None
    return _Sig(sum(st.st_size for st in sts), max(st.st_mtime_ns for st in sts))


def _clean_row(row, parser, st: _Sig | None):
    if row is None or st is None or row["agent"] != parser.agent_slug:
        return None
    if (st.size, st.mtime_ns) != (row["size"], row["mtime_ns"]):
        return None
    return row


def _search_content(conn: sqlite3.Connection, query: str, limit: int, sort: str) -> list[dict]:
    listed = _listed(_indexed_rows(conn))
    _check_dirty_bound(listed)
    match = _match_clause(conn, query)
    rowids = _matching_rowids(conn, match)
    parsed: dict = {}
    candidates = []
    for path, parser, row, dirty in listed:
        if row is None:
            meta = parser.parse_metadata(path)
            candidates.append((fs_store._activity_ms(meta) if meta else 0,
                               functools.partial(_parse_and_keep, dirty, meta, parsed)))
        else:
            candidates.append((row["activity_ms"],
                               functools.partial(_stored_session, conn, row, match, rowids)))
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
    return fs_store.search_titles(_metadata(conn), query, limit=limit, sort=sort)


def _metadata(conn: sqlite3.Connection) -> list:
    """parse_metadata results in list_sessions order: stored for clean files, parsed for dirty.

    Dirty files are not written back: writing needs parse_session too.
    """
    listed = _listed(_indexed_rows(conn))
    _check_dirty_bound(listed)
    return [parser.parse_metadata(path) if row is None else _stored_metadata(row)
            for path, parser, row, _ in listed]


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
        started_at=row["started_at"], ended_at=row["ended_at"], model=row["model"])


def _stored_session(conn: sqlite3.Connection, row, match: tuple[str, str], rowids: list[int]):
    """The stored session with only its candidate messages, or None if none match.

    Messages that cannot match count 0 in fs_store's loop, so leaving them
    out changes nothing; fs_store's own matching rechecks the rest.
    search_candidates calls this lazily, so message text is read only for
    the files its loop reaches before it stops at the limit.
    """
    if not _has_match(row, rowids):
        return None
    messages = _matching_messages(conn, row, match)
    return base.NormalizedSession(
        session_id=row["s_session_id"], agent=row["agent"], source_path=row["path"],
        workspace=row["s_workspace"], title=row["s_title"],
        started_at=row["s_started_at"], ended_at=row["s_ended_at"], messages=messages)


def _matching_rowids(conn: sqlite3.Connection, match: tuple[str, str]) -> list[int]:
    """Sorted rowids of a superset of the messages that match.

    Only rowids: reading file_id (UNINDEXED) or content would read every
    matching message's whole record. _has_match maps them to files.
    """
    where, arg = match
    return sorted(r for (r,) in conn.execute(f"SELECT rowid FROM messages WHERE {where}", (arg,)))


def _has_match(row, rowids: list[int]) -> bool:
    """True when some matching rowid lies in row's [first_rowid, last_rowid]."""
    if row["first_rowid"] is None:
        return False
    i = bisect.bisect_left(rowids, row["first_rowid"])
    return i < len(rowids) and rowids[i] <= row["last_rowid"]


def _matching_messages(conn: sqlite3.Connection, row, match: tuple[str, str]) -> list:
    """[NormalizedMessage in order]: row's messages that may match, by its rowid range."""
    where, arg = match
    return [base.NormalizedMessage(idx=ord_, role=role, content=content)
            for ord_, role, content in conn.execute(
                "SELECT ord, role, content FROM messages WHERE rowid BETWEEN ? AND ?"
                f" AND {where} ORDER BY ord", (row["first_rowid"], row["last_rowid"], arg))]


def _match_clause(conn: sqlite3.Connection, query: str) -> tuple[str, str]:
    """(WHERE clause, its argument) selecting a superset of the messages matching query."""
    if _trigram_safe(query):
        return "messages MATCH ?", '"' + query.replace('"', '""') + '"'
    conn.create_function("pj_count", 2, fs_store._count_occurrences, deterministic=True)
    return "pj_count(content, ?) > 0", query


def _trigram_safe(query: str) -> bool:
    """True when FTS5 trigram MATCH finds every message str.lower() matching finds.

    Not for terms under 3 characters (no trigram) or non-ASCII terms (SQLite
    folds case differently). One ASCII gap remains: "İ".lower() is "i" plus a
    combining dot, which SQLite does not fold, so "abi" matches "ABİ" only in
    Python. ASCII terms ending in "i" therefore take the exact path too.
    """
    return len(query) >= 3 and query.isascii() and not query.lower().endswith("i")


# --- Narrowing the scan path ---
# search._scan_sessions matches a whole session: " ".join([title, *messages]).
# The candidate set must be a superset of those matches.

MIN_FILTER_WORD = 3  # shorter words give no trigram filter


def _term_filters(terms: list[str], match: str) -> list[list[str]] | None:
    """Per term, the words to filter by; None when narrowing is impossible.

    A phrase can span the joining space, so it is filtered by each word
    (each word lies inside the title or one message). A term whose words
    are all short gives no filter: dropped for match="all", fatal for "any".
    """
    filters = [[w for w in term.split() if len(w) >= MIN_FILTER_WORD] for term in terms]
    usable = [f for f in filters if f]
    if not usable or (match != "all" and len(usable) < len(filters)):
        return None
    return usable


def _candidate_sessions(conn: sqlite3.Connection, filters: list[list[str]], match: str,
                        workspaces: set[str]) -> set[str]:
    listed = _listed(_indexed_rows(conn))
    _check_dirty_bound(listed)
    rows = {row["id"]: row for _, _, row, _ in listed
            if row is not None and row["workspace"] in workspaces}
    sets = [_term_candidates(conn, rows, words) for words in filters]
    found = set.intersection(*sets) if match == "all" else set.union(*sets)
    return found | {path for path, _, row, _ in listed if row is None}  # dirty: always


def _term_candidates(conn: sqlite3.Connection, rows: dict, words: list[str]) -> set[str]:
    """Paths of rows where each word is in the title or in some message."""
    return set.intersection(*(_word_candidates(conn, rows, w) for w in words))


def _word_candidates(conn: sqlite3.Connection, rows: dict, word: str) -> set[str]:
    where, arg = _match_clause(conn, word)
    ids = {file_id for (file_id,) in conn.execute(
        f"SELECT DISTINCT file_id FROM messages WHERE {where}", (arg,))}
    lowered = word.lower()
    return {row["path"] for file_id, row in rows.items()
            if file_id in ids or lowered in (row["title"] or "").lower()}


# --- SessionStore interface ---

def available() -> bool:
    return fs_store.available()


def list_projects(detail: bool = False) -> list[dict]:
    return _with_index(
        lambda conn: fs_store.group_projects(_metadata(conn), detail=detail),
        lambda: fs_store.list_projects(detail=detail))


def recent_session_counts(days: int = 7) -> dict[str, int]:
    return _with_index(
        lambda conn: fs_store.count_recent(_metadata(conn), days=days),
        lambda: fs_store.recent_session_counts(days=days))


def project_sessions(workspace_path: str, limit: int = 50) -> list[dict]:
    return _with_index(
        lambda conn: fs_store.project_summaries(_metadata(conn), workspace_path, limit=limit),
        lambda: fs_store.project_sessions(workspace_path, limit=limit))


def sessions_by_project(workspace_paths: list[str]) -> dict[str, list[dict]]:
    return _with_index(
        lambda conn: fs_store.summaries_by_project(_metadata(conn), workspace_paths),
        lambda: fs_store.sessions_by_project(workspace_paths))


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
    """Source paths in these workspaces that may match; None means scan them all."""
    filters = _term_filters(terms, match)
    if filters is None:
        return None
    return _with_index(
        lambda conn: _candidate_sessions(conn, filters, match, set(workspace_paths)),
        lambda: None)
