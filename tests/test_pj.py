"""Tests for pj Phase 1: envelope, cass_facade, state, discover, cache, cli, pretty."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

import pj.envelope as envelope
import pj.state as state
import pj.cass_facade as cass_facade
import pj.cache as cache
import pj.discover as discover
import pj.pretty as pretty
import pj.cli as cli


# --- envelope ---

def test_envelope_ok():
    env = envelope.ok([{"id": "abc"}], total=1, offset=0, limit=20)
    assert env["success"] is True
    assert env["data"] == [{"id": "abc"}]
    assert env["meta"]["total"] == 1


def test_envelope_err():
    env = envelope.err("something broke", source="cass")
    assert env["success"] is False
    assert env["data"] == []
    assert env["meta"]["error"] == "something broke"
    assert env["meta"]["source"] == "cass"


def test_envelope_to_json():
    env = envelope.ok([], total=0)
    s = envelope.to_json(env)
    parsed = json.loads(s)
    assert parsed["success"] is True


# --- state ---

def test_state_active():
    ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    assert state.derive(ts) == "active"


def test_state_stale():
    ts = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
    assert state.derive(ts) == "stale"


def test_state_dormant():
    ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    assert state.derive(ts) == "dormant"


def test_state_dormant_none():
    assert state.derive(None) == "dormant"


def test_state_archived():
    ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert state.derive(ts, archived=True) == "archived"


def test_state_blocked():
    ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert state.derive(ts, blocked=True) == "blocked"


def test_state_boundary_7_days():
    ts = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    assert state.derive(ts) == "active"


def test_state_boundary_8_days():
    ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    assert state.derive(ts) == "stale"


def test_state_boundary_30_days():
    ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    assert state.derive(ts) == "stale"


def test_state_boundary_31_days():
    ts = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    assert state.derive(ts) == "dormant"


# --- cass_facade with synthetic DB ---

def _create_test_db(path: Path):
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE agents (id INTEGER PRIMARY KEY, slug TEXT);
        CREATE TABLE workspaces (id INTEGER PRIMARY KEY, path TEXT);
        CREATE TABLE conversations (
            id TEXT PRIMARY KEY, agent_id INTEGER, workspace_id INTEGER,
            started_at INTEGER, title TEXT,
            source_path TEXT, source_id TEXT, origin_host TEXT
        );
    """)
    conn.execute("INSERT INTO agents VALUES (1, 'claude')")
    conn.execute("INSERT INTO agents VALUES (2, 'codex')")
    conn.execute("INSERT INTO workspaces VALUES (1, '/home/user/project-a')")
    conn.execute("INSERT INTO workspaces VALUES (2, '/home/user/project-b')")
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    old_ms = int((datetime.now(timezone.utc) - timedelta(days=20)).timestamp() * 1000)
    conn.execute("INSERT INTO conversations VALUES ('c1', 1, 1, ?, 't1', '', '', '')", (now_ms,))
    conn.execute("INSERT INTO conversations VALUES ('c2', 1, 1, ?, 't2', '', '', '')", (now_ms - 3600000,))
    conn.execute("INSERT INTO conversations VALUES ('c3', 2, 1, ?, 't3', '', '', '')", (now_ms - 7200000,))
    conn.execute("INSERT INTO conversations VALUES ('c4', 1, 2, ?, 't4', '', '', '')", (old_ms,))
    conn.commit()
    conn.close()


def test_cass_facade_list_projects():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_search.db"
        _create_test_db(db)
        with mock.patch.object(cass_facade, "db_path", return_value=db):
            projects = cass_facade.list_projects()

    assert len(projects) == 2
    proj_a = next(p for p in projects if p["path"] == "/home/user/project-a")
    proj_b = next(p for p in projects if p["path"] == "/home/user/project-b")
    assert proj_a["session_count"] == 3
    assert sorted(proj_a["agents"]) == ["claude", "codex"]
    assert proj_b["session_count"] == 1
    assert proj_b["agents"] == ["claude"]
    assert proj_a["last_active"] > proj_b["last_active"]


def test_cass_facade_no_db():
    with mock.patch.object(cass_facade, "db_path", return_value=None):
        assert cass_facade.list_projects() == []


# --- discover ---

def test_discover_with_cass():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake_projects = [
        {"path": "/tmp/proj-x", "agents": ["claude"], "session_count": 5, "last_active": now_iso},
    ]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake_projects), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch.object(discover, "ANNOTATIONS_PATH", Path("/nonexistent")):
        projects, total = discover.discover()

    assert total == 1
    assert projects[0]["name"] == "proj-x"
    assert projects[0]["state"] == "active"
    assert projects[0]["id"] == discover.project_id("/tmp/proj-x")


def test_discover_state_filter():
    now_iso = datetime.now(timezone.utc).isoformat()
    old_iso = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    fake = [
        {"path": "/tmp/active", "agents": ["claude"], "session_count": 1, "last_active": now_iso},
        {"path": "/tmp/dormant", "agents": ["claude"], "session_count": 1, "last_active": old_iso},
    ]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch.object(discover, "ANNOTATIONS_PATH", Path("/nonexistent")):
        projects, total = discover.discover(state_filter="active")

    assert total == 1
    assert projects[0]["name"] == "active"


