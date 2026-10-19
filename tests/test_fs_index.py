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
from datetime import datetime, timezone
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


def _kimi_turn(text, ts):
    return {"timestamp": ts, "message": {"type": "TurnBegin",
                                         "payload": {"user_input": [{"type": "text", "text": text}]}}}


def kimi_session(home, session_id, texts):
    """A kimi session is a directory; list_sessions yields the directory."""
    session_dir = home / ".kimi-test/sessions/wshash" / session_id
    _write(session_dir / "state.json", [{"custom_title": session_id}])
    _write(session_dir / "wire.jsonl", [_kimi_turn(t, 1788000000 + i) for i, t in enumerate(texts)])
    return session_dir


def _append_to_kimi_keeping_dir_stat(session_dir, text):
    """Grow wire.jsonl; restore the directory's mtime so only the file changes."""
    dir_st = os.stat(session_dir)
    with (session_dir / "wire.jsonl").open("a") as f:
        f.write(json.dumps(_kimi_turn(text, 1788000100)) + "\n")
    os.utime(session_dir, ns=(dir_st.st_atime_ns, dir_st.st_mtime_ns))
    assert os.stat(session_dir).st_mtime_ns == dir_st.st_mtime_ns


def test_appended_kimi_message_is_found_without_an_explicit_refresh(corpus, fs_index):
    session_dir = kimi_session(corpus, "k1", ["kimi needle"])
    fs_index.refresh()
    _append_to_kimi_keeping_dir_stat(session_dir, "kimi-appended-zz")
    assert_same_as_fs(fs_index, "kimi-appended-zz")
    assert fs_index.search_content("kimi-appended-zz")


def test_refresh_updates_an_appended_kimi_session(corpus, fs_index, monkeypatch):
    session_dir = kimi_session(corpus, "k1", ["kimi needle"])
    fs_index.refresh()
    _append_to_kimi_keeping_dir_stat(session_dir, "kimi-appended-zz")
    assert fs_index.refresh()["updated"] == 1
    expected = fs_store.search_content("kimi-appended-zz")
    assert expected
    _forbid_parsing(monkeypatch)  # the unchanged kimi session is answered from the index
    assert fs_index.search_content("kimi-appended-zz") == expected


def test_stat_signature_is_one_stat_for_a_file_and_the_aggregate_for_a_dir(
        corpus, fs_index, monkeypatch):
    session_dir = kimi_session(corpus, "k1", ["kimi needle"])
    file_path = str(sessions_home(corpus) / ".claude/projects/-w-alpha/c1.jsonl")
    file_st = os.stat(file_path)
    parts = [os.stat(p) for p in fs_store._signature_paths(str(session_dir))]
    assert len(parts) == 2
    calls = []
    real_stat = os.stat
    monkeypatch.setattr(os, "stat", lambda p, *a, **k: calls.append(p) or real_stat(p, *a, **k))

    assert fs_index._stat(file_path) == (file_st.st_size, file_st.st_mtime_ns)
    assert calls == [file_path]
    assert fs_index._stat(str(session_dir)) == (
        sum(st.st_size for st in parts), max(st.st_mtime_ns for st in parts))
    assert fs_index._stat(str(session_dir / "missing")) is None


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
    err = capsys.readouterr().err
    assert err.count("session index unavailable") == 1  # once per process
    assert "run `pj index refresh`" in err


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


def _assert_clean_wal_index(fs_index, monkeypatch):
    from pj import paths
    assert list(paths.data_dir().glob("*.tmp*")) == []
    conn = sqlite3.connect(str(paths.index_path()))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    finally:
        conn.close()
    _assert_index_is_used(fs_index, monkeypatch)


def test_full_build_leaves_no_tmp_files(corpus, fs_index, monkeypatch):
    fs_index.refresh()
    _assert_clean_wal_index(fs_index, monkeypatch)


def test_rebuild_of_corrupt_index_leaves_no_tmp_files(corpus, fs_index, monkeypatch):
    from pj import paths
    fs_index.refresh()
    paths.index_path().write_bytes(b"not a database")
    fs_index.refresh()
    _assert_clean_wal_index(fs_index, monkeypatch)


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


