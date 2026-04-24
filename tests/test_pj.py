"""Tests for pj: envelope, cass_facade, state, discover, cache, cli, pretty, annotate, schedule, search, resume."""
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
import pj.annotate as annotate
import pj.schedule as schedule
import pj.resume as resume
import pj.search as search_mod


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


# --- annotate ---

def test_annotate_note():
    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch.object(annotate, "ANNOTATIONS_PATH", ann_path):
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
        with mock.patch.object(annotate, "ANNOTATIONS_PATH", ann_path):
            event = annotate.prioritize("/tmp/proj", "high")

    assert event["type"] == "priority"
    assert event["value"] == "high"


def test_annotate_prioritize_invalid():
    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch.object(annotate, "ANNOTATIONS_PATH", ann_path):
            try:
                annotate.prioritize("/tmp/proj", "urgent")
                assert False, "Should have raised ValueError"
            except ValueError as e:
                assert "urgent" in str(e)

        assert not ann_path.exists()


def test_annotate_archive():
    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch.object(annotate, "ANNOTATIONS_PATH", ann_path):
            event = annotate.archive("/tmp/proj")

    assert event["type"] == "archive"
    assert event["project_id"] == discover.project_id("/tmp/proj")


def test_annotate_tag():
    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch.object(annotate, "ANNOTATIONS_PATH", ann_path):
            event = annotate.tag("/tmp/proj", "infra")

    assert event["type"] == "tag"
    assert event["tag"] == "infra"


def test_annotate_append_only():
    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch.object(annotate, "ANNOTATIONS_PATH", ann_path):
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
        with mock.patch.object(annotate, "ANNOTATIONS_PATH", ann_path):
            annotate.note("/tmp/proj", "test")

        assert ann_path.exists()


# --- discover annotation replay integration ---

def test_discover_replays_annotations_integration():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/tagged-proj", "agents": ["claude"], "session_count": 2, "last_active": now_iso}]

    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch.object(annotate, "ANNOTATIONS_PATH", ann_path):
            annotate.prioritize("/tmp/tagged-proj", "high")
            annotate.tag("/tmp/tagged-proj", "ml")
            annotate.tag("/tmp/tagged-proj", "infra")
            annotate.note("/tmp/tagged-proj", "next: retrain model")

        with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
             mock.patch.object(cache, "load", return_value=None), \
             mock.patch.object(cache, "save"), \
             mock.patch.object(discover, "ANNOTATIONS_PATH", ann_path):
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
        with mock.patch.object(annotate, "ANNOTATIONS_PATH", ann_path):
            annotate.archive("/tmp/arch-proj")

        with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
             mock.patch.object(cache, "load", return_value=None), \
             mock.patch.object(cache, "save"), \
             mock.patch.object(discover, "ANNOTATIONS_PATH", ann_path):
            projects, total = discover.discover(state_filter="archived")

        assert total == 1
        assert projects[0]["state"] == "archived"


# --- cli actuator commands ---

def test_cli_note(capsys):
    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch.object(annotate, "ANNOTATIONS_PATH", ann_path):
            cli.main(["note", "/tmp/proj", "remember to refactor"])

    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["data"]["type"] == "note"
    assert parsed["data"]["text"] == "remember to refactor"


def test_cli_prioritize(capsys):
    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch.object(annotate, "ANNOTATIONS_PATH", ann_path):
            cli.main(["prioritize", "/tmp/proj", "high"])

    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["data"]["type"] == "priority"
    assert parsed["data"]["value"] == "high"


def test_cli_archive(capsys):
    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch.object(annotate, "ANNOTATIONS_PATH", ann_path):
            cli.main(["archive", "/tmp/proj"])

    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["data"]["type"] == "archive"


def test_cli_tag(capsys):
    with tempfile.TemporaryDirectory() as tmpdir:
        ann_path = Path(tmpdir) / "annotations.jsonl"
        with mock.patch.object(annotate, "ANNOTATIONS_PATH", ann_path):
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
    with mock.patch.object(cass_facade, "db_path", return_value=None):
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
        with mock.patch.object(cass_facade, "db_path", return_value=db):
            counts = cass_facade.recent_session_counts(days=7)
    assert "/home/user/project-a" in counts
    assert counts["/home/user/project-a"] >= 1


def test_cass_facade_recent_session_counts_no_db():
    with mock.patch.object(cass_facade, "db_path", return_value=None):
        assert cass_facade.recent_session_counts() == {}


def test_cass_facade_search_sessions():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_search.db"
        _create_test_db(db)
        with mock.patch.object(cass_facade, "db_path", return_value=db):
            results = cass_facade.search_sessions("t1")
    assert len(results) >= 1
    assert results[0]["title"] == "t1"
    assert results[0]["path"] == "/home/user/project-a"


def test_cass_facade_search_sessions_no_match():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_search.db"
        _create_test_db(db)
        with mock.patch.object(cass_facade, "db_path", return_value=db):
            results = cass_facade.search_sessions("nonexistent_query_xyz")
    assert results == []


