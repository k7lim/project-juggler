"""fs_index searches beyond the gate: index edge cases (pj-8qn.6)."""
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
