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