def test_cass_facade_search_sessions_no_db():
    with mock.patch.object(cass_facade, "db_path", return_value=None):
        assert cass_facade.search_sessions("test") == []


# --- search ---

def test_search_by_name():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = [{"path": "/tmp/api-gateway", "agents": ["claude"], "session_count": 1, "last_active": now_iso}]
    with mock.patch.object(cass_facade, "list_projects", return_value=fake), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch.object(discover, "ANNOTATIONS_PATH", Path("/nonexistent")), \
         mock.patch.object(cass_facade, "search_sessions", return_value=[]):
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
             mock.patch.object(discover, "ANNOTATIONS_PATH", ann_path), \
             mock.patch.object(cass_facade, "search_sessions", return_value=[]):
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
         mock.patch.object(discover, "ANNOTATIONS_PATH", Path("/nonexistent")), \
         mock.patch.object(cass_facade, "search_sessions", return_value=session_hits):
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
         mock.patch.object(discover, "ANNOTATIONS_PATH", Path("/nonexistent")), \
         mock.patch.object(cass_facade, "search_sessions", return_value=[]):
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
             mock.patch.object(discover, "ANNOTATIONS_PATH", ann_path), \
             mock.patch.object(cass_facade, "search_sessions", return_value=[]):
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
         mock.patch.object(discover, "ANNOTATIONS_PATH", Path("/nonexistent")), \
         mock.patch.object(cass_facade, "search_sessions", return_value=session_hits):
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
         mock.patch.object(discover, "ANNOTATIONS_PATH", Path("/nonexistent")), \
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
         mock.patch.object(discover, "ANNOTATIONS_PATH", Path("/nonexistent")), \
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
         mock.patch.object(discover, "ANNOTATIONS_PATH", Path("/nonexistent")), \
         mock.patch.object(cass_facade, "search_sessions", return_value=[]):
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
         mock.patch.object(discover, "ANNOTATIONS_PATH", Path("/nonexistent")), \
         mock.patch.object(cass_facade, "search_sessions", return_value=[]):
        cli.main(["search", "--pretty", "search-pretty"])

    out = capsys.readouterr().out
    assert "search-pretty" in out


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
             mock.patch.object(discover, "ANNOTATIONS_PATH", ann_path):
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
         mock.patch.object(discover, "ANNOTATIONS_PATH", Path("/nonexistent")):
        projects, _ = discover.discover()
    assert projects[0]["latest_note"] is None


# --- resume ---

def test_resume_command_claude():
    cmd = resume.resume_command("claude", "abc-123")
    assert cmd == "claude --resume abc-123"


def test_resume_command_codex():
    cmd = resume.resume_command("codex", "sess-456")
    assert cmd == "codex --resume sess-456"


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
        with mock.patch.object(cass_facade, "db_path", return_value=db):
            sessions = cass_facade.project_sessions("/home/user/project-a")
    assert len(sessions) == 3
    assert sessions[0]["agent"] in ("claude", "codex")
    assert sessions[0]["started_at"] >= sessions[1]["started_at"]


def test_cass_facade_project_sessions_no_match():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_search.db"
        _create_test_db(db)
        with mock.patch.object(cass_facade, "db_path", return_value=db):
            sessions = cass_facade.project_sessions("/nonexistent/path")
    assert sessions == []


def test_cass_facade_project_sessions_no_db():
    with mock.patch.object(cass_facade, "db_path", return_value=None):
        assert cass_facade.project_sessions("/any/path") == []


# --- discover resolve_project ---

def _mock_discover_with(fake_projects):
    """Context manager that mocks discover to return fake_projects."""
    return (
        mock.patch.object(cass_facade, "list_projects", return_value=fake_projects),
        mock.patch.object(cache, "load", return_value=None),
        mock.patch.object(cache, "save"),
        mock.patch.object(discover, "ANNOTATIONS_PATH", Path("/nonexistent")),
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
         mock.patch.object(discover, "ANNOTATIONS_PATH", Path("/nonexistent")), \
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
         mock.patch.object(discover, "ANNOTATIONS_PATH", Path("/nonexistent")), \
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
         mock.patch.object(discover, "ANNOTATIONS_PATH", Path("/nonexistent")):
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
         mock.patch.object(discover, "ANNOTATIONS_PATH", Path("/nonexistent")), \
         mock.patch.object(cass_facade, "project_sessions", return_value=fake_sessions):
        cli.main(["resume", "resume-test"])

    out = capsys.readouterr().out.strip()
    assert out == "cd /tmp/resume-test && codex --resume sess-abc"


def test_cli_resume_not_found(capsys):
    with mock.patch.object(cass_facade, "list_projects", return_value=[]), \
         mock.patch.object(cache, "load", return_value=None), \
         mock.patch.object(cache, "save"), \
         mock.patch.object(discover, "ANNOTATIONS_PATH", Path("/nonexistent")):
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
         mock.patch.object(discover, "ANNOTATIONS_PATH", Path("/nonexistent")), \
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
