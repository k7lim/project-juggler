from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from pj import census, cli
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
