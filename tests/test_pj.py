"""Tests for pj: envelope, cass_facade, state, discover, cache, cli, pretty, annotate, schedule, search, resume, session_store."""
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
import pj.fs_store as fs_store
import pj.pretty as pretty
import pj.cli as cli
import pj.annotate as annotate
import pj.schedule as schedule
import pj.resume as resume
import pj.search as search_mod
import pj.session_store as session_store
from pj.parsers import codex
from pj.parsers.base import NormalizedMessage, NormalizedSession
import pytest


@pytest.fixture(autouse=True)
def _use_cass_backend():
    """Tests mock cass_facade directly, so ensure it's the active store."""
    old = session_store._store
    session_store._store = cass_facade
    yield
    session_store._store = old


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
            source_path TEXT, source_id TEXT, origin_host TEXT,
            primary_model TEXT, ended_at INTEGER,
            total_input_tokens INTEGER, total_output_tokens INTEGER,
            total_cache_read_tokens INTEGER, total_cache_creation_tokens INTEGER,
            grand_total_tokens INTEGER, user_message_count INTEGER,
            assistant_message_count INTEGER, tool_call_count INTEGER,
            api_call_count INTEGER
        );
    """)
    conn.execute("INSERT INTO agents VALUES (1, 'claude')")
    conn.execute("INSERT INTO agents VALUES (2, 'codex')")
    conn.execute("INSERT INTO workspaces VALUES (1, '/home/user/project-a')")
    conn.execute("INSERT INTO workspaces VALUES (2, '/home/user/project-b')")
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    old_ms = int((datetime.now(timezone.utc) - timedelta(days=20)).timestamp() * 1000)
    conn.execute("INSERT INTO conversations VALUES ('c1', 1, 1, ?, 't1', '', '', '', 'claude-opus-4-6', ?, 100, 5000, 50000, 10000, 65100, 3, 10, 8, 10)", (now_ms, now_ms + 3600000))
    conn.execute("INSERT INTO conversations VALUES ('c2', 1, 1, ?, 't2', '', '', '', 'claude-opus-4-6', ?, 50, 2000, 20000, 5000, 27050, 2, 5, 3, 5)", (now_ms - 3600000, now_ms - 1800000))
    conn.execute("INSERT INTO conversations VALUES ('c3', 2, 1, ?, 't3', '', '', '', 'glm-5.1', ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)", (now_ms - 7200000, now_ms - 3700000))
    conn.execute("INSERT INTO conversations VALUES ('c4', 1, 2, ?, 't4', '', '', '', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)", (old_ms,))
    conn.commit()
    conn.close()


def test_cass_facade_list_projects():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_search.db"
        _create_test_db(db)
        with mock.patch.object(cass_facade, "db_paths", return_value=[db]):
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
    with mock.patch.object(cass_facade, "db_paths", return_value=[]):
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
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")):
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
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")):
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
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")):
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
             mock.patch("pj.discover.annotations_path", return_value=ann_path):
            projects, total = discover.discover()

        assert projects[0]["priority"] == "high"
        assert projects[0]["state"] == "blocked"
    finally:
        os.unlink(ann_path)


# --- cache ---

def test_cache_round_trip():
    with tempfile.TemporaryDirectory() as tmpdir:
        with mock.patch("pj.cache.cache_file", return_value=Path(tmpdir) / "project_index.json"), \
             mock.patch("pj.cache.cache_dir", return_value=Path(tmpdir)), \
             mock.patch.object(cache, "_signatures", return_value={"test": 1.0}):
            assert cache.load() is None
            cache.save([{"id": "abc"}])
            loaded = cache.load()
            assert loaded == [{"id": "abc"}]


def test_cache_invalidation():
    with tempfile.TemporaryDirectory() as tmpdir:
        cf = Path(tmpdir) / "project_index.json"
        sig = {"v": 1.0}
        with mock.patch("pj.cache.cache_file", return_value=cf), \
             mock.patch("pj.cache.cache_dir", return_value=Path(tmpdir)), \
             mock.patch.object(cache, "_signatures", return_value=sig):
            cache.save([{"id": "abc"}])

        sig2 = {"v": 2.0}
        with mock.patch("pj.cache.cache_file", return_value=cf), \
             mock.patch.object(cache, "_signatures", return_value=sig2):
            assert cache.load() is None


def test_fs_store_cache_signature_tracks_session_file_changes():
    """Appending to an existing nested session file should invalidate pj list cache."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        session_file = root / "2026" / "05" / "07" / "rollout-test.jsonl"
        session_file.parent.mkdir(parents=True)
        session_file.write_text(
            json.dumps({"type": "session_meta", "timestamp": "2026-05-07T10:00:00Z"}) + "\n"
        )

        with mock.patch.object(fs_store, "_configured_roots", return_value=[(str(root), codex)]):
            sig1 = fs_store.cache_signatures()
            session_file.write_text(
                session_file.read_text()
                + json.dumps({"type": "event_msg", "timestamp": "2026-05-07T10:05:00Z"})
                + "\n"
            )
            sig2 = fs_store.cache_signatures()

        assert sig1 != sig2


def test_fs_store_search_content_applies_limit_after_recency_sort():
    """Root/file order should not crowd newer matches out of a limited search."""

    class FakeParser:
        agent_slug = "fake"

        def list_sessions(self, root: str) -> list[str]:
            return ["old", "new"]

        def parse_metadata(self, path: str) -> NormalizedSession:
            started_at = 1000 if path == "old" else 2000
            return NormalizedSession(
                session_id=path,
                agent=self.agent_slug,
                source_path=path,
                workspace=f"/tmp/{path}",
                started_at=started_at,
            )

        def parse_session(self, path: str) -> NormalizedSession:
            started_at = 1000 if path == "old" else 2000
            return NormalizedSession(
                session_id=path,
                agent=self.agent_slug,
                source_path=path,
                workspace=f"/tmp/{path}",
                title=path,
                started_at=started_at,
                messages=[
                    NormalizedMessage(
                        idx=0,
                        role="user",
                        content=f"{path} sports discussion",
                    )
                ],
            )

    with mock.patch.object(fs_store, "_configured_roots", return_value=[("/tmp/root", FakeParser())]):
        results = fs_store.search_content("sport", limit=1)

    assert len(results) == 1
    assert results[0]["path"] == "/tmp/new"


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
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")):
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
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")):
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
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")):
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


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])

    assert exc.value.code == 0
    assert "pj 0.2.0" in capsys.readouterr().out


# --- project_id ---

def test_project_id_deterministic():
    assert discover.project_id("/tmp/foo") == discover.project_id("/tmp/foo")
    assert discover.project_id("/tmp/foo") != discover.project_id("/tmp/bar")
    assert len(discover.project_id("/tmp/foo")) == 8


# --- annotate ---

def test_annotate_note():
    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch("pj.annotate.annotations_path", return_value=ann_path):
            event = annotate.note("/tmp/my-proj", "fix the build")

        assert event["type"] == "note"
        assert event["text"] == "fix the build"
        assert event["project_id"] == discover.project_id("/tmp/my-proj")
        assert event["project_path"] == "/tmp/my-proj"
        assert "timestamp" in event

        lines = ann_path.read_text().strip().split("\n")
        assert len(lines) == 1
        assert json.loads(lines[0]) == event


def test_annotate_prioritize():
    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch("pj.annotate.annotations_path", return_value=ann_path):
            event = annotate.prioritize("/tmp/proj", "high")

    assert event["type"] == "priority"
    assert event["value"] == "high"


def test_annotate_prioritize_invalid():
    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch("pj.annotate.annotations_path", return_value=ann_path):
            try:
                annotate.prioritize("/tmp/proj", "urgent")
                assert False, "Should have raised ValueError"
            except ValueError as e:
                assert "urgent" in str(e)

        assert not ann_path.exists()


def test_annotate_archive():
    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch("pj.annotate.annotations_path", return_value=ann_path):
            event = annotate.archive("/tmp/proj")

    assert event["type"] == "archive"
    assert event["project_id"] == discover.project_id("/tmp/proj")


def test_annotate_tag():
    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch("pj.annotate.annotations_path", return_value=ann_path):
            event = annotate.tag("/tmp/proj", "infra")

    assert event["type"] == "tag"
    assert event["tag"] == "infra"


def test_annotate_append_only():
    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch("pj.annotate.annotations_path", return_value=ann_path):
            annotate.note("/tmp/proj", "first")
            annotate.note("/tmp/proj", "second")
            annotate.prioritize("/tmp/proj", "medium")

        lines = ann_path.read_text().strip().split("\n")
        assert len(lines) == 3
        assert json.loads(lines[0])["text"] == "first"
        assert json.loads(lines[1])["text"] == "second"
        assert json.loads(lines[2])["value"] == "medium"