@pytest.mark.parametrize("column", ["title", "s_title", "model"])
def test_verify_repairs_a_tampered_session_field(corpus, fs_index, column):
    fs_index.refresh()
    path = str(sessions_home(corpus) / ".codex/sessions/2026/09/02/rollout-x1.jsonl")
    _tamper(f"UPDATE files SET {column} = 'tampered' WHERE path = ?", path)
    assert fs_index.refresh(verify=True)["mismatched"] == [path]
    assert fs_index.refresh(verify=True)["mismatched"] == []
    assert fs_index.search_sessions("needle") == fs_store.search_sessions("needle")


# --- Narrowing the scan path: candidate_sessions (pj-8qn.10) ---

def _ids(results):
    return sorted(s["session_id"] for p in results for s in p.get("matching_sessions", []))


def _search_with(store, monkeypatch, *args, **kwargs):
    from pj import search
    monkeypatch.setattr(session_store, "_store", store)
    return search.search(*args, **kwargs)


@pytest.mark.parametrize("query, kwargs", [
    ("needle", {"project": "alpha"}),
    ("café", {"project": "beta"}),
    (["beta", "alpha"], {"match": "all"}),
    ("with alpha", {"project": "span"}),   # only across two messages of s1
])
def test_scan_search_matches_fs_store_with_narrowing(corpus, fs_index, monkeypatch, query, kwargs):
    claude_session(corpus, "span", "s1", 7, [("user", "ends with"), ("assistant", "alpha x")])
    fs_index.refresh()
    expected = _search_with(fs_store, monkeypatch, query, **kwargs)
    assert expected, "a parity test on empty results proves nothing"
    terms = query if isinstance(query, list) else [query]
    workspaces = ["/w/alpha", "/w/beta", "/w/gamma", "/w/span"]
    assert fs_index.candidate_sessions(terms, kwargs.get("match", "any"), workspaces) is not None
    assert _search_with(fs_index, monkeypatch, query, **kwargs) == expected


def test_fixture_match_all_scan_finds_x3_and_c3(corpus, monkeypatch):
    results = _search_with(fs_store, monkeypatch, ["beta", "alpha"], match="all")
    assert set(_ids(results)) == {"x3", "c3"}


def _path(home, rel):
    return str(sessions_home(home) / rel)


C1 = ".claude/projects/-w-alpha/c1.jsonl"
C2 = ".claude/projects/-w-alpha/c2.jsonl"
X1 = ".codex/sessions/2026/09/02/rollout-x1.jsonl"
ALL_WS = ["/w/alpha", "/w/beta", "/w/gamma"]


def test_candidates_are_per_workspace_and_per_session(corpus, fs_index):
    fs_index.refresh()
    assert fs_index.candidate_sessions(["needle"], "any", ["/w/alpha"]) == {_path(corpus, C1)}
    assert fs_index.candidate_sessions(["needle"], "any", ALL_WS) == {
        _path(corpus, C1), _path(corpus, X1)}
    # x3 has "alpha" and "beta" in different messages; c2 has only "alpha"
    assert fs_index.candidate_sessions(["beta", "alpha"], "all", ALL_WS) == {
        _path(corpus, ".claude/projects/-w-beta/c3.jsonl"),
        _path(corpus, ".codex/sessions/2026/09/06/rollout-x3.jsonl")}


def test_short_word_gives_no_narrowing_for_any_and_is_dropped_for_all(corpus, fs_index):
    fs_index.refresh()
    assert fs_index.candidate_sessions(["go"], "any", ALL_WS) is None
    assert fs_index.candidate_sessions(["needle", "go"], "any", ALL_WS) is None
    assert (fs_index.candidate_sessions(["needle", "go"], "all", ALL_WS)
            == fs_index.candidate_sessions(["needle"], "all", ALL_WS))
    assert fs_index.candidate_sessions(["go", "ab"], "all", ALL_WS) is None


def test_phrase_is_filtered_by_each_word(corpus, fs_index):
    claude_session(corpus, "span", "s1", 7, [("user", "ends with"), ("assistant", "alpha x")])
    fs_index.refresh()
    s1 = _path(corpus, ".claude/projects/-w-span/s1.jsonl")
    assert s1 in fs_index.candidate_sessions(["with alpha"], "any", ["/w/span"])


def test_non_trigram_words_use_the_exact_scan(corpus, fs_index):
    dotted = str(claude_session(corpus, "delta", "c9", 9, [("user", "hello"), ("assistant", "XYZABİ")]))
    fs_index.refresh()
    assert "zabi" in "XYZABİ".lower()  # İ lowers to i + U+0307; trigram MATCH misses it
    assert fs_index.candidate_sessions(["zabi"], "any", ["/w/delta"]) == {dotted}
    c3 = _path(corpus, ".claude/projects/-w-beta/c3.jsonl")
    assert fs_index.candidate_sessions(["CAFÉ"], "any", ALL_WS) == {c3}