def test_discover_pagination():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [
        {"path": f"/tmp/p{i}", "agents": ["claude"], "session_count": 1, "last_active": now_iso}
        for i in range(5)
    ]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch.object(discover, "ANNOTATIONS_PATH", Path("/nonexistent")):
        projects, total = discover.discover(limit=2, offset=1)

    assert total == 5
    assert len(projects) == 2


def test_discover_uses_cache():
    cached_projects = [{"id": "cached01", "name": "cached", "state": "active"}]
    with mock.patch.object(cache, "load", return_value=cached_projects):
        projects, total = discover.discover()

    assert total == 1
    assert projects[0]["id"] == "cached01"


def test_discover_annotations():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/annotated", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    pid = discover.project_id("/tmp/annotated")
    ann_lines = [
        json.dumps({"type": "priority", "project_id": pid, "value": "high"}),
        json.dumps({"type": "note", "project_id": pid, "text": "blocked: waiting on API"}),
    ]

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("\n".join(ann_lines) + "\n")
        ann_path = Path(f.name)

    try:
        with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
             mock.patch.object(cache, "load", return_value=None), \
             mock.patch.object(cache, "save"), \
             mock.patch.object(discover, "ANNOTATIONS_PATH", ann_path):
            projects, total = discover.discover()

        assert projects[0]["priority"] == "high"
        assert projects[0]["state"] == "blocked"
    finally:
        os.unlink(ann_path)


# --- cache ---

def test_cache_round_trip():
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_file = Path(tmpdir) / "project_index.json"
        with mock.patch.object(cache, "CACHE_FILE", cache_file), \
             mock.patch.object(cache, "CACHE_DIR", Path(tmpdir)), \
             mock.patch.object(cache, "_signatures", return_value={"test": 1.0}):
            assert cache.load() is None
            cache.save([{"id": "abc"}])
            loaded = cache.load()
            assert loaded == [{"id": "abc"}]


def test_cache_invalidation():
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_file = Path(tmpdir) / "project_index.json"
        sig = {"v": 1.0}
        with mock.patch.object(cache, "CACHE_FILE", cache_file), \
             mock.patch.object(cache, "CACHE_DIR", Path(tmpdir)), \
             mock.patch.object(cache, "_signatures", return_value=sig):
            cache.save([{"id": "abc"}])

        sig2 = {"v": 2.0}
        with mock.patch.object(cache, "CACHE_FILE", cache_file), \
             mock.patch.object(cache, "_signatures", return_value=sig2):
            assert cache.load() is None


# --- pretty ---

def test_pretty_no_projects(capsys):
    pretty.print_projects([], 0, 0, 20)
    assert "No projects" in capsys.readouterr().out


def test_pretty_with_projects(capsys):
    now_iso = datetime.now(timezone.utc).isoformat()
    projects = [
        {"id": "abcd1234", "state": "active", "name": "my-project",
         "agents": ["claude", "codex"], "session_count": 42,
         "priority": "high", "last_active": now_iso},
    ]
    pretty.print_projects(projects, 1, 0, 20)
    out = capsys.readouterr().out
    assert "my-project" in out
    assert "active" in out
    assert "claude,codex" in out
    assert "1-1 of 1" in out


# --- cli ---

def test_cli_list_json(capsys):
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/cli-test", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch.object(discover, "ANNOTATIONS_PATH", Path("/nonexistent")):
        cli.main(["list"])

    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["meta"]["total"] == 1
    assert parsed["data"][0]["name"] == "cli-test"


def test_cli_list_pretty(capsys):
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/cli-test", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch.object(discover, "ANNOTATIONS_PATH", Path("/nonexistent")):
        cli.main(["list", "--pretty"])

    out = capsys.readouterr().out
    assert "cli-test" in out


def test_cli_list_state_filter(capsys):
    now_iso = datetime.now(timezone.utc).isoformat()
    old_iso = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    fake = [
        {"path": "/tmp/active", "agents": ["claude"], "session_count": 1, "last_active": now_iso},
        {"path": "/tmp/dormant", "agents": ["claude"], "session_count": 1, "last_active": old_iso},
    ]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch.object(discover, "ANNOTATIONS_PATH", Path("/nonexistent")):
        cli.main(["list", "--state", "dormant"])

    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["meta"]["total"] == 1
    assert parsed["data"][0]["name"] == "dormant"


def test_cli_no_command(capsys):
    try:
        cli.main([])
    except SystemExit as e:
        assert e.code == 1


# --- project_id ---

def test_project_id_deterministic():
    assert discover.project_id("/tmp/foo") == discover.project_id("/tmp/foo")
    assert discover.project_id("/tmp/foo") != discover.project_id("/tmp/bar")
    assert len(discover.project_id("/tmp/foo")) == 8