def test_annotate_creates_parent_dirs():
    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "nested" / "dir" / "annotations.jsonl"
        with mock.patch("pj.annotate.annotations_path", return_value=ann_path):
            annotate.note("/tmp/proj", "test")

        assert ann_path.exists()


# --- discover annotation replay integration ---

def test_discover_replays_annotations_integration():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/tagged-proj", "agents": ["claude"], "session_count": 2, "last_active": now_iso}]

    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch("pj.annotate.annotations_path", return_value=ann_path):
            annotate.prioritize("/tmp/tagged-proj", "high")
            annotate.tag("/tmp/tagged-proj", "ml")
            annotate.tag("/tmp/tagged-proj", "infra")
            annotate.note("/tmp/tagged-proj", "next: retrain model")

        with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
             mock.patch.object(cache, "load", return_value=None), \
             mock.patch.object(cache, "save"), \
             mock.patch("pj.discover.annotations_path", return_value=ann_path):
            projects, total = discover.discover()

        assert total == 1
        p = projects[0]
        assert p["priority"] == "high"
        assert sorted(p["tags"]) == ["infra", "ml"]


def test_discover_archive_state():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/arch-proj", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]

    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch("pj.annotate.annotations_path", return_value=ann_path):
            annotate.archive("/tmp/arch-proj")

        with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
             mock.patch.object(cache, "load", return_value=None), \
             mock.patch.object(cache, "save"), \
             mock.patch("pj.discover.annotations_path", return_value=ann_path):
            projects, total = discover.discover(state_filter="archived")

        assert total == 1
        assert projects[0]["state"] == "archived"


# --- cli actuator commands ---

def test_cli_note(capsys):
    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch("pj.annotate.annotations_path", return_value=ann_path):
            cli.main(["note", "/tmp/proj", "remember to refactor"])

    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["data"]["type"] == "note"
    assert parsed["data"]["text"] == "remember to refactor"


def test_cli_prioritize(capsys):
    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch("pj.annotate.annotations_path", return_value=ann_path):
            cli.main(["prioritize", "/tmp/proj", "high"])

    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["data"]["type"] == "priority"
    assert parsed["data"]["value"] == "high"


def test_cli_archive(capsys):
    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch("pj.annotate.annotations_path", return_value=ann_path):
            cli.main(["archive", "/tmp/proj"])

    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["data"]["type"] == "archive"


def test_cli_tag(capsys):
    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch("pj.annotate.annotations_path", return_value=ann_path):
            cli.main(["tag", "/tmp/proj", "backend"])

    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["data"]["type"] == "tag"
    assert parsed["data"]["tag"] == "backend"


# --- schedule ---

def _make_project(path, last_active=None, priority="none", state_val="active",
                  session_count=1, latest_note=None, agents=None):
    return {
        "id": discover.project_id(path),
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "agents": agents or ["claude"],
        "session_count": session_count,
        "last_active": last_active,
        "state": state_val,
        "priority": priority,
        "tags": [],
        "latest_note": latest_note,
    }


def test_schedule_scores_basic():
    now_iso = datetime.now(timezone.utc).isoformat()
    projects = [
        _make_project("/tmp/high-pri", last_active=now_iso, priority="high"),
        _make_project("/tmp/low-pri", last_active=now_iso, priority="low"),
    ]
    scored = schedule.score_projects(projects, recent_counts={})
    assert len(scored) == 2
    assert scored[0]["path"] == "/tmp/high-pri"
    assert scored[0]["score"] > scored[1]["score"]


def test_schedule_excludes_archived_and_blocked():
    now_iso = datetime.now(timezone.utc).isoformat()
    projects = [
        _make_project("/tmp/active", last_active=now_iso),
        _make_project("/tmp/archived", last_active=now_iso, state_val="archived"),
        _make_project("/tmp/blocked", last_active=now_iso, state_val="blocked"),
    ]
    scored = schedule.score_projects(projects, recent_counts={})
    assert len(scored) == 1
    assert scored[0]["path"] == "/tmp/active"


def test_schedule_factors_present():
    now_iso = datetime.now(timezone.utc).isoformat()
    projects = [_make_project("/tmp/proj", last_active=now_iso)]
    scored = schedule.score_projects(projects, recent_counts={})
    assert "factors" in scored[0]
    assert "score" in scored[0]
    assert "reason" in scored[0]
    for key in ("priority", "recency", "momentum", "staleness", "actionable"):
        assert key in scored[0]["factors"]


def test_schedule_momentum():
    now_iso = datetime.now(timezone.utc).isoformat()
    projects = [
        _make_project("/tmp/busy", last_active=now_iso),
        _make_project("/tmp/idle", last_active=now_iso),
    ]
    recent = {"/tmp/busy": 10, "/tmp/idle": 1}
    scored = schedule.score_projects(projects, recent_counts=recent)
    busy = next(s for s in scored if s["path"] == "/tmp/busy")
    idle = next(s for s in scored if s["path"] == "/tmp/idle")
    assert busy["factors"]["momentum"] > idle["factors"]["momentum"]


def test_schedule_staleness_boost():
    ts_5d = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    ts_1d = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    projects = [
        _make_project("/tmp/stale-ish", last_active=ts_5d, state_val="active"),
        _make_project("/tmp/fresh", last_active=ts_1d, state_val="active"),
    ]
    scored = schedule.score_projects(projects, recent_counts={})
    staleish = next(s for s in scored if s["path"] == "/tmp/stale-ish")
    fresh = next(s for s in scored if s["path"] == "/tmp/fresh")
    assert staleish["factors"]["staleness"] == 0.8
    assert fresh["factors"]["staleness"] == 0.0


def test_schedule_actionable_note():
    now_iso = datetime.now(timezone.utc).isoformat()
    projects = [
        _make_project("/tmp/with-note", last_active=now_iso, latest_note="next: fix tests"),
        _make_project("/tmp/no-note", last_active=now_iso),
        _make_project("/tmp/blocked-note", last_active=now_iso, latest_note="blocked: waiting on API"),
    ]
    scored = schedule.score_projects(projects, recent_counts={})
    with_note = next(s for s in scored if s["path"] == "/tmp/with-note")
    no_note = next(s for s in scored if s["path"] == "/tmp/no-note")
    assert with_note["factors"]["actionable"] == 1.0
    assert no_note["factors"]["actionable"] == 0.0


def test_schedule_reason_string():
    now_iso = datetime.now(timezone.utc).isoformat()
    projects = [_make_project("/tmp/high", last_active=now_iso, priority="high",
                              latest_note="next: ship it")]
    scored = schedule.score_projects(projects, recent_counts={})
    assert "high priority" in scored[0]["reason"]
    assert "has next step" in scored[0]["reason"]


def test_schedule_no_cass_fallback():
    now_iso = datetime.now(timezone.utc).isoformat()
    projects = [_make_project("/tmp/proj", last_active=now_iso)]
    with mock.patch.object(cass_facade, "db_paths", return_value=[]):
        scored = schedule.score_projects(projects)
    assert len(scored) == 1
    assert scored[0]["factors"]["momentum"] == 0.0


def test_schedule_recency_decay():
    ts_recent = datetime.now(timezone.utc).isoformat()
    ts_old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    projects = [
        _make_project("/tmp/recent", last_active=ts_recent),
        _make_project("/tmp/old", last_active=ts_old),
    ]
    scored = schedule.score_projects(projects, recent_counts={})
    recent = next(s for s in scored if s["path"] == "/tmp/recent")
    old = next(s for s in scored if s["path"] == "/tmp/old")
    assert recent["factors"]["recency"] > old["factors"]["recency"]


def test_schedule_none_last_active():
    projects = [_make_project("/tmp/no-ts", last_active=None, state_val="dormant")]
    scored = schedule.score_projects(projects, recent_counts={})
    assert len(scored) == 1
    assert scored[0]["factors"]["recency"] < 0.01


# --- cass_facade recent/search ---

def test_cass_facade_recent_session_counts():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_search.db"
        _create_test_db(db)
        with mock.patch.object(cass_facade, "db_paths", return_value=[db]):
            counts = cass_facade.recent_session_counts(days=7)
    assert "/home/user/project-a" in counts
    assert counts["/home/user/project-a"] >= 1


def test_cass_facade_recent_session_counts_no_db():
    with mock.patch.object(cass_facade, "db_paths", return_value=[]):
        assert cass_facade.recent_session_counts() == {}