def test_workspace_filter_uses_the_metadata_workspace(corpus, fs_index):
    fs_index.refresh()
    _tamper("UPDATE files SET s_workspace = '/w/elsewhere' WHERE path = ?", _path(corpus, C1))
    assert fs_index.candidate_sessions(["needle"], "any", ["/w/alpha"]) == {_path(corpus, C1)}


def test_title_is_matched_against_the_stored_title(corpus, fs_index):
    fs_index.refresh()
    _tamper("UPDATE files SET title = 'zebra crossing' WHERE path = ?", _path(corpus, C1))
    assert fs_index.candidate_sessions(["zebra"], "any", ["/w/alpha"]) == {_path(corpus, C1)}


def test_dirty_file_is_always_a_candidate(corpus, fs_index):
    fs_index.refresh()
    new = str(claude_session(corpus, "delta", "c9", 9, [("user", "nothing relevant")]))
    assert new in fs_index.candidate_sessions(["needle"], "any", ["/w/alpha"])


def test_unusable_index_gives_no_narrowing(corpus, fs_index, monkeypatch):
    from pj import paths
    monkeypatch.setattr(fs_index, "_warned", True)  # keep stderr quiet
    assert fs_index.candidate_sessions(["needle"], "any", ALL_WS) is None  # missing
    fs_index.refresh()
    paths.index_path().write_bytes(b"not a database")
    assert fs_index.candidate_sessions(["needle"], "any", ALL_WS) is None  # corrupt


def test_too_stale_index_gives_no_narrowing(corpus, fs_index, monkeypatch):
    monkeypatch.setattr(fs_index, "_warned", True)
    monkeypatch.setattr(fs_index, "MAX_DIRTY_FILES", 0)
    fs_index.refresh()
    claude_session(corpus, "delta", "c9", 9, [("user", "nothing relevant")])
    assert fs_index.candidate_sessions(["needle"], "any", ALL_WS) is None


def test_scoped_search_loads_only_candidate_sessions(corpus, fs_index, monkeypatch):
    expected = _search_with(fs_store, monkeypatch, "needle", project="alpha")
    fs_index.refresh()
    loaded = []
    real = fs_index.load_session_file
    monkeypatch.setattr(fs_index, "load_session_file",
                        lambda path, agent: loaded.append(path) or real(path, agent))
    assert _search_with(fs_index, monkeypatch, "needle", project="alpha") == expected
    assert loaded == [_path(corpus, C1)]  # c2 is in alpha too, but has no "needle"


def test_candidates_select_rowids_not_file_id(corpus, fs_index, monkeypatch):
    fs_index.refresh()
    c3 = _path(corpus, ".claude/projects/-w-beta/c3.jsonl")
    with monkeypatch.context() as patch:
        statements = _trace_sql(patch)
        assert fs_index.candidate_sessions(["needle"], "any", ["/w/alpha"]) == {_path(corpus, C1)}
        assert fs_index.candidate_sessions(["CAFÉ"], "any", ALL_WS) == {c3}  # pj_count path
    selects = [s for s in statements if "FROM messages" in s]
    assert len(selects) == 2, statements
    assert all(s.startswith("SELECT rowid FROM messages WHERE") for s in selects), selects
    assert not [s for s in statements if "file_id" in s]


# --- Schema v2: messages addressed by rowid range (pj-8qn.18) ---

_V1_SCHEMA = """
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
INSERT INTO meta VALUES ('schema_version', '1');
"""


def _append_claude(home, rel, session_id, text):
    path = sessions_home(home) / rel
    with path.open("a") as f:
        f.write(json.dumps({"type": "user", "sessionId": session_id, "cwd": "/w/alpha",
                            "timestamp": "2026-09-01T00:01:00Z",
                            "message": {"role": "user", "content": text}}) + "\n")
    return str(path)


