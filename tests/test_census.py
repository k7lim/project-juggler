from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest import mock

from pj import census, census_process, cli
from pj.census_server import CensusCache, HTML, make_handler


def _api_request(server, path, *, method="GET", body=None):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _test_server():
    cache = CensusCache(
        limit=7,
        check_interval=0,
        snapshot_fn=lambda limit: {"rows": [], "meta": census.summarize([])},
        signatures_fn=lambda: {},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cache))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


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


def test_census_server_search_endpoint_maps_cli_query_semantics():
    server, thread = _test_server()
    try:
        with mock.patch(
            "pj.census_server.search_mod.search",
            return_value=[
                {
                    "id": "p1",
                    "name": "sports",
                    "path": "/tmp/sports",
                    "matching_sessions": [
                        {
                            "session_id": "sess-1",
                            "agent": "codex",
                            "title": "sports search",
                            "snippet": "discussed soccer search",
                        }
                    ],
                }
            ],
        ) as search:
            status, payload = _api_request(
                server,
                "/api/search?q=sport&q=soccer&limit=3&sort=relevance&project=league&match=all&regex=1",
            )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert status == 200
    search.assert_called_once_with(
        ["sport", "soccer"],
        limit=3,
        sort="relevance",
        project="league",
        match="all",
        regex=True,
    )
    assert payload["success"] is True
    assert payload["data"][0]["name"] == "sports"
    assert payload["data"][0]["resume_cmd"] == "cd /tmp/sports && codex resume sess-1"
    assert payload["data"][0]["matching_sessions"][0]["resume_cmd"] == "cd /tmp/sports && codex resume sess-1"
    assert payload["meta"]["query"] == ["sport", "soccer"]
    assert payload["meta"]["total"] == 1


def test_census_dashboard_has_live_search_and_separate_table_filter():
    assert 'id="search" placeholder="Search projects and sessions..."' in HTML
    assert 'id="tableFilter" placeholder="Filter census table..."' in HTML
    assert "setTimeout(() => runSearch(q), 300)" in HTML
    assert "fetch(`/api/search?q=${encodeURIComponent(q)}&limit=8&sort=relevance`" in HTML
    assert "matching_sessions" in HTML
    assert "resume_cmd" in HTML


def test_census_server_show_chats_and_chat_endpoints_map_to_session_store():
    project = {"id": "abc123", "name": "proj", "path": "/tmp/proj", "agent": "codex"}
    sessions = [{"session_id": "sess-1", "agent": "codex", "title": "Build API"}]
    store = mock.Mock()
    store.project_sessions.return_value = [dict(sessions[0])]
    store.session_details.return_value = {"sess-1": {"models": ["gpt-5"], "versions": ["0.2.2"]}}
    store.get_session.return_value = {
        "session_id": "sess-1",
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
        ],
    }

    server, thread = _test_server()
    try:
        with mock.patch("pj.census_server.discover.resolve_project", return_value=project), \
             mock.patch("pj.project_sessions.get_store", return_value=store), \
             mock.patch("pj.census_server.get_store", return_value=store):
            show_status, show_payload = _api_request(server, "/api/show?project=abc&sessions=1")
            chats_status, chats_payload = _api_request(server, "/api/chats?project=abc&limit=1")
            chat_status, chat_payload = _api_request(
                server,
                "/api/chat/sess-1?roles=user,assistant&no_tools=1&last=2&offset=1&limit=1",
            )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert show_status == 200
    assert show_payload["data"]["path"] == "/tmp/proj"
    assert show_payload["data"]["sessions"][0]["models"] == ["gpt-5"]
    assert show_payload["data"]["resume_cmd"] == "cd /tmp/proj && codex resume sess-1"

    assert chats_status == 200
    assert chats_payload["data"][0]["session_id"] == "sess-1"
    assert chats_payload["meta"]["project"] == {"id": "abc123", "name": "proj", "path": "/tmp/proj"}
    assert chats_payload["meta"]["total"] == 1

    assert chat_status == 200
    store.get_session.assert_called_once_with(
        "sess-1",
        all_branches=False,
        include_tools=False,
        roles={"user", "assistant"},
    )
    assert chat_payload["data"]["messages"] == [{"role": "user", "content": "third"}]
    assert chat_payload["meta"]["total_messages"] == 2
    assert chat_payload["meta"]["offset"] == 1
    assert chat_payload["meta"]["limit"] == 1


def test_census_server_annotation_endpoints_append_only_via_annotate_api(tmp_path):
    ann_path = tmp_path / "annotations.jsonl"
    project = {"id": "abc123", "name": "proj", "path": "/tmp/proj"}

    server, thread = _test_server()
    try:
        with mock.patch("pj.annotate.annotations_path", return_value=ann_path), \
             mock.patch("pj.census_server.discover.resolve_project", return_value=project):
            note_status, note_payload = _api_request(
                server,
                "/api/annotations/note",
                method="POST",
                body={"project": "abc", "text": "next: document API"},
            )
            priority_status, priority_payload = _api_request(
                server,
                "/api/annotations/prioritize",
                method="POST",
                body={"project": "abc", "level": "high"},
            )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert note_status == 200
    assert note_payload["data"]["type"] == "note"
    assert note_payload["data"]["project_path"] == "/tmp/proj"
    assert priority_status == 200
    assert priority_payload["data"]["type"] == "priority"

    lines = ann_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["text"] == "next: document API"
    assert json.loads(lines[1])["value"] == "high"


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