def test_cass_facade_search_sessions():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_search.db"
        _create_test_db(db)
        with mock.patch.object(cass_facade, "db_paths", return_value=[db]):
            results = cass_facade.search_sessions("t1")
    assert len(results) >= 1
    assert results[0]["title"] == "t1"
    assert results[0]["path"] == "/home/user/project-a"


def test_cass_facade_search_sessions_no_match():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_search.db"
        _create_test_db(db)
        with mock.patch.object(cass_facade, "db_paths", return_value=[db]):
            results = cass_facade.search_sessions("nonexistent_query_xyz")
    assert results == []


def test_cass_facade_search_sessions_no_db():
    with mock.patch.object(cass_facade, "db_paths", return_value=[]):
        assert cass_facade.search_sessions("test") == []


# --- search ---

def test_search_by_name():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/api-gateway", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")), \
         mock.patch.object(cass_facade, "search_sessions", return_value=[]), \
         mock.patch.object(cass_facade, "search_content", return_value=[]):
        results = search_mod.search("api")
    assert len(results) == 1
    assert "name" in results[0]["match_fields"]


def test_search_by_note():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/my-proj", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    pid = discover.project_id("/tmp/my-proj")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"type": "note", "project_id": pid, "project_path": "/tmp/my-proj",
                            "text": "fix the authentication bug"}) + "\n")
        ann_path = Path(f.name)

    try:
        with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
             mock.patch.object(cache, "load", return_value=None), \
             mock.patch.object(cache, "save"), \
             mock.patch("pj.discover.annotations_path", return_value=ann_path), \
             mock.patch.object(cass_facade, "search_sessions", return_value=[]), \
         mock.patch.object(cass_facade, "search_content", return_value=[]):
            results = search_mod.search("authentication")
        assert len(results) == 1
        assert "note" in results[0]["match_fields"]
    finally:
        os.unlink(ann_path)


def test_search_by_session_title():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/proj", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    session_hits = [{"session_id": "s1", "path": "/tmp/proj", "agent": "claude",
                     "title": "debugging memory leak", "started_at": now_iso}]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")), \
         mock.patch.object(cass_facade, "search_sessions", return_value=session_hits), \
         mock.patch.object(cass_facade, "search_content", return_value=[]):
        results = search_mod.search("memory leak")
    assert len(results) == 1
    assert "session_title" in results[0]["match_fields"]
    assert "matching_titles" in results[0]


def test_search_no_results():
    fake = [{"path": "/tmp/proj", "agents": ["claude"], "session_count": 1,
             "last_active": datetime.now(timezone.utc).isoformat()}]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")), \
         mock.patch.object(cass_facade, "search_sessions", return_value=[]), \
         mock.patch.object(cass_facade, "search_content", return_value=[]):
        results = search_mod.search("zzz_no_match_zzz")
    assert results == []


def test_search_by_tag():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/tagged", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    pid = discover.project_id("/tmp/tagged")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"type": "tag", "project_id": pid, "project_path": "/tmp/tagged",
                            "tag": "infrastructure"}) + "\n")
        ann_path = Path(f.name)

    try:
        with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
             mock.patch.object(cache, "load", return_value=None), \
             mock.patch.object(cache, "save"), \
             mock.patch("pj.discover.annotations_path", return_value=ann_path), \
             mock.patch.object(cass_facade, "search_sessions", return_value=[]), \
         mock.patch.object(cass_facade, "search_content", return_value=[]):
            results = search_mod.search("infra")
        assert len(results) == 1
        assert "tag" in results[0]["match_fields"]
    finally:
        os.unlink(ann_path)


def test_search_deduplicates():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/api-proj", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    session_hits = [{"session_id": "s1", "path": "/tmp/api-proj", "agent": "claude",
                     "title": "api endpoint work", "started_at": now_iso}]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")), \
         mock.patch.object(cass_facade, "search_sessions", return_value=session_hits), \
         mock.patch.object(cass_facade, "search_content", return_value=[]):
        results = search_mod.search("api")
    assert len(results) == 1
    assert "name" in results[0]["match_fields"]
    assert "session_title" in results[0]["match_fields"]


# --- pretty next/search ---

def test_pretty_next_no_projects(capsys):
    pretty.print_next([])
    assert "No actionable" in capsys.readouterr().out


def test_pretty_next_with_projects(capsys):
    scored = [{
        "id": "abcd1234", "name": "my-project", "state": "active",
        "priority": "high", "score": 0.742,
        "factors": {"priority": 1.0, "recency": 0.8, "momentum": 0.5, "staleness": 0.0, "actionable": 1.0},
        "reason": "high priority; has next step",
    }]
    pretty.print_next(scored)
    out = capsys.readouterr().out
    assert "my-project" in out
    assert "0.74" in out
    assert "high priority" in out


def test_pretty_search_no_results(capsys):
    pretty.print_search([], "xyz")
    assert "No results" in capsys.readouterr().out


def test_pretty_search_no_results_suggests_split_terms(capsys):
    pretty.print_search([], "sports broadcast fan excitement")
    out = capsys.readouterr().out
    assert "exact phrases" in out
    assert "pj search sports broadcast fan excitement --pretty" in out


def test_pretty_search_with_results(capsys):
    results = [{
        "name": "api-gateway", "state": "active",
        "match_fields": ["name", "session_title"],
    }]
    pretty.print_search(results, "api")
    out = capsys.readouterr().out
    assert "api-gateway" in out
    assert 'Search: "api"' in out


# --- cli next/search ---

def test_cli_next_json(capsys):
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/next-test", "agents": ["claude"], "session_count": 3, "last_active": now_iso}]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")), \
         mock.patch.object(cass_facade, "recent_session_counts", return_value={}):
        cli.main(["next"])

    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert len(parsed["data"]) >= 1
    assert "score" in parsed["data"][0]
    assert "factors" in parsed["data"][0]


def test_cli_next_pretty(capsys):
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/next-pretty", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")), \
         mock.patch.object(cass_facade, "recent_session_counts", return_value={}):
        cli.main(["next", "--pretty"])

    out = capsys.readouterr().out
    assert "next-pretty" in out


def test_cli_search_json(capsys):
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/search-test", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")), \
         mock.patch.object(cass_facade, "search_sessions", return_value=[]), \
         mock.patch.object(cass_facade, "search_content", return_value=[]):
        cli.main(["search", "search-test"])

    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert len(parsed["data"]) == 1
    assert parsed["data"][0]["name"] == "search-test"
    assert "name" in parsed["data"][0]["match_fields"]


def test_cli_search_pretty(capsys):
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/search-pretty", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")), \
         mock.patch.object(cass_facade, "search_sessions", return_value=[]), \
         mock.patch.object(cass_facade, "search_content", return_value=[]):
        cli.main(["search", "--pretty", "search-pretty"])

    out = capsys.readouterr().out
    assert "search-pretty" in out


def test_cli_search_help_teaches_query_strategy(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["search", "--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--regex" in out
    assert "--project" in out
    assert "Query strategy" in out
    assert "quoted multi-word query is an exact substring phrase" in out


def test_cli_search_here_uses_current_project(capsys):
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/here-proj", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")), \
         mock.patch("os.getcwd", return_value="/tmp/here-proj/subdir"), \
         mock.patch.object(cass_facade, "search_sessions", return_value=[]), \
         mock.patch.object(cass_facade, "search_content", return_value=[]):
        cli.main(["search", "--here", "here-proj"])

    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["meta"]["project"] == "/tmp/here-proj"
    assert parsed["data"][0]["name"] == "here-proj"


def test_cli_search_here_outside_project_errors(capsys):
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/here-proj", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")), \
         mock.patch("os.getcwd", return_value="/tmp/elsewhere"), \
         pytest.raises(SystemExit) as exc:
        cli.main(["search", "--here", "anything"])

    assert exc.value.code == 1
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is False
    assert "not inside a discovered project" in parsed["meta"]["error"]


# --- discover latest_note ---

def test_discover_includes_latest_note():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/noted", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    pid = discover.project_id("/tmp/noted")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"type": "note", "project_id": pid, "project_path": "/tmp/noted",
                            "text": "first note"}) + "\n")
        f.write(json.dumps({"type": "note", "project_id": pid, "project_path": "/tmp/noted",
                            "text": "latest note"}) + "\n")
        ann_path = Path(f.name)

    try:
        with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
             mock.patch.object(cache, "load", return_value=None), \
             mock.patch.object(cache, "save"), \
             mock.patch("pj.discover.annotations_path", return_value=ann_path):
            projects, _ = discover.discover()
        assert projects[0]["latest_note"] == "latest note"
    finally:
        os.unlink(ann_path)