def _assert_rowid_ranges_exact():
    """Every file's messages are exactly the rowids first_rowid..last_rowid."""
    from pj import paths
    conn = sqlite3.connect(str(paths.index_path()))
    try:
        by_file: dict = {}
        for rowid, file_id in conn.execute("SELECT rowid, file_id FROM messages"):
            by_file.setdefault(file_id, []).append(rowid)
        files = conn.execute("SELECT id, first_rowid, last_rowid FROM files").fetchall()
    finally:
        conn.close()
    assert set(by_file) <= {file_id for file_id, _, _ in files}  # no orphans
    for file_id, first, last in files:
        expected = [] if first is None else list(range(first, last + 1))
        assert sorted(by_file.get(file_id, [])) == expected, file_id
    _assert_tree_ranges_exact()


def _assert_tree_ranges_exact():
    """tree_messages is exactly the union of disjoint t_first_rowid..t_last_rowid ranges."""
    from pj import paths
    conn = sqlite3.connect(str(paths.index_path()))
    try:
        rowids = sorted(r for (r,) in conn.execute("SELECT rowid FROM tree_messages"))
        ranges = conn.execute("SELECT t_first_rowid, t_last_rowid FROM files"
                              " WHERE t_first_rowid IS NOT NULL").fetchall()
    finally:
        conn.close()
    assert rowids == sorted(r for first, last in ranges for r in range(first, last + 1))


def _trace_sql(monkeypatch):
    """Collect every statement fs_index runs from now on."""
    statements, real = [], sqlite3.connect

    def connect(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(sqlite3, "connect", connect)
    return statements


def _assert_messages_deleted_by_range(statements):
    deletes = [s for s in statements if s.lstrip().upper().startswith("DELETE FROM MESSAGES")]
    assert deletes, statements
    assert all("rowid BETWEEN" in s for s in deletes), deletes
    assert not [s for s in statements if "file_id IN" in s or "file_id =" in s]


def test_refresh_and_write_back_delete_messages_by_rowid_range(corpus, fs_index, monkeypatch):
    fs_index.refresh()
    _append_claude(corpus, ".claude/projects/-w-alpha/c1.jsonl", "c1", "appended-term-qq")
    (sessions_home(corpus) / ".claude/projects/-w-alpha/c2.jsonl").unlink()
    with monkeypatch.context() as patch:
        statements = _trace_sql(patch)
        assert fs_index.refresh()["updated"] == 1
    _assert_messages_deleted_by_range(statements)

    _append_claude(corpus, ".claude/projects/-w-alpha/c1.jsonl", "c1", "second-term-qq")
    with monkeypatch.context() as patch:
        statements = _trace_sql(patch)
        assert_same_as_fs(fs_index, "second-term-qq")  # writes c1 back
    _assert_messages_deleted_by_range(statements)
    _assert_index_is_used(fs_index, monkeypatch)


def test_v1_index_falls_back_until_refresh_rebuilds_it_as_current(corpus, fs_index, monkeypatch, capsys):
    from pj import paths
    monkeypatch.setattr(fs_index, "_warned", False)
    paths.index_path().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(paths.index_path()))
    conn.executescript(_V1_SCHEMA)
    conn.close()
    assert_same_as_fs(fs_index, "needle")
    assert capsys.readouterr().err.count("session index unavailable") == 1
    assert fs_index.refresh()["rebuilt"] is True
    conn = sqlite3.connect(str(paths.index_path()))
    try:
        assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone() == \
            (fs_index.SCHEMA_VERSION,)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    finally:
        conn.close()
    assert {"first_rowid", "last_rowid", "model"} <= columns
    _assert_rowid_ranges_exact()
    _assert_index_is_used(fs_index, monkeypatch)


def test_rowid_ranges_stay_exact_across_every_write(corpus, fs_index):
    fs_index.refresh()  # add
    _assert_rowid_ranges_exact()
    _append_claude(corpus, ".claude/projects/-w-alpha/c1.jsonl", "c1", "appended-term-qq")
    assert fs_index.refresh()["updated"] == 1  # update
    _assert_rowid_ranges_exact()
    (sessions_home(corpus) / ".claude/projects/-w-alpha/c2.jsonl").unlink()
    assert fs_index.refresh()["removed"] == 1  # delete
    _assert_rowid_ranges_exact()
    _append_claude(corpus, ".claude/projects/-w-alpha/c1.jsonl", "c1", "second-term-qq")
    claude_session(corpus, "delta", "c9", 9, [("user", "second-term-qq"), ("assistant", "ok")])
    assert_same_as_fs(fs_index, "second-term-qq")  # write-back of an update and an add
    _assert_rowid_ranges_exact()
    path = str(sessions_home(corpus) / ".codex/sessions/2026/09/02/rollout-x1.jsonl")
    _tamper("UPDATE messages SET content = 'tampered' WHERE ord = 0 AND file_id ="
            " (SELECT id FROM files WHERE path = ?)", path)
    assert fs_index.refresh(verify=True)["repaired"] == 1  # verify repair
    _assert_rowid_ranges_exact()
    assert_same_as_fs(fs_index, "needle")


