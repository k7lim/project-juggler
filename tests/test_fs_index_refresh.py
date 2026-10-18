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