def test_discover_latest_note_none_when_no_notes():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/no-notes", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")):
        projects, _ = discover.discover()
    assert projects[0]["latest_note"] is None


# --- resume ---

def test_resume_command_claude():
    cmd = resume.resume_command("claude", "abc-123")
    assert cmd == "claude --resume abc-123"


def test_resume_command_codex():
    cmd = resume.resume_command("codex", "sess-456")
    assert cmd == "codex resume sess-456"


def test_resume_command_unknown_agent():
    cmd = resume.resume_command("new-agent", "s1")
    assert cmd == "new-agent --resume s1"


def test_resume_command_quotes_special_chars():
    cmd = resume.resume_command("claude", "id with spaces")
    assert "id with spaces" in cmd
    assert cmd.startswith("claude --resume")


def test_full_resume_command():
    cmd = resume.full_resume_command("/home/user/proj", "claude", "abc-123")
    assert cmd == "cd /home/user/proj && claude --resume abc-123"


def test_full_resume_command_quotes_path():
    cmd = resume.full_resume_command("/home/user/my project", "claude", "s1")
    assert "my project" in cmd
    assert cmd.startswith("cd ")
    assert "&& claude --resume" in cmd


# --- cass_facade project_sessions ---

def test_cass_facade_project_sessions():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_search.db"
        _create_test_db(db)
        with mock.patch.object(cass_facade, "db_paths", return_value=[db]):
            sessions = cass_facade.project_sessions("/home/user/project-a")
    assert len(sessions) == 3
    assert sessions[0]["agent"] in ("claude", "codex")
    assert sessions[0]["started_at"] >= sessions[1]["started_at"]


def test_cass_facade_project_sessions_no_match():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_search.db"
        _create_test_db(db)
        with mock.patch.object(cass_facade, "db_paths", return_value=[db]):
            sessions = cass_facade.project_sessions("/nonexistent/path")
    assert sessions == []


def test_cass_facade_project_sessions_no_db():
    with mock.patch.object(cass_facade, "db_paths", return_value=[]):
        assert cass_facade.project_sessions("/any/path") == []


# --- discover resolve_project ---

def _mock_discover_with(fake_projects):
    """Context manager that mocks discover to return fake_projects."""
    return (
        mock.patch.object(cass_facade, "list_projects", return_value=fake_projects),
        mock.patch.object(cache, "load", return_value=None),
        mock.patch.object(cache, "save"),
        mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")),
    )


def test_resolve_project_by_name():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [
        {"path": "/home/user/api-gateway", "agents": ["claude"], "session_count": 1, "last_active": now_iso},
        {"path": "/home/user/web-app", "agents": ["codex"], "session_count": 2, "last_active": now_iso},
    ]
    mocks = _mock_discover_with(fake)
    with mocks[0], mocks[1], mocks[2], mocks[3]:
        result = discover.resolve_project("api-gateway")
    assert result is not None
    assert result["name"] == "api-gateway"


def test_resolve_project_by_id_prefix():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/home/user/my-proj", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    pid = discover.project_id("/home/user/my-proj")
    mocks = _mock_discover_with(fake)
    with mocks[0], mocks[1], mocks[2], mocks[3]:
        result = discover.resolve_project(pid[:4])
    assert result is not None
    assert result["id"] == pid


def test_resolve_project_by_path():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/home/user/my-proj", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    mocks = _mock_discover_with(fake)
    with mocks[0], mocks[1], mocks[2], mocks[3]:
        result = discover.resolve_project("/home/user/my-proj")
    assert result is not None
    assert result["path"] == "/home/user/my-proj"


def test_resolve_project_substring():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [
        {"path": "/home/user/jam-sesh", "agents": ["claude"], "session_count": 1, "last_active": now_iso},
        {"path": "/home/user/web-app", "agents": ["codex"], "session_count": 1, "last_active": now_iso},
    ]
    mocks = _mock_discover_with(fake)
    with mocks[0], mocks[1], mocks[2], mocks[3]:
        result = discover.resolve_project("jam")
    assert result is not None
    assert result["name"] == "jam-sesh"


def test_resolve_project_ambiguous_returns_none():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [
        {"path": "/home/user/api-gateway", "agents": ["claude"], "session_count": 1, "last_active": now_iso},
        {"path": "/home/user/api-server", "agents": ["codex"], "session_count": 1, "last_active": now_iso},
    ]
    mocks = _mock_discover_with(fake)
    with mocks[0], mocks[1], mocks[2], mocks[3]:
        result = discover.resolve_project("api")
    assert result is None


def test_resolve_project_no_match():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/home/user/proj", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    mocks = _mock_discover_with(fake)
    with mocks[0], mocks[1], mocks[2], mocks[3]:
        result = discover.resolve_project("zzz_nonexistent")
    assert result is None


def test_resolve_project_for_cwd_prefers_deepest_match():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [
        {"path": "/home/user/proj", "agents": ["claude"], "session_count": 1, "last_active": now_iso},
        {"path": "/home/user/proj/packages/api", "agents": ["claude"], "session_count": 1, "last_active": now_iso},
    ]
    mocks = _mock_discover_with(fake)
    with mocks[0], mocks[1], mocks[2], mocks[3]:
        result = discover.resolve_project_for_cwd("/home/user/proj/packages/api/src")
    assert result is not None
    assert result["path"] == "/home/user/proj/packages/api"


def test_resolve_project_for_cwd_outside_projects():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/home/user/proj", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    mocks = _mock_discover_with(fake)
    with mocks[0], mocks[1], mocks[2], mocks[3]:
        result = discover.resolve_project_for_cwd("/home/user/elsewhere")
    assert result is None


# --- cli status ---

def test_cli_status_json(capsys):
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/status-test", "agents": ["claude"], "session_count": 3, "last_active": now_iso}]
    fake_sessions = [
        {"session_id": "s1", "agent": "claude", "title": "fixing bugs", "started_at": now_iso},
        {"session_id": "s2", "agent": "claude", "title": "adding tests", "started_at": now_iso},
    ]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")), \
         mock.patch.object(cass_facade, "project_sessions", return_value=fake_sessions):
        cli.main(["status", "status-test"])

    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["data"]["name"] == "status-test"
    assert len(parsed["data"]["sessions"]) == 2
    assert "resume_cmd" in parsed["data"]
    assert "claude --resume" in parsed["data"]["resume_cmd"]


def test_cli_status_pretty(capsys):
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/status-pretty", "agents": ["claude", "codex"], "session_count": 5, "last_active": now_iso}]
    fake_sessions = [
        {"session_id": "s1", "agent": "claude", "title": "work session", "started_at": now_iso},
    ]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")), \
         mock.patch.object(cass_facade, "project_sessions", return_value=fake_sessions):
        cli.main(["status", "--pretty", "status-pretty"])

    out = capsys.readouterr().out
    assert "status-pretty" in out
    assert "claude, codex" in out
    assert "Resume:" in out


def test_cli_status_not_found(capsys):
    with mock.patch.object(cass_facade, "list_projects", return_value=[]), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")):
        try:
            cli.main(["status", "nonexistent"])
        except SystemExit:
            pass

    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["success"] is False


# --- cli resume ---

def test_cli_resume(capsys):
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/resume-test", "agents": ["codex"], "session_count": 1, "last_active": now_iso}]
    fake_sessions = [{"session_id": "sess-abc", "agent": "codex", "title": "session", "started_at": now_iso}]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")), \
         mock.patch.object(cass_facade, "project_sessions", return_value=fake_sessions):
        cli.main(["resume", "resume-test"])

    out = capsys.readouterr().out.strip()
    assert out == "cd /tmp/resume-test && codex resume sess-abc"


def test_cli_resume_not_found(capsys):
    with mock.patch.object(cass_facade, "list_projects", return_value=[]), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")):
        try:
            cli.main(["resume", "nonexistent"])
        except SystemExit:
            pass

    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["success"] is False


def test_cli_resume_no_sessions(capsys):
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/no-sess", "agents": ["claude"], "session_count": 0, "last_active": now_iso}]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")), \
         mock.patch.object(cass_facade, "project_sessions", return_value=[]):
        try:
            cli.main(["resume", "no-sess"])
        except SystemExit:
            pass

    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "No sessions" in parsed["meta"]["error"]


# --- pretty status ---

