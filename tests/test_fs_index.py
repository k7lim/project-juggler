"""Acceptance contract for an opt-in session search index (pj-nd2).

The index is only worth its complexity if it is a drop-in for fs_store's
content search: same results, same freshness, and a safe fallback.  These
tests are that contract.

Assumed interface (SessionStore gains sessions_by_project,
load_session_file and candidate_sessions, which fs_index must implement too):
  pj.fs_index            -- module implementing the SessionStore protocol
  pj.fs_index.refresh()  -- bring the index up to date with the session files
  pj.paths.index_path()  -- where the index file lives (under data_dir())
  PJ_BACKEND=index       -- selects pj.fs_index in get_store()
"""
import importlib
import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from pj import fs_store, session_store
from pj.parsers import claude_code, codex, kimi

QUERIES = [
    "needle",          # several sessions, several workspaces
    "NEEDLE",          # case-insensitive
    "rate limit",      # phrase with a space
    "café",            # non-ASCII
    "tool: bash",      # only in claude_code's synthesized "[Tool: Bash]" text
    "tool result",     # only in synthesized "[Tool Result]" text
    "alphabeta",       # spans two messages: must not match
    'say "hi"',        # JSON-escaped quotes in the raw line
    "go",              # shorter than a trigram
    "absent-term",     # no match
]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for parser in (claude_code, codex, kimi):
        monkeypatch.setattr(parser, "_DEFAULT_ROOT", str(tmp_path / "missing" / parser.agent_slug))
    for key in ("PJ_SOURCES", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "PJ_BACKEND"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PJ_DATA_DIR", str(tmp_path / "pj-data"))
    monkeypatch.setattr(session_store, "_store", fs_store)
    return tmp_path


@pytest.fixture
def fs_index():
    return importlib.import_module("pj.fs_index")


def sessions_home(home):
    # The fixture hides the default roots; pj always scans the yolobox home.
    return home / ".local/share/yolobox/home"


def _write(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def claude_session(home, project, session_id, day, texts, *, tool=False):
    """texts: list of (role, text); day orders activity."""
    events = []
    for i, (role, text) in enumerate(texts):
        content = [{"type": "text", "text": text}]
        if tool and role == "assistant":
            content.append({"type": "tool_use", "name": "Bash", "input": {}})
        events.append({"type": role, "sessionId": session_id, "cwd": f"/w/{project}",
                       "uuid": f"{session_id}-{i}",  # chained, as in real logs
                       "parentUuid": f"{session_id}-{i - 1}" if i else None,
                       "timestamp": f"2026-09-{day:02d}T00:00:{i:02d}Z",
                       "message": {"role": role, "content": content}})
    return _write(sessions_home(home) / f".claude/projects/-w-{project}/{session_id}.jsonl", events)


def codex_session(home, project, session_id, day, texts):
    events = [{"type": "session_meta", "timestamp": f"2026-09-{day:02d}T00:00:00Z",
               "payload": {"id": session_id, "cwd": f"/w/{project}"}}]
    for i, (role, text) in enumerate(texts, start=1):
        kind = "input_text" if role == "user" else "output_text"
        events.append({"type": "response_item", "timestamp": f"2026-09-{day:02d}T00:00:{i:02d}Z",
                       "payload": {"type": "message", "role": role,
                                   "content": [{"type": kind, "text": text}]}})
    return _write(sessions_home(home) / f".codex/sessions/2026/09/{day:02d}/rollout-{session_id}.jsonl", events)


@pytest.fixture
def corpus(home):
    claude_session(home, "alpha", "c1", 1, [("user", "find the needle"), ("assistant", "needle found, needle again")])
    claude_session(home, "alpha", "c2", 3, [("user", "hit the rate limit"), ("assistant", "ends with alpha")], tool=True)
    claude_session(home, "beta", "c3", 5, [("user", "beta then alpha"), ("assistant", "beta café")])
    codex_session(home, "beta", "x1", 2, [("user", "Needle in codex"), ("assistant", 'say "hi" to go')])
    codex_session(home, "gamma", "x2", 4, [("user", "nothing here"), ("assistant", "RATE LIMIT hit")])
    codex_session(home, "gamma", "x3", 6, [("user", "alpha"), ("assistant", "beta")])
    return home


def test_fixture_exercises_fs_store_behaviour(corpus):
    """Characterize the oracle, so parity tests are not vacuous."""
    ids = lambda q, **kw: [h["session_id"] for h in fs_store.search_content(q, **kw)]
    assert ids("needle") == ["x1", "c1"]                       # newest first, both agents
    assert ids("rate limit") == ["x2", "c2"]                   # phrase, case-insensitive
    assert ids("café") == ["c3"]
    assert ids("tool: bash") == ["c2"]                         # synthesized tool text
    assert ids("tool result") == []
    assert ids("alphabeta") == []                              # no cross-message match
    assert ids('say "hi"') == ["x1"]
    assert "x1" in ids("go")
    assert ids("needle", sort="relevance")[0] == "c1"         # 3 occurrences beat 1
    assert ids("alpha", limit=2) == ["x3", "c3"]
    c1 = str(sessions_home(corpus) / ".claude/projects/-w-alpha/c1.jsonl")
    assert len(fs_store.load_session_file(c1, "claude_code")["messages"]) == 2  # the scan path sees Claude messages


def assert_same_as_fs(fs_index, query, *, limit=20, sort="newest"):
    expected = fs_store.search_content(query, limit=limit, sort=sort)
    assert fs_index.search_content(query, limit=limit, sort=sort) == expected, query


@pytest.mark.parametrize("sort", ["newest", "oldest", "relevance"])
@pytest.mark.parametrize("query", QUERIES)
def test_search_content_matches_fs_store_exactly(corpus, fs_index, query, sort):
    fs_index.refresh()
    assert_same_as_fs(fs_index, query, sort=sort)


@pytest.mark.parametrize("sort", ["newest", "oldest", "relevance"])
@pytest.mark.parametrize("query", QUERIES)
def test_search_sessions_matches_fs_store_exactly(corpus, fs_index, query, sort):
    fs_index.refresh()
    expected = fs_store.search_sessions(query, sort=sort)
    assert fs_index.search_sessions(query, sort=sort) == expected, query


@pytest.mark.parametrize("limit", [1, 2, 3, 100])
def test_limit_cuts_at_the_same_session_as_fs_store(corpus, fs_index, limit):
    fs_index.refresh()
    assert_same_as_fs(fs_index, "needle", limit=limit)
    assert_same_as_fs(fs_index, "alpha", limit=limit)


def test_new_session_is_found_without_an_explicit_refresh(corpus, fs_index):
    fs_index.refresh()
    claude_session(corpus, "delta", "c9", 9, [("user", "fresh-needle-xyz")])
    assert [h["session_id"] for h in fs_index.search_content("fresh-needle-xyz")] == ["c9"]


def test_appended_message_is_found_without_an_explicit_refresh(corpus, fs_index):
    fs_index.refresh()
    path = sessions_home(corpus) / ".claude/projects/-w-alpha/c1.jsonl"
    with path.open("a") as f:
        f.write(json.dumps({"type": "user", "sessionId": "c1", "cwd": "/w/alpha",
                            "timestamp": "2026-09-01T00:01:00Z",
                            "message": {"role": "user", "content": "appended-term-qq"}}) + "\n")
    assert_same_as_fs(fs_index, "appended-term-qq")
    assert fs_index.search_content("appended-term-qq")


def test_deleted_session_disappears(corpus, fs_index):
    fs_index.refresh()
    (sessions_home(corpus) / ".claude/projects/-w-alpha/c1.jsonl").unlink()
    assert_same_as_fs(fs_index, "needle")


def test_missing_index_falls_back_to_fs_results(corpus, fs_index):
    from pj import paths
    assert not paths.index_path().exists()
    assert_same_as_fs(fs_index, "needle")


def test_corrupt_index_falls_back_to_fs_results(corpus, fs_index, monkeypatch, capsys):
    from pj import paths
    monkeypatch.setattr(fs_index, "_warned", False)
    fs_index.refresh()
    paths.index_path().write_bytes(b"not a database")
    assert_same_as_fs(fs_index, "needle")
    assert_same_as_fs(fs_index, "alpha")
    assert capsys.readouterr().err.count("session index unavailable") == 1  # once per process


def _forbid_parsing(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("an unchanged file was parsed")
    for parser in (claude_code, codex, kimi):
        monkeypatch.setattr(parser, "parse_session", boom)
        monkeypatch.setattr(parser, "parse_metadata", boom)
    monkeypatch.setattr(claude_code, "parse_session_tree", boom)


@pytest.mark.parametrize("sort", ["newest", "oldest", "relevance"])
def test_index_is_used(corpus, fs_index, monkeypatch, sort):
    """After refresh() no file is dirty, so searches must not parse at all."""
    expected = {q: fs_store.search_content(q, sort=sort) for q in QUERIES}
    expected_titles = {q: fs_store.search_sessions(q, sort=sort) for q in QUERIES}
    fs_index.refresh()
    _forbid_parsing(monkeypatch)
    for query in QUERIES:
        assert fs_index.search_content(query, sort=sort) == expected[query], query
        assert fs_index.search_sessions(query, sort=sort) == expected_titles[query], query


def _assert_index_is_used(fs_index, monkeypatch):
    """Searches answer from the index alone: no session file is parsed."""
    expected = fs_store.search_content("needle")
    expected_titles = fs_store.search_sessions("alpha")
    _forbid_parsing(monkeypatch)
    assert fs_index.search_content("needle") == expected
    assert fs_index.search_sessions("alpha") == expected_titles


def test_wrong_schema_index_falls_back_until_refresh(corpus, fs_index, monkeypatch, capsys):
    from pj import paths
    monkeypatch.setattr(fs_index, "_warned", False)
    fs_index.refresh()
    with sqlite3.connect(str(paths.index_path())) as conn:
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
    conn.close()
    assert_same_as_fs(fs_index, "needle")
    assert capsys.readouterr().err.count("session index unavailable") == 1
    fs_index.refresh()
    _assert_index_is_used(fs_index, monkeypatch)


def test_refresh_rebuilds_a_corrupt_index(corpus, fs_index, monkeypatch):
    from pj import paths
    fs_index.refresh()
    paths.index_path().write_bytes(b"not a database")
    fs_index.refresh()
    _assert_index_is_used(fs_index, monkeypatch)


def test_index_file_is_owner_only_and_under_data_dir(corpus, fs_index):
    from pj import paths
    fs_index.refresh()
    path = paths.index_path()
    assert path.parent == paths.data_dir()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_refresh_is_idempotent(corpus, fs_index):
    fs_index.refresh()
    first = fs_index.search_content("needle", sort="relevance")
    fs_index.refresh()
    assert fs_index.search_content("needle", sort="relevance") == first


def test_backend_env_selects_the_index(home, monkeypatch, fs_index):
    monkeypatch.setenv("PJ_BACKEND", "index")
    monkeypatch.setattr(session_store, "_store", None)
    assert session_store.get_store() is fs_index


def test_index_implements_the_whole_session_store_protocol(fs_index):
    for name in ("available", "list_projects", "recent_session_counts", "project_sessions",
                 "session_details", "search_sessions", "search_content", "get_session",
                 "sessions_by_project", "load_session_file", "candidate_sessions"):
        assert callable(getattr(fs_index, name)), name


# --- Write-back of the per-search dirty set (pj-8qn.7) ---

def _indexed_paths():
    import sqlite3
    from pj import paths
    conn = sqlite3.connect(str(paths.index_path()))
    try:
        return {path for (path,) in conn.execute("SELECT path FROM files")}
    finally:
        conn.close()


def test_search_writes_the_dirty_set_back(corpus, fs_index, monkeypatch):
    fs_index.refresh()
    claude_session(corpus, "delta", "c9", 9, [("user", "fresh-needle-xyz")])
    assert [h["session_id"] for h in fs_index.search_content("fresh-needle-xyz")] == ["c9"]
    _forbid_parsing(monkeypatch)
    assert [h["session_id"] for h in fs_index.search_content("fresh-needle-xyz")] == ["c9"]


def test_search_does_not_wait_for_a_held_write_lock(corpus, fs_index):
    import fcntl
    import threading
    from pj import paths
    fs_index.refresh()
    path = str(claude_session(corpus, "delta", "c9", 9, [("user", "fresh-needle-xyz")]))
    fd = os.open(paths.index_path().with_suffix(".lock"), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        results = []
        search = threading.Thread(target=lambda: results.append(
            fs_index.search_content("fresh-needle-xyz")), daemon=True)
        search.start()
        search.join(timeout=10)
        assert not search.is_alive(), "search waited for the write lock"
    finally:
        os.close(fd)
    assert results == [fs_store.search_content("fresh-needle-xyz")]
    assert path not in _indexed_paths()  # skipped, not queued


def test_none_parse_during_a_search_is_never_written_back(corpus, fs_index, monkeypatch):
    fs_index.refresh()
    path = str(claude_session(corpus, "delta", "c9", 9, [("user", "fresh-needle-xyz")]))
    real = claude_code.parse_session
    with monkeypatch.context() as patch:  # like the query service skipping a non-candidate
        patch.setattr(claude_code, "parse_session",
                      lambda p, *a, **kw: None if p == path else real(p, *a, **kw))
        assert fs_index.search_content("fresh-needle-xyz") == []
    assert path not in _indexed_paths()
    assert [h["session_id"] for h in fs_index.search_content("fresh-needle-xyz")] == ["c9"]


class _Aborted(BaseException):
    """Like the query service's _ScanAborted."""


def test_parse_raising_base_exception_writes_nothing_back(corpus, fs_index, monkeypatch):
    fs_index.refresh()
    kept = str(claude_session(corpus, "delta", "c8", 8, [("user", "fresh-needle-xyz")]))
    aborts = str(claude_session(corpus, "delta", "c9", 9, [("user", "nothing")]))
    real = claude_code.parse_session

    def parse(p, *args, **kwargs):
        if p == aborts:
            raise _Aborted()
        return real(p, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(claude_code, "parse_session", parse)
        with pytest.raises(_Aborted):
            fs_index.search_content("fresh-needle-xyz", sort="oldest")  # parses c8 first
    assert not {kept, aborts} & _indexed_paths()
    assert_same_as_fs(fs_index, "fresh-needle-xyz")


def test_write_back_rolls_back_on_base_exception(corpus, fs_index, monkeypatch):
    fs_index.refresh()
    new = {str(claude_session(corpus, "delta", f"c{d}", d, [("user", "fresh-needle-xyz")]))
           for d in (8, 9)}
    real, calls = fs_index._insert_file, []

    def insert(*args):
        calls.append(args)
        if len(calls) == 2:
            raise _Aborted()
        real(*args)
    with monkeypatch.context() as patch:
        patch.setattr(fs_index, "_insert_file", insert)
        with pytest.raises(_Aborted):
            fs_index.search_content("fresh-needle-xyz")
    assert len(calls) == 2
    assert not new & _indexed_paths()  # the first insert was rolled back too
    assert_same_as_fs(fs_index, "fresh-needle-xyz")


def test_write_back_keeps_files_owner_only_and_restores_the_umask(corpus, fs_index):
    from pj import paths
    fs_index.refresh()
    for suffix in ("-wal", "-shm"):
        Path(f"{paths.index_path()}{suffix}").unlink(missing_ok=True)
    claude_session(corpus, "delta", "c9", 9, [("user", "fresh-needle-xyz")])
    before = os.umask(0o022)
    try:
        fs_index.search_content("fresh-needle-xyz")
        assert os.umask(0o022) == 0o022
    finally:
        os.umask(before)
    for created in paths.data_dir().iterdir():
        assert stat.S_IMODE(os.stat(created).st_mode) == 0o600, created


@pytest.mark.parametrize("bound", [("MAX_DIRTY_FILES", 1), ("MAX_DIRTY_BYTES", 10)])
def test_too_many_dirty_files_gives_fs_store_results(corpus, fs_index, monkeypatch, capsys, bound):
    monkeypatch.setattr(fs_index, "_warned", False)
    monkeypatch.setattr(fs_index, *bound)
    fs_index.refresh()
    new = {str(claude_session(corpus, "delta", "c8", 8, [("user", "fresh-needle-xyz")])),
           str(claude_session(corpus, "delta", "c9", 9, [("user", "fresh title needle")]))}
    assert_same_as_fs(fs_index, "needle")
    assert fs_index.search_sessions("needle") == fs_store.search_sessions("needle")
    assert "pj index refresh" in capsys.readouterr().err
    assert not new & _indexed_paths()  # fs_store answered; nothing was written back


# --- Divergence check: refresh(verify=True) (pj-8qn.9) ---

def _tamper(sql, path):
    from pj import paths
    conn = sqlite3.connect(str(paths.index_path()))
    try:
        with conn:
            conn.execute(sql, (path,))
    finally:
        conn.close()


def test_verify_reports_and_repairs_a_tampered_message(corpus, fs_index):
    fs_index.refresh()
    path = str(sessions_home(corpus) / ".claude/projects/-w-alpha/c1.jsonl")
    _tamper("UPDATE messages SET content = 'tampered' WHERE ord = 0 AND file_id ="
            " (SELECT id FROM files WHERE path = ?)", path)
    first = fs_index.refresh(verify=True)
    assert (first["checked"], first["mismatched"], first["repaired"]) == (6, [path], 1)
    assert_same_as_fs(fs_index, "needle")
    second = fs_index.refresh(verify=True)
    assert (second["checked"], second["mismatched"], second["repaired"]) == (6, [], 0)


def test_verify_checks_at_most_the_sample_size(corpus, fs_index):
    import random
    fs_index.refresh()
    result = fs_index.refresh(verify=True, sample_size=2, rng=random.Random(0))
    assert (result["checked"], result["mismatched"], result["repaired"]) == (2, [], 0)


def test_verify_works_right_after_a_full_build(corpus, fs_index):
    result = fs_index.refresh(verify=True)
    assert result["rebuilt"] is True
    assert (result["checked"], result["mismatched"], result["repaired"]) == (6, [], 0)


def test_plain_refresh_does_not_sample(corpus, fs_index, monkeypatch):
    def forbidden(*args):
        raise AssertionError("plain refresh() must not verify")
    monkeypatch.setattr(fs_index, "_verify", forbidden)
    fs_index.refresh()
    result = fs_index.refresh()
    assert "checked" not in result


@pytest.mark.parametrize("column", ["title", "s_title"])
def test_verify_repairs_a_tampered_session_field(corpus, fs_index, column):
    fs_index.refresh()
    path = str(sessions_home(corpus) / ".codex/sessions/2026/09/02/rollout-x1.jsonl")
    _tamper(f"UPDATE files SET {column} = 'tampered' WHERE path = ?", path)
    assert fs_index.refresh(verify=True)["mismatched"] == [path]
    assert fs_index.refresh(verify=True)["mismatched"] == []
    assert fs_index.search_sessions("needle") == fs_store.search_sessions("needle")
