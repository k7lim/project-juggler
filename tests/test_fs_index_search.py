"""fs_index searches beyond the gate: index edge cases (pj-8qn.6)."""
import pytest

from pj import fs_store
from tests.test_fs_index import (  # noqa: F401  (fixtures)
    assert_same_as_fs, claude_session, corpus, fs_index, home, sessions_home)


def test_dotted_capital_i_matches_like_python(corpus, fs_index):
    # "İ".lower() is "i" + U+0307; SQLite's trigram folding does not do that.
    claude_session(corpus, "eta", "c7", 7, [("user", "ABİ x")])
    fs_index.refresh()
    assert [h["session_id"] for h in fs_index.search_content("abi")] == ["c7"]
    assert_same_as_fs(fs_index, "abi")


def test_file_listed_under_two_roots_matches_fs_store(corpus, fs_index, monkeypatch):
    # A nested root lists the same codex file twice; fs_store sees both copies.
    nested = sessions_home(corpus) / ".codex/sessions/2026"
    monkeypatch.setenv("PJ_SOURCES", f"codex:{nested}")
    paths = [p for root, parser in fs_store._configured_roots() for p in parser.list_sessions(root)]
    assert len(paths) > len(set(paths))
    fs_index.refresh()
    for sort in ("newest", "oldest", "relevance"):
        assert_same_as_fs(fs_index, "needle", sort=sort)
        assert_same_as_fs(fs_index, "alpha", sort=sort, limit=2)
        assert fs_index.search_sessions("needle", sort=sort) == \
            fs_store.search_sessions("needle", sort=sort)


def test_missing_index_warns_once(corpus, fs_index, monkeypatch, capsys):
    monkeypatch.setattr(fs_index, "_warned", False)
    assert_same_as_fs(fs_index, "needle")
    assert_same_as_fs(fs_index, "needle")
    assert capsys.readouterr().err.count("session index unavailable") == 1


def test_titles_follow_new_and_deleted_files(corpus, fs_index):
    fs_index.refresh()
    claude_session(corpus, "delta", "c9", 9, [("user", "fresh title needle")])
    (sessions_home(corpus) / ".claude/projects/-w-alpha/c1.jsonl").unlink()
    expected = fs_store.search_sessions("needle")
    assert [h["session_id"] for h in expected] == ["c9", "x1"]
    assert fs_index.search_sessions("needle") == expected


# pj-85c: a long-running consumer (the yolobox query server) ran stale pj code
# against a newer index for three weeks, and the only signal was one stderr line.
# A fallback must be machine-readable, every time, not just printed once.

def _break_index(fs_index, how):
    import sqlite3
    from pj import paths
    fs_index.refresh()
    if how == "outdated":
        with sqlite3.connect(str(paths.index_path())) as conn:
            conn.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
        conn.close()
    else:
        paths.index_path().write_bytes(b"not a database")


def _search_meta(capsys, *args):
    import json
    from pj import cli
    cli.main(["search", "needle", *args])
    return json.loads(capsys.readouterr().out)["meta"]


@pytest.mark.parametrize("how", ["outdated", "corrupt"])
def test_index_fallback_is_flagged_in_search_meta(corpus, fs_index, monkeypatch, capsys, how):
    from pj import session_store
    monkeypatch.setattr(session_store, "_store", fs_index)
    fs_index.refresh()
    meta = _search_meta(capsys)
    assert meta["source"] == "index" and "warning" not in meta

    _break_index(fs_index, how)
    for _ in range(2):  # every call, not once per process
        meta = _search_meta(capsys)
        assert meta["source"] == "fs"
        assert "session index unavailable" in meta["warning"]


def test_fs_backend_search_meta_says_fs_without_a_warning(corpus, capsys):
    meta = _search_meta(capsys)
    assert meta["source"] == "fs" and "warning" not in meta


def test_observe_fallbacks_counts_every_fallback_in_this_thread_only(corpus, fs_index):
    import threading
    _break_index(fs_index, "outdated")
    with fs_index.observe_fallbacks() as seen:
        fs_index.search_content("needle")
        fs_index.search_sessions("needle")
        other = threading.Thread(target=fs_index.search_content, args=("needle",))
        other.start()
        other.join()
    assert len(seen) == 2 and all("outdated" in r for r in seen)
    with fs_index.observe_fallbacks() as nested_outer:
        with fs_index.observe_fallbacks() as nested_inner:
            fs_index.search_content("needle")
    assert len(nested_outer) == len(nested_inner) == 1