def test_pretty_status(capsys):
    now_iso = datetime.now(timezone.utc).isoformat()
    data = {
        "name": "my-proj",
        "path": "/home/user/my-proj",
        "id": "abcd1234",
        "state": "active",
        "priority": "high",
        "agents": ["claude", "codex"],
        "session_count": 10,
        "last_active": now_iso,
        "tags": ["ml", "infra"],
        "latest_note": "next: retrain model",
        "sessions": [
            {"session_id": "s1-long-id-here", "agent": "claude", "title": "training run", "started_at": now_iso},
        ],
        "resume_cmd": "cd /home/user/my-proj && claude --resume s1-long-id-here",
    }
    pretty.print_status(data)
    out = capsys.readouterr().out
    assert "my-proj" in out
    assert "active" in out
    assert "high" in out
    assert "claude, codex" in out
    assert "ml, infra" in out
    assert "retrain model" in out
    assert "training run" in out
    assert "Resume:" in out


def test_pretty_status_minimal(capsys):
    data = {
        "name": "bare-proj",
        "path": "/tmp/bare",
        "id": "deadbeef",
        "state": "dormant",
        "priority": "none",
        "agents": [],
        "session_count": 0,
        "last_active": None,
        "tags": [],
        "latest_note": None,
        "sessions": [],
        "resume_cmd": None,
    }
    pretty.print_status(data)
    out = capsys.readouterr().out
    assert "bare-proj" in out
    assert "dormant" in out
    assert "Resume:" not in out


# --- Phase 5: tag filter ---

def test_discover_tag_filter():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [
        {"path": "/tmp/tagged-a", "agents": ["claude"], "session_count": 1, "last_active": now_iso},
        {"path": "/tmp/tagged-b", "agents": ["claude"], "session_count": 1, "last_active": now_iso},
        {"path": "/tmp/untagged", "agents": ["claude"], "session_count": 1, "last_active": now_iso},
    ]
    pid_a = discover.project_id("/tmp/tagged-a")
    pid_b = discover.project_id("/tmp/tagged-b")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"type": "tag", "project_id": pid_a, "project_path": "/tmp/tagged-a", "tag": "ml"}) + "\n")
        f.write(json.dumps({"type": "tag", "project_id": pid_b, "project_path": "/tmp/tagged-b", "tag": "infra"}) + "\n")
        ann_path = Path(f.name)

    try:
        with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
             mock.patch.object(cache, "load", return_value=None), \
             mock.patch.object(cache, "save"), \
             mock.patch("pj.discover.annotations_path", return_value=ann_path):
            projects, total = discover.discover(tag_filter="ml")

        assert total == 1
        assert projects[0]["name"] == "tagged-a"
        assert "ml" in projects[0]["tags"]
    finally:
        os.unlink(ann_path)


def test_discover_tag_filter_no_match():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/proj", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")):
        projects, total = discover.discover(tag_filter="nonexistent")
    assert total == 0
    assert projects == []


def test_cli_list_tag_filter(capsys):
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [
        {"path": "/tmp/ml-proj", "agents": ["claude"], "session_count": 1, "last_active": now_iso},
        {"path": "/tmp/web-proj", "agents": ["claude"], "session_count": 1, "last_active": now_iso},
    ]
    pid_ml = discover.project_id("/tmp/ml-proj")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"type": "tag", "project_id": pid_ml, "project_path": "/tmp/ml-proj", "tag": "ml"}) + "\n")
        ann_path = Path(f.name)

    try:
        with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
             mock.patch.object(cache, "load", return_value=None), \
             mock.patch.object(cache, "save"), \
             mock.patch("pj.discover.annotations_path", return_value=ann_path):
            cli.main(["list", "--tag", "ml"])

        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["success"] is True
        assert parsed["meta"]["total"] == 1
        assert parsed["data"][0]["name"] == "ml-proj"
    finally:
        os.unlink(ann_path)


# --- Phase 5: latency_ms in all envelopes ---

def test_cli_list_has_latency(capsys):
    fake = [{"path": "/tmp/t", "agents": ["claude"], "session_count": 1,
             "last_active": datetime.now(timezone.utc).isoformat()}]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")):
        cli.main(["list"])
    parsed = json.loads(capsys.readouterr().out)
    assert "latency_ms" in parsed["meta"]
    assert isinstance(parsed["meta"]["latency_ms"], int)


def test_cli_next_has_latency(capsys):
    fake = [{"path": "/tmp/t", "agents": ["claude"], "session_count": 1,
             "last_active": datetime.now(timezone.utc).isoformat()}]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")), \
         mock.patch.object(cass_facade, "recent_session_counts", return_value={}):
        cli.main(["next"])
    parsed = json.loads(capsys.readouterr().out)
    assert "latency_ms" in parsed["meta"]


def test_cli_annotate_has_latency(capsys):
    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch("pj.annotate.annotations_path", return_value=ann_path):
            cli.main(["note", "/tmp/proj", "test"])
    parsed = json.loads(capsys.readouterr().out)
    assert "latency_ms" in parsed["meta"]


# --- Phase 5: pretty color helpers (no TTY = no ANSI) ---

def test_pretty_no_color_when_not_tty(capsys):
    now_iso = datetime.now(timezone.utc).isoformat()
    projects = [
        {"id": "abcd1234", "state": "active", "name": "color-test",
         "agents": ["claude"], "session_count": 1,
         "priority": "high", "last_active": now_iso},
    ]
    pretty.print_projects(projects, 1, 0, 20)
    out = capsys.readouterr().out
    assert "\033[" not in out
    assert "color-test" in out
    assert "active" in out


def test_pretty_pad_plain():
    assert len(pretty._pad("hello", 10)) == 10
    assert pretty._pad("hello", 3) == "hello"


def test_pretty_pad_with_ansi():
    colored = "\033[32mhello\033[0m"
    padded = pretty._pad(colored, 10)
    assert padded.startswith("\033[32m")
    import re
    visible = re.sub(r"\033\[[0-9;]*m", "", padded)
    assert len(visible) == 10


def test_pretty_color_state():
    with mock.patch.object(pretty, "_use_color", return_value=False):
        assert pretty._color_state("active") == "active"
    with mock.patch.object(pretty, "_use_color", return_value=True):
        result = pretty._color_state("active")
        assert "\033[32m" in result
        assert "active" in result


def test_pretty_color_score():
    with mock.patch.object(pretty, "_use_color", return_value=False):
        assert pretty._color_score(0.75) == "0.75"
    with mock.patch.object(pretty, "_use_color", return_value=True):
        assert "\033[32m" in pretty._color_score(0.75)
        assert "\033[33m" in pretty._color_score(0.50)
        assert "\033[31m" in pretty._color_score(0.20)


# --- Bug fix: session_id as integer from SQLite ---
# CASS stores session IDs as INTEGER PRIMARY KEY, so they come back as int.
# resume.py and pretty.py must handle int, str, and UUID-style IDs.


def test_resume_command_int_session_id():
    """Bug: shlex.quote() crashed on int session_id from SQLite."""
    cmd = resume.resume_command("claude", 42)
    assert cmd == "claude --resume 42"


def test_resume_command_large_int_session_id():
    cmd = resume.resume_command("codex", 999999)
    assert cmd == "codex resume 999999"


def test_full_resume_command_int_session_id():
    cmd = resume.full_resume_command("/home/user/proj", "claude", 1)
    assert "cd /home/user/proj" in cmd
    assert "--resume 1" in cmd


def test_resume_command_uuid_session_id():
    """Normal case: string UUID session ID."""
    uid = "abc12345-dead-beef-cafe-0123456789ab"
    cmd = resume.resume_command("claude", uid)
    assert uid in cmd


def test_resume_command_none_session_id():
    """Edge case: None session_id should not crash."""
    cmd = resume.resume_command("claude", None)
    assert "--resume" in cmd


def test_pretty_status_int_session_id(capsys):
    """Bug: str[:12] crashed on int session_id in pretty.print_status."""
    data = {
        "name": "proj",
        "path": "/tmp/proj",
        "id": "abcd1234",
        "state": "active",
        "priority": "none",
        "agents": ["claude"],
        "session_count": 1,
        "last_active": datetime.now(timezone.utc).isoformat(),
        "tags": [],
        "latest_note": None,
        "sessions": [
            {"session_id": 42, "agent": "claude", "title": "session", "started_at": datetime.now(timezone.utc).isoformat()},
        ],
        "resume_cmd": "cd /tmp/proj && claude --resume 42",
    }
    pretty.print_status(data)
    out = capsys.readouterr().out
    assert "42" in out
    assert "proj" in out


