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
from pj.paths import annotations_path
from pj.project_sessions import resolve_project_detail


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
            "web_hint": {
                "type": "web_app",
                "confidence": "medium",
                "evidence": ["vite.config.ts"],
            },
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
    assert row["web_hint"]["evidence"] == ["vite.config.ts"]


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


def test_snapshot_without_ports_does_not_call_runtime_port_discovery():
    projects = [{"id": "abc123", "name": "foo", "path": "/tmp/foo"}]

    with mock.patch("pj.census.discover.discover", return_value=(projects, 1)), \
         mock.patch("pj.census.runtime_ports.discover_ports") as ports_mock:
        snap = census.snapshot(limit=50)

    ports_mock.assert_not_called()
    assert "live_urls" not in snap["rows"][0]
    assert "live_port_count" not in snap["rows"][0]
    assert "ports_included" not in snap["meta"]


def test_snapshot_include_ports_overlays_strong_project_matches():
    projects = [
        {"id": "p1", "name": "foo", "path": "/tmp/foo"},
        {"id": "p2", "name": "bar", "path": "/tmp/bar"},
    ]
    records = [
        {
            "project_id": "p1",
            "path": "/tmp/foo",
            "live_urls": ["http://127.0.0.1:3000/", "http://localhost:3000/"],
            "port": 3000,
            "confidence": "high",
            "source": "lsof",
        },
        {
            "project_id": "p1",
            "path": "/tmp/foo",
            "live_urls": ["http://127.0.0.1:3000/"],
            "port": 3001,
            "confidence": "medium",
            "source": "ss",
        },
    ]

    with mock.patch("pj.census.discover.discover", return_value=(projects, 2)), \
         mock.patch(
             "pj.census.runtime_ports.discover_ports",
             return_value=(records, {"total": 2, "sources": ["lsof", "ss"], "warnings": ["ss partial"]}),
         ) as ports_mock:
        snap = census.snapshot(limit=50, include_ports=True)

    ports_mock.assert_called_once_with(projects=projects)
    foo = snap["rows"][0]
    assert foo["ports"] == records
    assert foo["live_urls"] == ["http://127.0.0.1:3000/", "http://localhost:3000/"]
    assert foo["live_port_count"] == 2
    assert snap["rows"][1]["ports"] == []
    assert snap["rows"][1]["live_urls"] == []
    assert snap["rows"][1]["live_port_count"] == 0
    assert snap["meta"]["ports_included"] is True
    assert snap["meta"]["ports_total"] == 2
    assert snap["meta"]["ports_sources"] == ["lsof", "ss"]
    assert snap["meta"]["warnings"] == ["ss partial"]


def test_snapshot_include_ports_keeps_no_match_out_of_rows():
    projects = [{"id": "p1", "name": "foo", "path": "/tmp/foo"}]
    records = [
        {
            "project_id": None,
            "path": None,
            "live_urls": ["http://127.0.0.1:5432/"],
            "port": 5432,
            "confidence": "unknown",
            "source": "lsof",
        }
    ]

    with mock.patch("pj.census.discover.discover", return_value=(projects, 1)), \
         mock.patch(
             "pj.census.runtime_ports.discover_ports",
             return_value=(records, {"total": 1, "sources": ["lsof"], "warnings": []}),
         ):
        snap = census.snapshot(include_ports=True)

    assert snap["rows"][0]["ports"] == []
    assert snap["rows"][0]["live_urls"] == []
    assert snap["rows"][0]["live_port_count"] == 0
    assert snap["meta"]["ports_total"] == 1


