from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from pj import census, census_process, cli
from pj.census_server import CensusCache


def test_normalize_project_maps_detail_fields(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".beads").mkdir()
    (project_dir / ".git").mkdir()

    row = census.normalize_project(
        {
            "id": "abc123",
            "name": "project",
            "path": str(project_dir),
            "state": "active",
            "session_count": 3,
            "agents": ["codex", "claude_code"],
            "last_active": "2026-05-14T10:21:27.759000+00:00",
            "first_active": "2026-05-14T03:25:01.048000+00:00",
            "total_duration_secs": 5400,
            "models": ["gpt-5.5"],
            "priority": "high",
            "tags": ["dashboard"],
            "latest_note": "next: ship it",
        }
    )

    assert row["sessions"] == 3
    assert row["duration_hrs"] == 1.5
    assert row["last_active"] == "2026-05-14 10:21"
    assert row["first_session"] == "2026-05-14"
    assert row["agents"] == "codex, claude_code"
    assert row["models"] == "gpt-5.5"
    assert row["has_beads"] is True
    assert row["has_git"] is True
    assert row["priority"] == "high"
    assert row["tags"] == "dashboard"
    assert row["note"] == "next: ship it"


def test_normalize_project_derives_origin_and_category():
    row = census.normalize_project(
        {
            "id": "abc123",
            "path": "/workspace/sandbox/research/network-forensics-app",
            "state": "active",
            "session_count": 1,
            "last_active": None,
        }
    )

    assert row["origin"] == "yolobox"
    assert row["category"] == "sandbox"

    top_level = census.normalize_project(
        {
            "id": "def456",
            "path": "/Users/kevin/Development/sandbox",
            "state": "active",
            "session_count": 1,
            "last_active": None,
        }
    )
    assert top_level["category"] == "sandbox"


def test_snapshot_uses_detail_discovery():
    projects = [
        {
            "id": "abc123",
            "name": "foo",
            "path": "/Users/kevin/Development/external/foo",
            "state": "active",
            "session_count": 2,
            "last_active": "2026-05-14T10:21:27+00:00",
            "first_active": "2026-05-14T09:00:00+00:00",
            "total_duration_secs": 3600,
        }
    ]

    with mock.patch("pj.census.discover.discover", return_value=(projects, 1)) as discover_mock:
        snap = census.snapshot(limit=50)

    discover_mock.assert_called_once_with(limit=50, detail=True)
    assert snap["rows"][0]["name"] == "foo"
    assert snap["rows"][0]["category"] == "external"
    assert snap["meta"]["total"] == 1
    assert snap["meta"]["session_total"] == 2
    assert snap["meta"]["duration_hrs_total"] == 1.0


def test_census_cache_refreshes_only_when_signatures_change():
    signatures = iter([{"size": 1}, {"size": 1}, {"size": 2}])
    calls = []

    def snapshot_fn(limit):
        calls.append(limit)
        return {"rows": [], "meta": census.summarize([])}

    cache = CensusCache(
        limit=7,
        check_interval=0,
        snapshot_fn=snapshot_fn,
        signatures_fn=lambda: next(signatures),
    )

    first = cache.get()
    second = cache.get()
    third = cache.get()

    assert len(calls) == 2
    assert first["meta"]["signature_changed"] is True
    assert second["meta"]["signature_changed"] is False
    assert third["meta"]["signature_changed"] is True
    assert calls == [7, 7]


def test_cli_census_outputs_dashboard_json(capsys):
    snap = {
        "rows": [{"name": "foo", "path": "/tmp/foo"}],
        "meta": {"total": 1, "generated_at": "2026-05-14T00:00:00+00:00"},
    }

    with mock.patch("pj.census.snapshot", return_value=snap):
        cli.main(["census", "--limit", "1"])

    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is True
    assert parsed["data"] == snap["rows"]
    assert parsed["meta"]["total"] == 1


def test_census_process_status_reports_running_without_control_token(tmp_path, monkeypatch):
    monkeypatch.setenv("PJ_DATA_DIR", str(tmp_path))
    census_process.state_path().write_text(
        json.dumps(
            {
                "pid": 12345,
                "host": "127.0.0.1",
                "port": 8765,
                "url": "http://127.0.0.1:8765/",
                "limit": 50,
                "check_interval": 10,
                "control_token": "secret-token",
            }
        ),
        encoding="utf-8",
    )

    with mock.patch("pj.census_process._is_process_alive", return_value=True), \
         mock.patch(
             "pj.census_process._request_json",
             return_value={"success": True, "data": {"status": "running"}, "meta": {}},
         ):
        status = census_process.status()

    assert status["running"] is True
    assert status["status"] == "running"
    assert status["url"] == "http://127.0.0.1:8765/"
    assert "control_token" not in status


def test_census_process_liveness_treats_zombie_as_stopped():
    result = mock.Mock(returncode=0, stdout="Zs\n")

    with mock.patch("pj.census_process.os.kill"), \
         mock.patch("pj.census_process.subprocess.run", return_value=result):
        assert census_process._is_process_alive(12345) is False


def test_census_process_start_launches_background_server(tmp_path, monkeypatch):
    monkeypatch.setenv("PJ_DATA_DIR", str(tmp_path))
    process = mock.Mock(pid=4321)
    process.poll.return_value = None

    with mock.patch("pj.census_process.subprocess.Popen", return_value=process) as popen, \
         mock.patch("pj.census_process._is_process_alive", return_value=True), \
         mock.patch(
             "pj.census_process._request_json",
             return_value={"success": True, "data": {"status": "running"}, "meta": {}},
         ):
        result = census_process.start(host="127.0.0.1", port=9876, limit=5, check_interval=3)

    assert result["running"] is True
    assert result["started"] is True
    assert result["url"] == "http://127.0.0.1:9876/"
    assert "control_token" not in result
    stored = json.loads(census_process.state_path().read_text(encoding="utf-8"))
    assert stored["pid"] == 4321
    assert stored["control_token"]
    popen.assert_called_once()


def test_census_process_stop_reports_final_stopped_state(tmp_path, monkeypatch):
    monkeypatch.setenv("PJ_DATA_DIR", str(tmp_path))
    census_process.state_path().write_text(
        json.dumps(
            {
                "pid": 12345,
                "host": "127.0.0.1",
                "port": 8765,
                "url": "http://127.0.0.1:8765/",
                "control_token": "secret-token",
            }
        ),
        encoding="utf-8",
    )

    with mock.patch("pj.census_process._is_process_alive", side_effect=[True, False]), \
         mock.patch(
             "pj.census_process._request_json",
             return_value={"success": True, "data": {"status": "running"}, "meta": {}},
         ), \
         mock.patch(
             "pj.census_process._post_json",
             return_value={"success": True, "data": {"status": "stopping"}, "meta": {}},
         ):
        result = census_process.stop()

    assert result["stopped"] is True
    assert result["running"] is False
    assert result["pid_alive"] is False
    assert result["health_ok"] is False
    assert "health" not in result
    assert not census_process.state_path().exists()


def test_cli_census_status_outputs_structured_json(capsys):
    status = {
        "status": "running",
        "running": True,
        "pid": 12345,
        "url": "http://127.0.0.1:8765/",
    }

    with mock.patch("pj.census_process.status", return_value=status):
        cli.main(["census", "status"])

    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is True
    assert parsed["data"] == status