def test_files_store_the_metadata_model(corpus, fs_index):
    from pj import paths
    path = _write(sessions_home(corpus) / ".codex/sessions/2026/09/07/rollout-x7.jsonl", [
        {"type": "session_meta", "timestamp": "2026-09-07T00:00:00Z",
         "payload": {"id": "x7", "cwd": "/w/gamma"}},
        {"type": "turn_context", "timestamp": "2026-09-07T00:00:01Z",
         "payload": {"cwd": "/w/gamma", "model": "gpt-test-1"}},
        {"type": "response_item", "timestamp": "2026-09-07T00:00:02Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "model test"}]}}])
    assert codex.parse_metadata(str(path)).model == "gpt-test-1"
    fs_index.refresh()
    conn = sqlite3.connect(str(paths.index_path()))
    conn.row_factory = sqlite3.Row
    try:
        rows = {r["path"]: r for r in conn.execute("SELECT * FROM files")}
    finally:
        conn.close()
    assert rows[str(path)]["model"] == "gpt-test-1"
    for row_path, row in rows.items():
        parser = codex if "/.codex/" in row_path else claude_code
        assert fs_index._stored_metadata(row) == parser.parse_metadata(row_path), row_path


@pytest.mark.parametrize("sort", ["newest", "oldest", "relevance"])
@pytest.mark.parametrize("query", ["lazyterm", "lazytermi"])  # MATCH and exact pj_count paths
def test_message_text_is_read_only_for_files_the_search_reaches(corpus, fs_index, monkeypatch,
                                                                query, sort):
    for day in range(10, 18):
        claude_session(corpus, "lazy", f"z{day}", day, [("user", "no match here"),
                                                         ("assistant", f"lazytermi {day}")])
    fs_index.refresh()
    expected = fs_store.search_content(query, limit=3, sort=sort)
    reached, read = [], []
    real_session, real_messages = fs_index._stored_session, fs_index._matching_messages

    def stored_session(conn, row, *args):
        reached.append(row["id"])
        return real_session(conn, row, *args)

    def matching_messages(conn, row, *args):
        read.append(row["id"])
        return real_messages(conn, row, *args)
    monkeypatch.setattr(fs_index, "_stored_session", stored_session)
    monkeypatch.setattr(fs_index, "_matching_messages", matching_messages)
    statements = _trace_sql(monkeypatch)
    _forbid_parsing(monkeypatch)
    assert fs_index.search_content(query, limit=3, sort=sort) == expected
    # 8 files match. newest/oldest stop after 3 results; relevance reaches every file.
    assert len(expected) == 3
    assert len(read) == len(set(read)) == (8 if sort == "relevance" else 3)
    assert set(read) <= set(reached)
    reads = [s for s in statements if "content FROM messages" in s]  # selects message text
    assert len(reads) == len(read) and all("rowid BETWEEN" in s for s in reads), reads


# --- Metadata calls served from the index (pj-8qn.20) ---

META_WS = ["/w/alpha", "/w/beta", "/w/gamma", "/w/missing", "(no workspace)"]
X7 = ".codex/sessions/2026/09/07/rollout-x7.jsonl"
M1 = ".claude/projects/-w-alpha/m1.jsonl"


@pytest.fixture
def meta_corpus(corpus):
    """corpus plus sessions with a model (both agents) and a kimi directory session."""
    _write(sessions_home(corpus) / X7, [
        {"type": "session_meta", "timestamp": "2026-09-07T00:00:00Z",
         "payload": {"id": "x7", "cwd": "/w/gamma"}},
        {"type": "turn_context", "timestamp": "2026-09-07T00:00:01Z",
         "payload": {"cwd": "/w/gamma", "model": "gpt-test-1"}},
        {"type": "response_item", "timestamp": "2026-09-07T00:00:02Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "model test"}]}}])
    _write(sessions_home(corpus) / M1, [
        {"type": "user", "sessionId": "m1", "cwd": "/w/alpha", "uuid": "m1-0", "parentUuid": None,
         "timestamp": "2026-09-08T00:00:00Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "which model"}]}},
        {"type": "assistant", "sessionId": "m1", "cwd": "/w/alpha", "uuid": "m1-1",
         "parentUuid": "m1-0", "timestamp": "2026-09-08T00:10:00Z",
         "message": {"role": "assistant", "model": "claude-test-1",
                     "content": [{"type": "text", "text": "this one"}]}}])
    kimi_session(corpus, "k1", ["kimi needle"])
    return corpus


def _metadata_results(store):
    # Sessions start on 2026-09-01..08: this window keeps days 5 onwards.
    split = (datetime.now(timezone.utc) - datetime(2026, 9, 4, tzinfo=timezone.utc)).days
    return {
        "list_projects": store.list_projects(),
        "list_projects_detail": store.list_projects(detail=True),
        "recent_all": store.recent_session_counts(days=100000),
        "recent_split": store.recent_session_counts(days=split),
        "sessions_by_project": store.sessions_by_project(META_WS),
        "project_sessions": store.project_sessions("/w/alpha"),
        "project_sessions_limit": store.project_sessions("/w/gamma", limit=1),
        "overview": store.session_overview(),
    }


def test_meta_fixture_exercises_fs_store_metadata(meta_corpus):
    """Characterize the oracle, so the metadata parity tests are not vacuous."""
    r = _metadata_results(fs_store)
    models = {p["path"]: p["models"] for p in r["list_projects_detail"]}
    assert models["/w/gamma"] == ["gpt-test-1"] and models["/w/alpha"] == ["claude-test-1"]
    assert "(no workspace)" in models                                  # kimi
    assert any(p["total_duration_secs"] for p in r["list_projects_detail"])
    assert r["recent_split"] and r["recent_split"] != r["recent_all"]
    assert [s["session_id"] for s in r["project_sessions"]] == ["m1", "c2", "c1"]
    assert r["project_sessions"][0]["model"] == "claude-test-1"
    assert [s["session_id"] for s in r["project_sessions_limit"]] == ["x7"]
    assert "/w/missing" not in r["sessions_by_project"]
    assert r["sessions_by_project"]["/w/alpha"][0]["source_path"] == _path(meta_corpus, M1)
    overview = r["overview"]
    assert set(overview["by_agent"]) >= {"claude_code", "codex", "kimi"}
    assert sum(a["sessions"] for a in overview["by_agent"].values()) == \
        sum(p["session_count"] for p in r["list_projects"])
    assert overview["workspaces"] == {
        p["path"]: {"sessions": p["session_count"], "newest": p["last_active"]}
        for p in r["list_projects"]}


def test_metadata_calls_match_fs_store_exactly(meta_corpus, fs_index):
    fs_index.refresh()
    assert _metadata_results(fs_index) == _metadata_results(fs_store)


def test_metadata_calls_match_fs_store_with_new_and_deleted_files(meta_corpus, fs_index, monkeypatch):
    fs_index.refresh()
    new = str(claude_session(meta_corpus, "alpha", "c9", 9, [("user", "new session")]))
    (sessions_home(meta_corpus) / C1).unlink()
    expected = _metadata_results(fs_store)
    assert [s["session_id"] for s in expected["project_sessions"]] == ["c9", "m1", "c2"]
    parsed = []
    real = claude_code.parse_metadata
    monkeypatch.setattr(claude_code, "parse_metadata",
                        lambda path: parsed.append(path) or real(path))
    assert _metadata_results(fs_index) == expected
    assert set(parsed) == {new}  # only the dirty file is parsed


def test_metadata_calls_use_the_index(meta_corpus, fs_index, monkeypatch):
    """After refresh() no file is dirty, so the metadata calls must not parse at all."""
    expected = _metadata_results(fs_store)
    fs_index.refresh()
    _forbid_parsing(monkeypatch)
    assert _metadata_results(fs_index) == expected


@pytest.mark.parametrize("unusable", ["missing", "too_stale"])
def test_metadata_calls_fall_back_to_fs_store(meta_corpus, fs_index, monkeypatch, capsys, unusable):
    monkeypatch.setattr(fs_index, "_warned", False)
    if unusable == "too_stale":
        fs_index.refresh()
        monkeypatch.setattr(fs_index, "MAX_DIRTY_FILES", 0)
        claude_session(meta_corpus, "delta", "c9", 9, [("user", "new session")])
    assert _metadata_results(fs_index) == _metadata_results(fs_store)
    assert "pj index refresh" in capsys.readouterr().err


def test_scoped_search_parses_no_clean_file(meta_corpus, fs_index, monkeypatch):
    """discover's list_projects, sessions_by_project and the candidate's
    load_session_file all answer from the index: nothing is parsed."""
    from pj import paths
    expected = _search_with(fs_store, monkeypatch, "needle", project="alpha")
    assert expected
    fs_index.refresh()
    paths.cache_file().unlink()  # discover must call list_projects, not read its cache
    parsed = []

    def recording(real):
        return lambda path, *args, **kwargs: parsed.append(path) or real(path, *args, **kwargs)
    for parser in (claude_code, codex, kimi):
        monkeypatch.setattr(parser, "parse_metadata", lambda *a, **k: pytest.fail("parse_metadata"))
        monkeypatch.setattr(parser, "parse_session", recording(parser.parse_session))
    monkeypatch.setattr(claude_code, "parse_session_tree", recording(claude_code.parse_session_tree))
    assert _search_with(fs_index, monkeypatch, "needle", project="alpha") == expected
    assert parsed == []


# --- load_session_file from the index: schema v3 (pj-8qn.25) ---

B1 = ".claude/projects/-w-alpha/b1.jsonl"


def branched_session(home):
    """A claude session with an abandoned fork, a tool call and its result.

    parse_session keeps every line; parse_session_tree keeps the active
    branch (last child at the fork) and tags it, so the two views differ.
    """
    def event(uuid, parent, role, content, second, **extra):
        return {"type": role, "sessionId": "b1", "cwd": "/w/alpha", "uuid": uuid,
                "parentUuid": parent, "timestamp": f"2026-09-09T00:00:{second:02d}Z",
                "message": {"role": role, "content": content, **extra}}
    return _write(sessions_home(home) / B1, [
        event("b1-0", None, "user", [{"type": "text", "text": "branch needle start"}], 0),
        event("b1-1", "b1-0", "assistant", [{"type": "text", "text": "abandoned reply"}], 1,
              model="claude-old"),
        event("b1-2", "b1-0", "assistant",
              [{"type": "text", "text": "active needle reply"},
               {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}],
              2, model="claude-test-2"),
        event("b1-3", "b1-2", "user",
              [{"type": "tool_result", "tool_use_id": "t1", "content": "file.txt"}], 3),
    ])


def _index_tables():
    from pj import paths
    conn = sqlite3.connect(str(paths.index_path()))
    try:
        return {name for (name,) in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()


def test_v2_index_is_outdated_and_rebuilt(corpus, fs_index, monkeypatch, capsys):
    monkeypatch.setattr(fs_index, "_warned", False)
    fs_index.refresh()
    from pj import paths
    with sqlite3.connect(str(paths.index_path())) as conn:  # what a v2 index lacks
        conn.execute("DROP TABLE tree_messages")
        conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
    conn.close()
    assert_same_as_fs(fs_index, "needle")
    assert capsys.readouterr().err.count("session index unavailable") == 1
    assert fs_index.refresh()["rebuilt"] is True
    assert "tree_messages" in _index_tables()
    _assert_index_is_used(fs_index, monkeypatch)


def test_verify_repairs_a_tampered_tree_row(corpus, fs_index):
    branched_session(corpus)
    fs_index.refresh()
    path = _path(corpus, B1)
    assert fs_index.refresh(verify=True)["mismatched"] == []
    _tamper("UPDATE tree_messages SET content = 'tampered'"
            " WHERE rowid = (SELECT t_first_rowid FROM files WHERE path = ?)", path)
    first = fs_index.refresh(verify=True)
    assert (first["mismatched"], first["repaired"]) == ([path], 1)
    assert fs_index.refresh(verify=True)["mismatched"] == []
    _assert_rowid_ranges_exact()


@pytest.mark.parametrize("column", ["branch", "uuid", "parent_uuid", "author", "created_at"])
def test_verify_repairs_a_tampered_tree_message_field(corpus, fs_index, column):
    branched_session(corpus)
    fs_index.refresh()
    path = _path(corpus, B1)
    _tamper(f"UPDATE tree_messages SET {column} = 'tampered'"
            " WHERE rowid = (SELECT t_last_rowid FROM files WHERE path = ?)", path)
    assert fs_index.refresh(verify=True)["mismatched"] == [path]


@pytest.mark.parametrize("sql", [
    "UPDATE files SET t_model = 'tampered' WHERE path = ?",
    "UPDATE files SET s_model = 'tampered' WHERE path = ?",
    "UPDATE messages SET created_at = 1 WHERE file_id = (SELECT id FROM files WHERE path = ?)",
])
def test_verify_repairs_tampered_load_fields(corpus, fs_index, sql):
    branched_session(corpus)
    fs_index.refresh()
    path = _path(corpus, B1) if "t_model" in sql else _path(corpus, X1)
    _tamper(sql, path)
    assert fs_index.refresh(verify=True)["mismatched"] == [path]


def _all_session_files():
    return [(path, parser.agent_slug) for root, parser in fs_store._configured_roots()
            for path in parser.list_sessions(root)]


def _loads(store):
    return {path: store.load_session_file(path, agent) for path, agent in _all_session_files()}


def test_load_corpus_exercises_both_claude_views(meta_corpus):
    """Characterize the oracle: the branched file's tree differs from parse_session."""
    branched_session(meta_corpus)
    agents = {agent for _, agent in _all_session_files()}
    assert agents == {"claude_code", "codex", "kimi"}
    full = fs_store.load_session_file(_path(meta_corpus, B1), "claude_code")
    assert [m["uuid"] for m in full["messages"]] == ["b1-0", "b1-2", "b1-3"]
    assert {m["branch"] for m in full["messages"]} == {"active"}
    assert "[Tool: Bash]" in full["messages"][1]["content"]
    assert len(claude_code.parse_session(_path(meta_corpus, B1)).messages) == 4


def test_load_session_file_matches_fs_store_for_every_file(meta_corpus, fs_index, monkeypatch):
    branched_session(meta_corpus)
    expected = _loads(fs_store)
    assert all(expected.values())
    fs_index.refresh()
    _forbid_parsing(monkeypatch)  # clean files: the stored dicts, no parse
    assert _loads(fs_index) == expected


def test_load_session_file_delegates_for_a_dirty_file(meta_corpus, fs_index, monkeypatch):
    branched_session(meta_corpus)
    fs_index.refresh()
    path = _append_claude(meta_corpus, B1, "b1", "appended after refresh")
    parsed = []
    real = claude_code.parse_session_tree
    monkeypatch.setattr(claude_code, "parse_session_tree",
                        lambda p, **kw: parsed.append(p) or real(p, **kw))
    assert fs_index.load_session_file(path, "claude_code") == \
        fs_store.load_session_file(path, "claude_code")
    assert parsed == [path, path]  # fresh parse, then fs_store's oracle


def test_load_session_file_delegates_for_unknown_paths_and_agents(corpus, fs_index):
    fs_index.refresh()
    c1 = _path(corpus, C1)
    assert fs_index.load_session_file(c1, "codex") == fs_store.load_session_file(c1, "codex")
    assert fs_index.load_session_file(c1, "nope") is None
    assert fs_index.load_session_file(c1 + ".missing", "claude_code") is None


def test_load_session_file_falls_back_without_an_index(corpus, fs_index, monkeypatch):
    monkeypatch.setattr(fs_index, "_warned", True)
    c1 = _path(corpus, C1)
    assert fs_index.load_session_file(c1, "claude_code") == \
        fs_store.load_session_file(c1, "claude_code")


def test_written_back_claude_file_serves_its_tree(meta_corpus, fs_index, monkeypatch):
    branched_session(meta_corpus)
    fs_index.refresh()
    path = _append_claude(meta_corpus, B1, "b1", "write-back-term-qq")
    assert_same_as_fs(fs_index, "write-back-term-qq")  # writes b1 back, tree included
    expected = fs_store.load_session_file(path, "claude_code")
    _forbid_parsing(monkeypatch)
    assert fs_index.load_session_file(path, "claude_code") == expected
    _assert_rowid_ranges_exact()


def test_write_back_never_stores_a_claude_file_without_its_tree(corpus, fs_index, monkeypatch):
    fs_index.refresh()
    path = _append_claude(corpus, C1, "c1", "no-tree-term-qq")
    with monkeypatch.context() as patch:
        patch.setattr(claude_code, "parse_session_tree", lambda *a, **k: None)
        assert fs_index.search_content("no-tree-term-qq")  # parses c1 for write-back
    from pj import paths
    conn = sqlite3.connect(str(paths.index_path()))
    try:
        (size,) = conn.execute("SELECT size FROM files WHERE path = ?", (path,)).fetchone()
    finally:
        conn.close()
    assert size != os.path.getsize(path)  # still dirty: not written back