def test_snapshot_include_ports_preserves_weak_match_and_ignores_ambiguous_record():
    projects = [{"id": "p1", "name": "foo", "path": "/tmp/foo"}]
    weak = {
        "project_id": "p1",
        "path": "/tmp/foo",
        "live_urls": ["http://127.0.0.1:5173/"],
        "port": 5173,
        "confidence": "low",
        "source": "lsof",
    }
    ambiguous = {
        "project_id": None,
        "path": None,
        "live_urls": ["http://127.0.0.1:8888/"],
        "port": 8888,
        "confidence": "low",
        "source": "lsof",
    }

    with mock.patch("pj.census.discover.discover", return_value=(projects, 1)), \
         mock.patch(
             "pj.census.runtime_ports.discover_ports",
             return_value=([weak, ambiguous], {"total": 2, "sources": ["lsof"], "warnings": []}),
         ):
        snap = census.snapshot(include_ports=True)

    assert snap["rows"][0]["ports"] == [weak]
    assert snap["rows"][0]["live_urls"] == ["http://127.0.0.1:5173/"]
    assert snap["rows"][0]["live_port_count"] == 1
    assert snap["meta"]["ports_total"] == 2


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


def test_census_cache_keeps_port_enriched_snapshot_separate():
    calls = []

    def snapshot_fn(limit, *, include_ports=False):
        calls.append((limit, include_ports))
        base_row = {"id": "p1", "state": "active", "category": "sandbox", "origin": "mac"}
        rows = [{**base_row, "live_port_count": 1}] if include_ports else [base_row]
        return {"rows": rows, "meta": census.summarize(rows)}

    cache = CensusCache(
        limit=7,
        check_interval=60,
        snapshot_fn=snapshot_fn,
        signatures_fn=lambda: {"size": 1},
    )

    plain = cache.get()
    enriched = cache.get(include_ports=True)
    plain_again = cache.get()

    assert calls == [(7, False), (7, True)]
    assert "live_port_count" not in plain["rows"][0]
    assert enriched["rows"][0]["live_port_count"] == 1
    assert "live_port_count" not in plain_again["rows"][0]


def test_census_cache_rescans_port_enriched_snapshot_each_request():
    calls = []

    def snapshot_fn(limit, *, include_ports=False):
        calls.append((limit, include_ports))
        base_row = {"id": "p1", "state": "active", "category": "sandbox", "origin": "mac"}
        live_count = len(calls) if include_ports else 0
        rows = [{**base_row, "live_port_count": live_count}] if include_ports else [base_row]
        return {"rows": rows, "meta": census.summarize(rows)}

    cache = CensusCache(
        limit=7,
        check_interval=60,
        snapshot_fn=snapshot_fn,
        signatures_fn=lambda: {"size": 1},
    )

    first = cache.get(include_ports=True)
    second = cache.get(include_ports=True)

    assert calls == [(7, True), (7, True)]
    assert first["rows"][0]["live_port_count"] == 1
    assert second["rows"][0]["live_port_count"] == 2


def test_census_server_include_ports_query_passes_through_to_cache():
    calls = []

    def snapshot_fn(limit, *, include_ports=False):
        calls.append((limit, include_ports))
        base_row = {"id": "p1", "state": "active", "category": "sandbox", "origin": "mac"}
        rows = [{**base_row, "live_port_count": 1}] if include_ports else [base_row]
        meta = census.summarize(rows)
        if include_ports:
            meta["ports_included"] = True
        return {"rows": rows, "meta": meta}

    cache = CensusCache(
        limit=7,
        check_interval=0,
        snapshot_fn=snapshot_fn,
        signatures_fn=lambda: {"size": 1},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cache))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, payload = _api_request(server, "/api/census?include_ports=1")
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert status == 200
    assert calls == [(7, True)]
    assert payload["data"][0]["live_port_count"] == 1
    assert payload["meta"]["ports_included"] is True


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


def test_census_server_next_endpoint_uses_schedule_flow():
    projects = [
        {"id": "p1", "name": "alpha", "path": "/tmp/alpha", "state": "active", "priority": "high"},
        {"id": "p2", "name": "beta", "path": "/tmp/beta", "state": "stale", "priority": "none"},
    ]
    scored = [
        {**projects[0], "score": 0.9, "reason": "high priority", "factors": {"priority": 1.0}},
        {**projects[1], "score": 0.4, "reason": "baseline score", "factors": {"priority": 0.0}},
    ]
    server, thread = _test_server()
    try:
        with mock.patch("pj.census_server.discover.discover", return_value=(projects, 2)) as discover_mock, \
             mock.patch("pj.census_server.schedule.score_projects", return_value=scored) as score_mock:
            status, payload = _api_request(server, "/api/next?limit=1")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert status == 200
    discover_mock.assert_called_once_with(limit=9999)
    score_mock.assert_called_once_with(projects)
    assert payload["success"] is True
    assert payload["data"] == scored[:1]
    assert payload["meta"]["limit"] == 1
    assert payload["meta"]["total"] == 1
    assert "latency_ms" in payload["meta"]