def test_pretty_status_mixed_session_ids(capsys):
    """Both int and string session IDs in the same status view."""
    now_iso = datetime.now(timezone.utc).isoformat()
    data = {
        "name": "mixed",
        "path": "/tmp/mixed",
        "id": "deadbeef",
        "state": "active",
        "priority": "none",
        "agents": ["claude", "codex"],
        "session_count": 2,
        "last_active": now_iso,
        "tags": [],
        "latest_note": None,
        "sessions": [
            {"session_id": 7, "agent": "claude", "title": "int id", "started_at": now_iso},
            {"session_id": "abc-def-123", "agent": "codex", "title": "str id", "started_at": now_iso},
        ],
        "resume_cmd": "cd /tmp/mixed && claude --resume 7",
    }
    pretty.print_status(data)
    out = capsys.readouterr().out
    assert "7" in out
    assert "abc-def-123" in out


# --- Multi-DB: PJ_CASS_DBS support ---


def _create_test_db_with_data(path: Path, workspaces: list[tuple], conversations: list[tuple]):
    """Create a CASS SQLite DB with specified workspaces and conversations."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE agents (id INTEGER PRIMARY KEY, slug TEXT);
        CREATE TABLE workspaces (id INTEGER PRIMARY KEY, path TEXT);
        CREATE TABLE conversations (
            id TEXT PRIMARY KEY, agent_id INTEGER, workspace_id INTEGER,
            started_at INTEGER, title TEXT,
            source_path TEXT, source_id TEXT, origin_host TEXT,
            primary_model TEXT, ended_at INTEGER,
            total_input_tokens INTEGER, total_output_tokens INTEGER,
            total_cache_read_tokens INTEGER, total_cache_creation_tokens INTEGER,
            grand_total_tokens INTEGER, user_message_count INTEGER,
            assistant_message_count INTEGER, tool_call_count INTEGER,
            api_call_count INTEGER
        );
    """)
    conn.execute("INSERT INTO agents VALUES (1, 'claude_code')")
    conn.execute("INSERT INTO agents VALUES (2, 'codex')")
    for ws in workspaces:
        conn.execute("INSERT INTO workspaces VALUES (?, ?)", ws)
    for conv in conversations:
        conn.execute(
            "INSERT INTO conversations VALUES (?, ?, ?, ?, ?, '', '', '', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)",
            conv,
        )
    conn.commit()
    conn.close()


def test_multi_db_list_projects():
    """Two CASS databases should merge their projects."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    with tempfile.TemporaryDirectory() as tmpdir:
        db1 = Path(tmpdir) / "host.db"
        db2 = Path(tmpdir) / "yolobox.db"

        _create_test_db_with_data(db1,
            workspaces=[(1, "/home/user/proj-host")],
            conversations=[("c1", 1, 1, now_ms, "host session")],
        )
        _create_test_db_with_data(db2,
            workspaces=[(1, "/home/user/proj-yolobox")],
            conversations=[("c2", 1, 1, now_ms - 1000, "yolobox session")],
        )

        with mock.patch.object(cass_facade, "db_paths", return_value=[db1, db2]):
            projects = cass_facade.list_projects()

    paths = {p["path"] for p in projects}
    assert "/home/user/proj-host" in paths
    assert "/home/user/proj-yolobox" in paths
    assert len(projects) == 2


def test_multi_db_same_workspace_merges():
    """Same workspace in two DBs should merge session counts and agents."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    with tempfile.TemporaryDirectory() as tmpdir:
        db1 = Path(tmpdir) / "host.db"
        db2 = Path(tmpdir) / "yolobox.db"

        _create_test_db_with_data(db1,
            workspaces=[(1, "/home/user/shared-proj")],
            conversations=[("c1", 1, 1, now_ms, "from host")],
        )
        _create_test_db_with_data(db2,
            workspaces=[(1, "/home/user/shared-proj")],
            conversations=[
                ("c2", 2, 1, now_ms - 1000, "from yolobox"),
                ("c3", 2, 1, now_ms - 2000, "from yolobox 2"),
            ],
        )

        with mock.patch.object(cass_facade, "db_paths", return_value=[db1, db2]):
            projects = cass_facade.list_projects()

    assert len(projects) == 1
    p = projects[0]
    assert p["session_count"] == 3
    assert sorted(p["agents"]) == ["claude_code", "codex"]


def test_multi_db_recent_session_counts_merge():
    """Recent session counts should sum across DBs for same workspace."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    with tempfile.TemporaryDirectory() as tmpdir:
        db1 = Path(tmpdir) / "a.db"
        db2 = Path(tmpdir) / "b.db"

        _create_test_db_with_data(db1,
            workspaces=[(1, "/proj")],
            conversations=[("c1", 1, 1, now_ms, "s1")],
        )
        _create_test_db_with_data(db2,
            workspaces=[(1, "/proj")],
            conversations=[("c2", 1, 1, now_ms, "s2"), ("c3", 1, 1, now_ms, "s3")],
        )

        with mock.patch.object(cass_facade, "db_paths", return_value=[db1, db2]):
            counts = cass_facade.recent_session_counts(days=7)

    assert counts["/proj"] == 3


def test_multi_db_project_sessions_sorted():
    """Sessions from multiple DBs should be sorted by time, most recent first."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    with tempfile.TemporaryDirectory() as tmpdir:
        db1 = Path(tmpdir) / "a.db"
        db2 = Path(tmpdir) / "b.db"

        _create_test_db_with_data(db1,
            workspaces=[(1, "/proj")],
            conversations=[("c1", 1, 1, now_ms - 2000, "older")],
        )
        _create_test_db_with_data(db2,
            workspaces=[(1, "/proj")],
            conversations=[("c2", 1, 1, now_ms, "newer")],
        )

        with mock.patch.object(cass_facade, "db_paths", return_value=[db1, db2]):
            sessions = cass_facade.project_sessions("/proj")

    assert len(sessions) == 2
    assert sessions[0]["title"] == "newer"
    assert sessions[1]["title"] == "older"


def test_multi_db_search_sessions():
    """Search should find results across both DBs."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    with tempfile.TemporaryDirectory() as tmpdir:
        db1 = Path(tmpdir) / "a.db"
        db2 = Path(tmpdir) / "b.db"

        _create_test_db_with_data(db1,
            workspaces=[(1, "/proj-a")],
            conversations=[("c1", 1, 1, now_ms, "fixing auth bug")],
        )
        _create_test_db_with_data(db2,
            workspaces=[(1, "/proj-b")],
            conversations=[("c2", 1, 1, now_ms, "fixing auth middleware")],
        )

        with mock.patch.object(cass_facade, "db_paths", return_value=[db1, db2]):
            results = cass_facade.search_sessions("auth")

    assert len(results) == 2
    paths = {r["path"] for r in results}
    assert "/proj-a" in paths
    assert "/proj-b" in paths


def test_multi_db_empty_list():
    """No DBs should return empty results, not crash."""
    with mock.patch.object(cass_facade, "db_paths", return_value=[]):
        assert cass_facade.list_projects() == []
        assert cass_facade.recent_session_counts() == {}
        assert cass_facade.project_sessions("/any") == []
        assert cass_facade.search_sessions("any") == []


def test_multi_db_one_corrupt():
    """A corrupt DB should be skipped, not crash the whole query."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    with tempfile.TemporaryDirectory() as tmpdir:
        good_db = Path(tmpdir) / "good.db"
        bad_db = Path(tmpdir) / "bad.db"

        _create_test_db_with_data(good_db,
            workspaces=[(1, "/proj")],
            conversations=[("c1", 1, 1, now_ms, "works")],
        )
        bad_db.write_text("not a sqlite database")

        with mock.patch.object(cass_facade, "db_paths", return_value=[bad_db, good_db]):
            projects = cass_facade.list_projects()

    assert len(projects) == 1
    assert projects[0]["path"] == "/proj"


def test_multi_db_nonexistent_skipped():
    """A DB path that doesn't exist should be filtered out by db_paths()."""
    with tempfile.TemporaryDirectory() as tmpdir:
        missing = Path(tmpdir) / "does_not_exist.db"
        assert not missing.exists()
        # db_paths filters by existence, so this shouldn't appear
        with mock.patch.dict(os.environ, {"PJ_CASS_DBS": str(missing)}):
            paths = cass_facade.db_paths()
        assert missing not in paths


