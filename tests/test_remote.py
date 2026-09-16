from __future__ import annotations

import json
import os
import socketserver
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import pytest

from pj import census_process, cli, remote
from tests.test_history_service import TOKEN, codex_file, config_file, entry, running


@pytest.fixture(autouse=True)
def _remote_environment(monkeypatch):
    monkeypatch.delenv("PJ_REMOTE_URL", raising=False)
    monkeypatch.delenv("PJ_REMOTE_SOCKET", raising=False)
    monkeypatch.delenv("PJ_REMOTE_TOKEN", raising=False)


def _ok(data, **meta):
    return {"success": True, "data": data, "meta": meta}


@pytest.fixture
def history_server():
    state = {"requests": [], "responses": {}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlsplit(self.path)
            state["requests"].append(
                {
                    "path": parsed.path,
                    "query": parse_qs(parsed.query),
                    "authorization": self.headers.get("Authorization"),
                }
            )
            status, payload, headers = state["responses"].get(
                parsed.path,
                (404, {"success": False, "data": [], "meta": {"error": "not found"}}, {}),
            )
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def unix_history_server():
    state = {"requests": [], "responses": {}}
    socket_directory = tempfile.TemporaryDirectory(prefix="pj-remote-", dir="/private/tmp")
    socket_path = Path(socket_directory.name) / "history.sock"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlsplit(self.path)
            state["requests"].append(
                {
                    "path": parsed.path,
                    "query": parse_qs(parsed.query),
                    "authorization": self.headers.get("Authorization"),
                }
            )
            status, payload = state["responses"].get(
                parsed.path,
                (404, {"success": False, "data": [], "meta": {"error": "not found"}}),
            )
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            pass

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    server = Server(str(socket_path), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, str(socket_path)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        socket_path.unlink(missing_ok=True)
        socket_directory.cleanup()


def _configure_http(monkeypatch, url, token="opaque-token"):
    monkeypatch.setenv("PJ_REMOTE_URL", url)
    monkeypatch.setenv("PJ_REMOTE_TOKEN", token)


def test_remote_search_uses_bearer_and_strict_query(history_server, monkeypatch):
    state, url = history_server
    state["responses"]["/api/search"] = (
        200,
        _ok([{"name": "alpha"}], source="history", audience="private"),
        {},
    )
    _configure_http(monkeypatch, url)

    result = remote.search(
        ["auth", "proxy"],
        project="/workspace/project",
        sort="relevance",
        match="all",
        limit=7,
    )

    assert result["data"] == [{"name": "alpha"}]
    assert state["requests"] == [
        {
            "path": "/api/search",
            "query": {
                "q": ["auth", "proxy"],
                "project": ["/workspace/project"],
                "sort": ["relevance"],
                "match": ["all"],
                "limit": ["7"],
            },
            "authorization": "Bearer opaque-token",
        }
    ]


def test_remote_health_over_unix_socket(unix_history_server, monkeypatch):
    state, socket_path = unix_history_server
    state["responses"]["/api/health"] = (
        200,
        _ok({"status": "running"}, source="history", audience="private"),
    )
    monkeypatch.setenv("PJ_REMOTE_SOCKET", socket_path)
    monkeypatch.setenv("PJ_REMOTE_TOKEN", "socket-token")

    result = remote.health()

    assert result["data"]["status"] == "running"
    assert state["requests"][0]["authorization"] == "Bearer socket-token"


def test_remote_transports_are_mutually_exclusive(monkeypatch):
    monkeypatch.setenv("PJ_REMOTE_URL", "")
    monkeypatch.setenv("PJ_REMOTE_SOCKET", "/tmp/history.sock")
    monkeypatch.setenv("PJ_REMOTE_TOKEN", "token")

    with pytest.raises(remote.RemoteError, match="exactly one"):
        remote.health()


def test_remote_rejects_redirect_without_following(history_server, monkeypatch):
    state, url = history_server
    state["responses"]["/api/health"] = (
        302,
        _ok({}),
        {"Location": f"{url}/redirect-target"},
    )
    state["responses"]["/redirect-target"] = (200, _ok({"leaked": True}), {})
    _configure_http(monkeypatch, url)

    with pytest.raises(remote.RemoteError, match="HTTP 302"):
        remote.health()

    assert [request["path"] for request in state["requests"]] == ["/api/health"]


def test_remote_requires_literal_loopback_and_safe_ascii_token(monkeypatch):
    monkeypatch.setenv("PJ_REMOTE_URL", "http://example.com:8765")
    monkeypatch.setenv("PJ_REMOTE_TOKEN", "token")
    with pytest.raises(remote.RemoteError, match="literal loopback"):
        remote.health()

    monkeypatch.setenv("PJ_REMOTE_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("PJ_REMOTE_TOKEN", "secret value")
    with pytest.raises(remote.RemoteError, match="PJ_REMOTE_TOKEN") as exc:
        remote.health()
    assert "secret value" not in str(exc.value)


def test_cli_remote_search_here_uses_canonical_cwd_without_discovery(
    history_server, monkeypatch, tmp_path, capsys
):
    state, url = history_server
    state["responses"]["/api/search"] = (
        200,
        _ok([], source="history", audience="private", total=0, limit=20),
        {},
    )
    _configure_http(monkeypatch, url)
    monkeypatch.chdir(tmp_path)

    with mock.patch.object(cli.discover, "resolve_project_for_cwd", side_effect=AssertionError), \
         mock.patch.object(cli.search_mod, "search", side_effect=AssertionError):
        cli.main(["search", "needle", "--here"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["meta"]["here"] is True
    assert state["requests"][0]["query"]["project"] == [os.path.realpath(tmp_path)]


def test_cli_configured_failure_never_falls_back_or_prints_token(monkeypatch, capsys):
    _configure_http(monkeypatch, "http://127.0.0.1:1", token="do-not-print-me")

    with mock.patch.object(cli.search_mod, "search", side_effect=AssertionError), \
         mock.patch.object(cli.discover, "discover", side_effect=AssertionError), \
         pytest.raises(SystemExit) as exc:
        cli.main(["search", "needle"])

    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert json.loads(output)["meta"]["source"] == "remote"
    assert "do-not-print-me" not in output


def test_cli_remote_chat_maps_filters_and_preserves_pretty(
    history_server, monkeypatch, capsys
):
    state, url = history_server
    state["responses"]["/api/chat/session-123"] = (
        200,
        _ok(
            {
                "session_id": "session-123",
                "title": "Remote chat",
                "agent": "codex",
                "messages": [{"role": "assistant", "content": "hello"}],
            },
            source="history",
            audience="private",
        ),
        {},
    )
    _configure_http(monkeypatch, url)

    with mock.patch.object(cli, "get_store", side_effect=AssertionError):
        cli.main(
            [
                "chat",
                "session-123",
                "--pretty",
                "--no-tools",
                "--roles",
                "user,assistant",
                "--last",
                "10",
                "--offset",
                "500",
            ]
        )

    assert "Remote chat" in capsys.readouterr().out
    query = state["requests"][0]["query"]
    assert query["include_tools"] == ["false"]
    assert query["roles"] == ["user,assistant"]
    assert query["last"] == ["10"]
    assert query["offset"] == ["500"]


def test_cli_remote_chat_list_alias_preserves_pretty(history_server, monkeypatch, capsys):
    state, url = history_server
    state["responses"]["/api/chats"] = (
        200,
        _ok(
            [{"session_id": "session-123", "agent": "codex", "title": "Alias chat"}],
            source="history",
            audience="private",
            project={"name": "remote-project", "path": "/host/remote-project"},
        ),
        {},
    )
    _configure_http(monkeypatch, url)

    with mock.patch.object(cli.discover, "discover", side_effect=AssertionError):
        cli.main(["chat", "list", "remote-project", "--pretty"])

    output = capsys.readouterr().out
    assert "remote-project" in output
    assert "Alias chat" in output


def test_cli_remote_chats_requires_explicit_project_or_here(history_server, monkeypatch, capsys):
    state, url = history_server
    _configure_http(monkeypatch, url)

    with pytest.raises(SystemExit) as exc:
        cli.main(["chats"])

    assert exc.value.code == 1
    assert "explicit project or --here" in json.loads(capsys.readouterr().out)["meta"]["error"]
    assert state["requests"] == []


def test_cli_remote_chats_rejects_project_with_here(history_server, monkeypatch, capsys):
    state, url = history_server
    _configure_http(monkeypatch, url)

    with pytest.raises(SystemExit) as exc:
        cli.main(["chats", "project", "--here"])

    assert exc.value.code == 1
    assert "either a project or --here" in json.loads(capsys.readouterr().out)["meta"]["error"]
    assert state["requests"] == []


def test_cli_remote_regex_and_unsupported_reads_fail_before_local_work(
    history_server, monkeypatch, capsys
):
    state, url = history_server
    _configure_http(monkeypatch, url)

    with mock.patch.object(cli.search_mod, "search", side_effect=AssertionError), \
         pytest.raises(SystemExit):
        cli.main(["search", "needle", "--regex"])
    assert state["requests"] == []
    assert json.loads(capsys.readouterr().out)["meta"]["source"] == "remote"

    with mock.patch.object(cli, "resolve_project_detail", side_effect=AssertionError), \
         pytest.raises(SystemExit):
        cli.main(["show", "project"])
    assert "not supported" in json.loads(capsys.readouterr().out)["meta"]["error"]


def test_local_mode_remains_local_and_health_is_cheap(monkeypatch, capsys):
    with mock.patch.object(cli.search_mod, "search", return_value=[]), \
         mock.patch.object(remote, "search", side_effect=AssertionError):
        cli.main(["search", "needle"])
    assert json.loads(capsys.readouterr().out)["success"] is True

    status = {"status": "stopped", "running": False}
    with mock.patch.object(census_process, "status", return_value=status), \
         mock.patch.object(cli.discover, "discover", side_effect=AssertionError):
        cli.main(["health"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"] == {"mode": "local", "census": status, "remote_url": None}


def test_cli_pretty_reads_end_to_end_against_history_service(monkeypatch, capsys):
    with tempfile.TemporaryDirectory(prefix="pj-e2e-", dir="/private/tmp") as directory:
        root = Path(directory)
        history = codex_file(root / "history" / "session.jsonl")
        socket_path = root / "config-private" / "history.sock"
        config = config_file(root, [entry(history)], unix_socket=str(socket_path))

        with running(config, unix=True):
            monkeypatch.setenv("PJ_REMOTE_SOCKET", str(socket_path))
            monkeypatch.setenv("PJ_REMOTE_TOKEN", TOKEN)
            cli.main(["search", "meteor", "--pretty"])
            cli.main(["chats", "/approved/project", "--pretty"])
            cli.main(["chat", "public-chat", "--pretty", "--no-tools"])

    output = capsys.readouterr().out
    assert "approved" in output
    assert "Chats: " in output
    assert "Session: public-chat" in output


def test_version_bumped_for_remote_history_release():
    from pj import __version__

    assert __version__ == "0.4.0"
    assert 'version = "0.4.0"' in Path("pyproject.toml").read_text(encoding="utf-8")
