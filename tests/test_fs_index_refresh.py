"""fs_index.refresh(): the index file's schema, privacy and update rules (pj-8qn.5)."""
import os
import sqlite3
import stat

from pj import paths
from tests.test_fs_index import corpus, fs_index, home  # noqa: F401  (fixtures)


def _meta(key):
    conn = sqlite3.connect(str(paths.index_path()))
    try:
        return conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()


def test_refresh_records_the_schema_version(corpus, fs_index):
    fs_index.refresh()
    assert _meta("schema_version") == (fs_index.SCHEMA_VERSION,)


def test_index_and_lock_files_are_owner_only(corpus, fs_index):
    fs_index.refresh()
    for path in (paths.index_path(), paths.index_path().with_suffix(".lock")):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, path
    assert stat.S_IMODE(os.stat(paths.data_dir()).st_mode) == 0o700


def test_refresh_restores_the_umask(corpus, fs_index):
    before = os.umask(0o022)
    try:
        fs_index.refresh()
        assert os.umask(0o022) == 0o022
    finally:
        os.umask(before)


def _count_parses(monkeypatch):
    from pj.parsers import claude_code, codex
    calls = []
    for parser in (claude_code, codex):
        original = parser.parse_session
        monkeypatch.setattr(parser, "parse_session",
                            lambda path, _orig=original: calls.append(path) or _orig(path))
    return calls


def _rows(sql, *args):
    conn = sqlite3.connect(str(paths.index_path()))
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def test_unchanged_files_are_not_reparsed(corpus, fs_index, monkeypatch):
    first = fs_index.refresh()
    assert (first["added"], first["rebuilt"]) == (6, True)
    calls = _count_parses(monkeypatch)
    second = fs_index.refresh()
    assert calls == []
    assert (second["added"], second["updated"], second["removed"], second["rebuilt"]) == (0, 0, 0, False)


def test_changed_file_is_reparsed_once(corpus, fs_index, monkeypatch):
    from tests.test_fs_index import sessions_home
    fs_index.refresh()
    path = sessions_home(corpus) / ".claude/projects/-w-alpha/c1.jsonl"
    path.write_text(path.read_text() + path.read_text().splitlines()[0] + "\n")
    calls = _count_parses(monkeypatch)
    assert fs_index.refresh()["updated"] == 1
    assert calls == [str(path)]
    assert len(_rows("SELECT id FROM files WHERE path = ?", str(path))) == 1


def test_deleted_file_rows_are_removed(corpus, fs_index):
    from tests.test_fs_index import sessions_home
    fs_index.refresh()
    path = sessions_home(corpus) / ".claude/projects/-w-alpha/c1.jsonl"
    (file_id,), = _rows("SELECT id FROM files WHERE path = ?", str(path))
    path.unlink()
    assert fs_index.refresh()["removed"] == 1
    assert _rows("SELECT id FROM files WHERE path = ?", str(path)) == []
    assert _rows("SELECT rowid FROM messages WHERE file_id = ?", file_id) == []


def test_corrupt_index_is_rebuilt(corpus, fs_index):
    fs_index.refresh()
    paths.index_path().write_bytes(b"not a database")
    assert fs_index.refresh()["rebuilt"] is True
    assert _meta("schema_version") == (fs_index.SCHEMA_VERSION,)


def test_status_of_a_missing_index_creates_nothing(home, fs_index):
    info = fs_index.status()
    assert info["exists"] is False and info["path"] == str(paths.index_path())
    assert info["indexed_files"] is None and info["schema_matches"] is False
    assert not paths.data_dir().exists()  # a sensor


def test_status_after_refresh_counts_files_without_parsing(corpus, fs_index, monkeypatch):
    fs_index.refresh()
    parses = _count_parses(monkeypatch)
    info = fs_index.status()
    assert parses == []
    assert info["exists"] is True and info["bytes"] > 0
    assert info["schema_version"] == fs_index.SCHEMA_VERSION and info["schema_matches"] is True
    assert info["indexed_files"] == 6 and info["dirty_files"] == 0
    assert info["built_at"] == _meta("built_at")[0]
    assert info["fts5"] is True


def test_status_counts_a_changed_file_as_dirty(corpus, fs_index):
    from tests.test_fs_index import codex_session
    fs_index.refresh()
    codex_session(corpus, "delta", "x9", 7, [("user", "new")])
    assert fs_index.status()["dirty_files"] == 1


def test_status_reports_a_wrong_schema_version(corpus, fs_index):
    fs_index.refresh()
    conn = sqlite3.connect(str(paths.index_path()))
    with conn:
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
    conn.close()
    info = fs_index.status()
    assert info["schema_version"] == "0" and info["schema_matches"] is False
    assert info["indexed_files"] is None


def test_status_of_a_corrupt_index(corpus, fs_index):
    fs_index.refresh()
    paths.index_path().write_bytes(b"not a database")
    info = fs_index.status()
    assert info["exists"] is True and info["schema_matches"] is False