def test_db_paths_deduplicates():
    """Same DB path listed twice should only appear once."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_search.db"
        _create_test_db_with_data(db, workspaces=[], conversations=[])

        with mock.patch.dict(os.environ, {"PJ_CASS_DBS": f"{db}:{db}"}):
            # Clear other env vars that might add paths
            with mock.patch.dict(os.environ, {"CASS_DATA_DIR": ""}, clear=False):
                paths = cass_facade.db_paths()

    assert len([p for p in paths if p.resolve() == db.resolve()]) == 1


# --- PJ_DATA_DIR isolation ---

def test_pj_data_dir_isolates_annotations():
    """PJ_DATA_DIR env var should redirect annotations to a temp directory."""
    import pj.paths as pj_paths
    with tempfile.TemporaryDirectory() as tmpdir:
        with mock.patch.dict(os.environ, {"PJ_DATA_DIR": tmpdir}):
            p = pj_paths.annotations_path()
            assert str(p).startswith(tmpdir)
            assert p.name == "annotations.jsonl"


def test_pj_data_dir_isolates_cache():
    """PJ_DATA_DIR env var should redirect cache to a subdirectory."""
    import pj.paths as pj_paths
    with tempfile.TemporaryDirectory() as tmpdir:
        with mock.patch.dict(os.environ, {"PJ_DATA_DIR": tmpdir}):
            cd = pj_paths.cache_dir()
            cf = pj_paths.cache_file()
            assert str(cd).startswith(tmpdir)
            assert str(cf).startswith(tmpdir)


# --- Content search (search_content + search integration) ---


def _create_test_db_with_messages(path: Path, workspaces, conversations, messages):
    """Create a CASS DB with messages for content search tests."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE agents (id INTEGER PRIMARY KEY, slug TEXT);
        CREATE TABLE workspaces (id INTEGER PRIMARY KEY, path TEXT);
        CREATE TABLE conversations (
            id TEXT PRIMARY KEY, agent_id INTEGER, workspace_id INTEGER,
            started_at INTEGER, title TEXT,
            source_path TEXT, source_id TEXT, origin_host TEXT,
            primary_model TEXT, ended_at INTEGER,
            total_input_tokens INTEGER, total_output_tokens INTEGER,
            total_cache_read_tokens INTEGER, total_cache_creation_tokens INTEGER,
            grand_total_tokens INTEGER, user_message_count INTEGER,
            assistant_message_count INTEGER, tool_call_count INTEGER,
            api_call_count INTEGER
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, conversation_id INTEGER, idx INTEGER,
            role TEXT, author TEXT, created_at INTEGER, content TEXT,
            extra_json TEXT, extra_bin BLOB
        );
    """)
    conn.execute("INSERT INTO agents VALUES (1, 'claude_code')")
    for ws in workspaces:
        conn.execute("INSERT INTO workspaces VALUES (?, ?)", ws)
    for conv in conversations:
        conn.execute(
            "INSERT INTO conversations VALUES (?, ?, ?, ?, ?, '', '', '', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)", conv,
        )
    for msg in messages:
        conn.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, '', '')", msg,
        )
    conn.commit()
    conn.close()


def test_search_content_like_fallback():
    """search_content falls back to LIKE when FTS5 is unavailable."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.db"
        _create_test_db_with_messages(db,
            workspaces=[(1, "/proj/health-tracker")],
            conversations=[("c1", 1, 1, now_ms, "health session")],
            messages=[
                (1, "c1", 0, "user", None, now_ms, "research personal health metrics"),
                (2, "c1", 1, "assistant", None, now_ms, "I'll help with health data analysis"),
            ],
        )
        with mock.patch.object(cass_facade, "db_paths", return_value=[db]):
            results = cass_facade.search_content("health")

    assert len(results) >= 1
    assert results[0]["path"] == "/proj/health-tracker"
    assert "health" in results[0]["snippet"].lower()
    assert results[0]["match_type"] == "like"


def test_search_content_across_multi_db():
    """Content search merges results from multiple databases."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    with tempfile.TemporaryDirectory() as tmpdir:
        db1 = Path(tmpdir) / "host.db"
        db2 = Path(tmpdir) / "yolobox.db"

        _create_test_db_with_messages(db1,
            workspaces=[(1, "/proj/host-research")],
            conversations=[("c1", 1, 1, now_ms, "host session")],
            messages=[(1, "c1", 0, "user", None, now_ms, "analyzing nutrition data")],
        )
        _create_test_db_with_messages(db2,
            workspaces=[(1, "/proj/yolo-research")],
            conversations=[("c1", 1, 1, now_ms - 1000, "yolo session")],
            messages=[(1, "c1", 0, "user", None, now_ms, "tracking nutrition intake")],
        )

        with mock.patch.object(cass_facade, "db_paths", return_value=[db1, db2]):
            results = cass_facade.search_content("nutrition")

    paths = {r["path"] for r in results}
    assert "/proj/host-research" in paths
    assert "/proj/yolo-research" in paths


def test_search_content_dedupes_by_session():
    """Multiple message hits in the same session should return one result."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.db"
        _create_test_db_with_messages(db,
            workspaces=[(1, "/proj")],
            conversations=[("c1", 1, 1, now_ms, "session")],
            messages=[
                (1, "c1", 0, "user", None, now_ms, "first mention of vitamins"),
                (2, "c1", 1, "assistant", None, now_ms, "vitamins are important"),
                (3, "c1", 2, "user", None, now_ms, "more about vitamins please"),
            ],
        )
        with mock.patch.object(cass_facade, "db_paths", return_value=[db]):
            results = cass_facade.search_content("vitamins")

    assert len(results) == 1  # one session, not three messages


def test_search_content_no_results():
    """Search for nonexistent term returns empty."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.db"
        _create_test_db_with_messages(db,
            workspaces=[(1, "/proj")],
            conversations=[("c1", 1, 1, now_ms, "session")],
            messages=[(1, "c1", 0, "user", None, now_ms, "hello world")],
        )
        with mock.patch.object(cass_facade, "db_paths", return_value=[db]):
            results = cass_facade.search_content("xyznonexistent")

    assert results == []


def test_search_content_corrupt_db_skipped():
    """A corrupt DB shouldn't crash content search."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    with tempfile.TemporaryDirectory() as tmpdir:
        good = Path(tmpdir) / "good.db"
        bad = Path(tmpdir) / "bad.db"

        _create_test_db_with_messages(good,
            workspaces=[(1, "/proj")],
            conversations=[("c1", 1, 1, now_ms, "session")],
            messages=[(1, "c1", 0, "user", None, now_ms, "health research data")],
        )
        bad.write_text("not a database")

        with mock.patch.object(cass_facade, "db_paths", return_value=[bad, good]):
            results = cass_facade.search_content("health")

    assert len(results) == 1


def test_extract_snippet_centered():
    """Snippet should be centered around the match."""
    content = "x" * 200 + "KEYWORD" + "y" * 200
    snippet = cass_facade._extract_snippet(content, "KEYWORD", context_chars=20)
    assert "KEYWORD" in snippet
    assert snippet.startswith("...")
    assert snippet.endswith("...")
    assert len(snippet) < 60  # 20 + 7 + 20 + 6 (ellipses)


def test_extract_snippet_at_start():
    """Match at the start shouldn't have leading ellipsis."""
    snippet = cass_facade._extract_snippet("KEYWORD is here", "KEYWORD", context_chars=50)
    assert not snippet.startswith("...")
    assert "KEYWORD" in snippet


def test_extract_snippet_no_match():
    """If query not found (e.g. FTS stemmed match), return content start."""
    snippet = cass_facade._extract_snippet("some long content here", "missing", context_chars=10)
    assert snippet.startswith("some")