def test_resolve_project_detail_uses_discovery_rules_for_id_name_and_path():
    projects = [
        {
            "id": "abc12345",
            "name": "proj",
            "path": "/tmp/proj",
            "state": "active",
            "session_count": 1,
            "agents": ["codex"],
        }
    ]
    store = mock.Mock()
    store.project_sessions.return_value = [{"session_id": "sess-1", "agent": "codex", "title": "Build drawer"}]
    store.session_details.return_value = {"sess-1": {"models": ["gpt-5"], "versions": ["0.2.2"]}}

    with mock.patch("pj.discover.discover", return_value=(projects, 1)), \
         mock.patch("pj.project_sessions.get_store", return_value=store):
        by_id = resolve_project_detail("abc", 3)
        by_name = resolve_project_detail("proj", 3)
        by_path = resolve_project_detail("/tmp/proj", 3)

    assert by_id["id"] == "abc12345"
    assert by_name["path"] == "/tmp/proj"
    assert by_path["sessions"][0]["models"] == ["gpt-5"]
    assert by_path["resume_cmd"] == "cd /tmp/proj && codex resume sess-1"


def test_census_dashboard_has_live_search_table_filter_and_detail_drawer():
    assert 'id="search" placeholder="Search projects and sessions..."' in HTML
    assert 'id="tableFilter" placeholder="Filter census table..."' in HTML
    assert 'id="projectDrawer"' in HTML
    assert 'id="drawerClose"' in HTML
    assert 'data-project-id="${esc(r.id)}"' in HTML
    assert "openProjectDetail(row.dataset.projectId)" in HTML
    assert "fetch(`/api/show?project=${encodeURIComponent(projectId)}&sessions=10`" in HTML
    assert "renderProjectDetail(payload.data || {})" in HTML
    assert "setTimeout(() => runSearch(q), 300)" in HTML
    assert "fetch(`/api/search?q=${encodeURIComponent(q)}&limit=8&sort=relevance`" in HTML
    assert "matching_sessions" in HTML
    assert "resume_cmd" in HTML


def test_census_dashboard_has_next_queue_tab_and_preserves_census_view():
    assert 'data-view="census">Census</button>' in HTML
    assert 'data-view="next">Next Queue</button>' in HTML
    assert 'id="censusView"' in HTML
    assert 'id="nextView" hidden' in HTML
    assert '<table id="censusTable">' in HTML
    assert '<table id="nextTable">' in HTML
    assert 'fetch(`/api/next?limit=20${force ? "&refresh=1" : ""}`' in HTML
    assert "function renderNextQueue()" in HTML
    assert 'class="project-link" data-project-id="${esc(ref)}"' in HTML
    assert "openProjectDetail(projectId)" in HTML
    assert 'document.querySelectorAll("#censusTable th")' in HTML
    assert 'if (activeView === "next") loadNextQueue(true);' in HTML


def test_census_dashboard_has_lazy_session_transcript_viewer():
    assert 'class="transcript-open" data-session-id="${esc(sessionId)}"' in HTML
    assert 'class="transcript-roles" aria-label="Transcript roles"' in HTML
    assert '<option value="user,assistant">User + assistant</option>' in HTML
    assert 'class="transcript-last" value="50"' in HTML
    assert 'class="transcript-hide-tools" checked' in HTML
    assert "async function openTranscript(button)" in HTML
    assert 'fetch(`/api/chat/${encodeURIComponent(sessionId)}${query ? `?${query}` : ""}`' in HTML
    assert 'params.set("roles", roles)' in HTML
    assert 'params.set("last", last)' in HTML
    assert 'params.set("no_tools", "1")' in HTML
    assert "renderTranscript((payload.data || {}).messages || [])" in HTML
    assert 'document.getElementById("projectDrawer").addEventListener("click"' in HTML


