"""Acceptance contract for an opt-in session search index (pj-nd2).

The index is only worth its complexity if it is a drop-in for fs_store's
content search: same results, same freshness, and a safe fallback.  These
tests are that contract.  They are strict-xfail until pj/fs_index.py exists;
remove the mark when they pass.

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
import stat
from pathlib import Path

import pytest

from pj import fs_store, session_store
from pj.parsers import claude_code, codex, kimi

pending = pytest.mark.xfail(
    strict=True, reason="pj-nd2: session index not implemented yet")

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


def assert_same_as_fs(fs_index, query, *, limit=20, sort="newest"):
    expected = fs_store.search_content(query, limit=limit, sort=sort)
    assert fs_index.search_content(query, limit=limit, sort=sort) == expected, query


@pending
@pytest.mark.parametrize("sort", ["newest", "oldest", "relevance"])
@pytest.mark.parametrize("query", QUERIES)
def test_search_content_matches_fs_store_exactly(corpus, fs_index, query, sort):
    fs_index.refresh()
    assert_same_as_fs(fs_index, query, sort=sort)


@pending
@pytest.mark.parametrize("limit", [1, 2, 3, 100])
def test_limit_cuts_at_the_same_session_as_fs_store(corpus, fs_index, limit):
    fs_index.refresh()
    assert_same_as_fs(fs_index, "needle", limit=limit)
    assert_same_as_fs(fs_index, "alpha", limit=limit)


@pending
def test_new_session_is_found_without_an_explicit_refresh(corpus, fs_index):
    fs_index.refresh()
    claude_session(corpus, "delta", "c9", 9, [("user", "fresh-needle-xyz")])
    assert [h["session_id"] for h in fs_index.search_content("fresh-needle-xyz")] == ["c9"]


@pending
def test_appended_message_is_found_without_an_explicit_refresh(corpus, fs_index):
    fs_index.refresh()
    path = sessions_home(corpus) / ".claude/projects/-w-alpha/c1.jsonl"
    with path.open("a") as f:
        f.write(json.dumps({"type": "user", "sessionId": "c1", "cwd": "/w/alpha",
                            "timestamp": "2026-09-01T00:01:00Z",
                            "message": {"role": "user", "content": "appended-term-qq"}}) + "\n")
    assert_same_as_fs(fs_index, "appended-term-qq")
    assert fs_index.search_content("appended-term-qq")


@pending
def test_deleted_session_disappears(corpus, fs_index):
    fs_index.refresh()
    (sessions_home(corpus) / ".claude/projects/-w-alpha/c1.jsonl").unlink()
    assert_same_as_fs(fs_index, "needle")


@pending
def test_missing_index_falls_back_to_fs_results(corpus, fs_index):
    from pj import paths
    assert not paths.index_path().exists()
    assert_same_as_fs(fs_index, "needle")


@pending
def test_corrupt_index_falls_back_to_fs_results(corpus, fs_index):
    from pj import paths
    fs_index.refresh()
    paths.index_path().write_bytes(b"not a database")
    assert_same_as_fs(fs_index, "needle")


@pending
def test_index_file_is_owner_only_and_under_data_dir(corpus, fs_index):
    from pj import paths
    fs_index.refresh()
    path = paths.index_path()
    assert path.parent == paths.data_dir()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


@pending
def test_refresh_is_idempotent(corpus, fs_index):
    fs_index.refresh()
    first = fs_index.search_content("needle", sort="relevance")
    fs_index.refresh()
    assert fs_index.search_content("needle", sort="relevance") == first


@pending
def test_backend_env_selects_the_index(home, monkeypatch, fs_index):
    monkeypatch.setenv("PJ_BACKEND", "index")
    monkeypatch.setattr(session_store, "_store", None)
    assert session_store.get_store() is fs_index


@pending
def test_index_implements_the_whole_session_store_protocol(fs_index):
    for name in ("available", "list_projects", "recent_session_counts", "project_sessions",
                 "session_details", "search_sessions", "search_content", "get_session",
                 "sessions_by_project", "load_session_file", "candidate_sessions"):
        assert callable(getattr(fs_index, name)), name