def test_search_integration_content_match():
    """pj search should include content matches with snippets."""
    now_iso = datetime.now(timezone.utc).isoformat()
    fake_projects = [{"path": "/proj", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    content_hits = [{
        "path": "/proj",
        "session_id": 1,
        "agent": "claude",
        "title": "session",
        "snippet": "...discussing personal health research...",
        "role": "user",
        "started_at": now_iso,
        "match_type": "like",
    }]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake_projects), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")), \
         mock.patch.object(cass_facade, "search_sessions", return_value=[]), \
         mock.patch.object(cass_facade, "search_content", return_value=content_hits):
        results = search_mod.search("health")

    assert len(results) == 1
    assert "content" in results[0]["match_fields"]
    assert "snippets" in results[0]
    assert "health research" in results[0]["snippets"][0]


def test_search_integration_merges_content_with_name():
    """Content match on a project already matched by name should merge."""
    now_iso = datetime.now(timezone.utc).isoformat()
    fake_projects = [{"path": "/proj/health-app", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    content_hits = [{
        "path": "/proj/health-app",
        "session_id": 1,
        "agent": "claude",
        "title": "session",
        "snippet": "...health metrics dashboard...",
        "role": "assistant",
        "started_at": now_iso,
        "match_type": "like",
    }]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake_projects), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")), \
         mock.patch.object(cass_facade, "search_sessions", return_value=[]), \
         mock.patch.object(cass_facade, "search_content", return_value=content_hits):
        results = search_mod.search("health")

    assert len(results) == 1
    assert "name" in results[0]["match_fields"]
    assert "content" in results[0]["match_fields"]
    assert "snippets" in results[0]


# --- session_store: contract tests ---

class FakeStore:
    """Minimal SessionStore implementation for contract testing."""

    def __init__(self, projects=None, sessions=None, content=None, full_sessions=None):
        self._projects = projects or []
        self._sessions = sessions or []
        self._content = content or []
        self._full_sessions = full_sessions or {}

    def available(self) -> bool:
        return True

    def list_projects(self, detail: bool = False) -> list[dict]:
        return self._projects

    def recent_session_counts(self, days: int = 7) -> dict[str, int]:
        return {}

    def project_sessions(self, workspace_path: str, limit: int = 50) -> list[dict]:
        return [s for s in self._sessions if s.get("_path") == workspace_path][:limit]

    def session_details(self, session_ids: list) -> dict:
        return {}

    def search_sessions(self, query: str, limit: int = 20, sort: str = "newest") -> list[dict]:
        return []

    def search_content(self, query: str, limit: int = 20, sort: str = "newest") -> list[dict]:
        return [h for h in self._content if query.lower() in h.get("snippet", "").lower()][:limit]

    def get_session(
        self,
        session_id: str,
        *,
        all_branches: bool = False,
        include_tools: bool = True,
        roles: set[str] | None = None,
    ) -> dict | None:
        return self._full_sessions.get(str(session_id))


def test_store_swap_discover_uses_new_backend():
    """Swapping the store makes discover() use the new backend."""
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = FakeStore(projects=[
        {"path": "/fake/project", "agents": ["test_agent"], "session_count": 5, "last_active": now_iso},
    ])
    old_store = session_store._store
    try:
        session_store.set_store(fake)
        with mock.patch.object(cache, "load", return_value=None), \
             mock.patch.object(cache, "save"), \
             mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")):
            projects, total = discover.discover(limit=20)
        assert total == 1
        assert projects[0]["path"] == "/fake/project"
        assert projects[0]["agents"] == ["test_agent"]
    finally:
        session_store._store = old_store


def test_store_swap_search_uses_new_backend():
    """Swapping the store makes search() use the new backend for content."""
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = FakeStore(
        projects=[{"path": "/proj", "agents": ["claude"], "session_count": 1, "last_active": now_iso}],
        content=[{
            "path": "/proj", "session_id": "abc", "agent": "claude",
            "snippet": "found the hackathon discussion", "title": "test",
            "started_at": now_iso, "match_type": "fake",
        }],
    )
    old_store = session_store._store
    try:
        session_store.set_store(fake)
        with mock.patch.object(cache, "load", return_value=None), \
             mock.patch.object(cache, "save"), \
             mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")):
            results = search_mod.search("hackathon")
        assert len(results) == 1
        assert "content" in results[0]["match_fields"]
        assert "hackathon" in results[0]["snippets"][0]
    finally:
        session_store._store = old_store


def test_search_project_filter_scopes_multi_term_content():
    """--project should constrain multi-term content search to one workspace."""
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = FakeStore(
        projects=[
            {"path": "/proj/epic-odds", "agents": ["codex"], "session_count": 1, "last_active": now_iso},
            {"path": "/proj/other", "agents": ["codex"], "session_count": 1, "last_active": now_iso},
        ],
        sessions=[
            {"_path": "/proj/epic-odds", "session_id": "epic-1", "agent": "codex",
             "title": "odds", "started_at": now_iso},
            {"_path": "/proj/other", "session_id": "other-1", "agent": "codex",
             "title": "other", "started_at": now_iso},
        ],
        full_sessions={
            "epic-1": {"messages": [{"content": "football odds and soccer odds"}]},
            "other-1": {"messages": [{"content": "football odds and soccer odds"}]},
        },
    )
    old_store = session_store._store
    try:
        session_store.set_store(fake)
        with mock.patch.object(cache, "load", return_value=None), \
             mock.patch.object(cache, "save"), \
             mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")):
            results = search_mod.search(["football", "soccer"], project="epic-odds")
        assert len(results) == 1
        assert results[0]["path"] == "/proj/epic-odds"
        assert results[0]["query_terms"] == ["football", "soccer"]
    finally:
        session_store._store = old_store


def test_search_regex_all_terms():
    """Regex terms should be applied exactly during scanned searches."""
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = FakeStore(
        projects=[{"path": "/proj/epic-odds", "agents": ["codex"], "session_count": 2, "last_active": now_iso}],
        sessions=[
            {"_path": "/proj/epic-odds", "session_id": "match", "agent": "codex",
             "title": "match", "started_at": now_iso},
            {"_path": "/proj/epic-odds", "session_id": "miss", "agent": "codex",
             "title": "miss", "started_at": now_iso},
        ],
        full_sessions={
            "match": {"messages": [{"content": "football and soccer both appear"}]},
            "miss": {"messages": [{"content": "soccer only"}]},
        },
    )
    old_store = session_store._store
    try:
        session_store.set_store(fake)
        with mock.patch.object(cache, "load", return_value=None), \
             mock.patch.object(cache, "save"), \
             mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")):
            results = search_mod.search([r"foot(ball)?", "soccer"], project="epic", match="all", regex=True)
        assert len(results) == 1
        assert results[0]["matching_sessions"][0]["session_id"] == "match"
    finally:
        session_store._store = old_store


def test_store_swap_schedule_uses_new_backend():
    """Swapping the store makes schedule use the new backend for momentum."""
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = FakeStore(projects=[
        {"path": "/proj", "agents": ["claude"], "session_count": 3, "last_active": now_iso},
    ])
    old_store = session_store._store
    try:
        session_store.set_store(fake)
        with mock.patch.object(cache, "load", return_value=None), \
             mock.patch.object(cache, "save"), \
             mock.patch("pj.discover.annotations_path", return_value=Path("/nonexistent")):
            projects, _ = discover.discover(limit=20)
            scored = schedule.score_projects(projects)
        assert len(scored) == 1
        assert "score" in scored[0]
        assert scored[0]["score"] > 0
    finally:
        session_store._store = old_store


def test_store_default_is_fs():
    """Default store is fs_store (PJ_BACKEND unset)."""
    import pj.fs_store as fs_store_mod
    old_store = session_store._store
    try:
        session_store._store = None  # reset
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PJ_BACKEND", None)
            store = session_store.get_store()
        assert store is fs_store_mod
    finally:
        session_store._store = old_store


def test_store_cass_via_env():
    """PJ_BACKEND=cass selects cass_facade."""
    old_store = session_store._store
    try:
        session_store._store = None
        with mock.patch.dict(os.environ, {"PJ_BACKEND": "cass"}):
            store = session_store.get_store()
        assert store is cass_facade
    finally:
        session_store._store = old_store


def test_store_contract_list_projects_shape():
    """Any store's list_projects must return the expected dict shape."""
    fake = FakeStore(projects=[
        {"path": "/p", "agents": ["a"], "session_count": 1, "last_active": None},
    ])
    projects = fake.list_projects()
    assert len(projects) == 1
    p = projects[0]
    assert "path" in p and isinstance(p["path"], str)
    assert "agents" in p and isinstance(p["agents"], list)
    assert "session_count" in p and isinstance(p["session_count"], int)
    assert "last_active" in p  # can be None or str


def test_store_contract_empty_returns():
    """A store with no data returns empty collections, never raises."""
    fake = FakeStore()
    assert fake.list_projects() == []
    assert fake.recent_session_counts() == {}
    assert fake.project_sessions("/any") == []
    assert fake.session_details([]) == {}
    assert fake.search_sessions("any") == []
    assert fake.search_content("any") == []


def test_store_contract_cass_facade_satisfies_protocol():
    """cass_facade module has all SessionStore methods."""
    required = ["available", "list_projects", "recent_session_counts",
                "project_sessions", "session_details", "search_sessions",
                "search_content"]
    for method in required:
        assert hasattr(cass_facade, method), f"cass_facade missing {method}"
        assert callable(getattr(cass_facade, method)), f"cass_facade.{method} not callable"


def test_no_cass_imports_in_consumers():
    """No pj consumer module should import cass_facade directly."""
    import importlib
    consumer_modules = ["pj.discover", "pj.cli", "pj.search", "pj.schedule", "pj.cache"]
    for mod_name in consumer_modules:
        mod = importlib.import_module(mod_name)
        source = open(mod.__file__).read()
        assert "import cass_facade" not in source, f"{mod_name} still imports cass_facade directly"
        assert "from . import cass_facade" not in source and \
               "from .cass_facade" not in source, f"{mod_name} still imports from cass_facade"