def test_census_dashboard_has_annotation_actions_and_archive_confirmation():
    assert 'class="annotation-actions" data-project-id="${esc(projectId)}"' in HTML
    assert 'class="annotation-note" placeholder="Add note"' in HTML
    assert 'class="annotation-priority" aria-label="Priority"' in HTML
    assert 'data-action="prioritize"' in HTML
    assert 'class="annotation-tag" placeholder="Tag"' in HTML
    assert 'data-action="tag"' in HTML
    assert 'class="annotation-archive-confirm"> Confirm archive' in HTML
    assert 'data-action="archive" disabled' in HTML
    assert 'if (action === "archive" && !confirmArchive?.checked)' in HTML
    assert 'fetch(`/api/annotations/${action}`' in HTML
    assert 'method: "POST"' in HTML
    assert 'if (activeView === "next") await loadNextQueue(true);' in HTML
    assert "else await loadData(true)" in HTML
    assert "if (activeProjectId) await openProjectDetail(activeProjectId)" in HTML
    assert 'archiveButton.disabled = !confirmArchive.checked' in HTML


def test_census_dashboard_requests_server_provided_live_urls():
    assert 'fetch(`/api/census?include_ports=1${force ? "&refresh=1" : ""}`' in HTML
    assert "liveUrlsCell(r.live_urls)" in HTML
    assert "r.ports" not in HTML
    assert "fetch(`http" not in HTML


def test_census_dashboard_renders_compact_live_links_cleanly():
    assert '<th data-key="live_port_count" data-type="num">Live' in HTML
    assert 'if (!Array.isArray(urls) || urls.length === 0) return "";' in HTML
    assert 'href="${esc(url)}"' in HTML
    assert 'target="_blank" rel="noopener noreferrer"' in HTML
    assert "compactLiveUrl(url)" in HTML
    assert "parsed.port ? `${parsed.hostname}:${parsed.port}` : parsed.host" in HTML
    assert 'if (event.target.closest("a")) return;' in HTML


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
        with mock.patch("pj.project_sessions.discover.resolve_project", return_value=project), \
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


def test_census_server_annotation_endpoints_append_only_via_annotate_api(tmp_path, monkeypatch):
    monkeypatch.setenv("PJ_DATA_DIR", str(tmp_path))
    project = {"id": "abc123", "name": "proj", "path": "/tmp/proj"}

    server, thread = _test_server()
    try:
        with mock.patch("pj.census_server.discover.resolve_project", return_value=project):
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
            tag_status, tag_payload = _api_request(
                server,
                "/api/annotations/tag",
                method="POST",
                body={"project": "abc", "tag": "dashboard"},
            )
            archive_status, archive_payload = _api_request(
                server,
                "/api/annotations/archive",
                method="POST",
                body={"project": "abc"},
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
    assert tag_status == 200
    assert tag_payload["data"]["type"] == "tag"
    assert archive_status == 200
    assert archive_payload["data"]["type"] == "archive"

    ann_path = annotations_path()
    lines = ann_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 4
    events = [json.loads(line) for line in lines]
    assert [event["type"] for event in events] == ["note", "priority", "tag", "archive"]
    assert [event["project_path"] for event in events] == ["/tmp/proj"] * 4
    assert events[0]["text"] == "next: document API"
    assert events[1]["value"] == "high"
    assert events[2]["tag"] == "dashboard"
    assert "text" not in events[3]


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


def test_cli_census_include_ports_passes_snapshot_flag(capsys):
    snap = {
        "rows": [{"name": "foo", "path": "/tmp/foo", "live_urls": [], "live_port_count": 0}],
        "meta": {"total": 1, "ports_included": True},
    }

    with mock.patch("pj.census.snapshot", return_value=snap) as snapshot_mock:
        cli.main(["census", "--limit", "1", "--include-ports"])

    snapshot_mock.assert_called_once_with(limit=1, include_ports=True)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is True
    assert parsed["meta"]["ports_included"] is True


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
