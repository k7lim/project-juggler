"""HTTP boundary tests use synthetic histories only; never discover host data."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import stat
import threading
import time
from urllib.parse import urlencode

import pytest

from pj import history_service as service

TOKEN = "synthetic-test-token-" + "x" * 48


def codex_file(path, sid="public-chat", workspace="/approved/project", text="approved meteor lesson"):
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {"type": "session_meta", "timestamp": "2026-01-01T01:00:00Z", "payload": {"id": sid, "cwd": workspace}},
        {"type": "response_item", "timestamp": "2026-01-01T01:00:01Z", "payload": {"role": "user", "content": [{"type": "input_text", "text": text}]}},
        {"type": "response_item", "timestamp": "2026-01-01T01:00:02Z", "payload": {"role": "assistant", "content": [{"type": "output_text", "text": "a synthetic reply"}]}},
    ]
    path.write_text("\n".join(json.dumps(x) for x in events) + "\n")
    return path


def config_file(tmp_path, entries, audience="private", expired=False, **extra):
    directory = tmp_path / ("config-" + audience)
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    config = {"schema_version": 1, "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
              "expires_at": (datetime.now(timezone.utc) + timedelta(hours=-1 if expired else 1)).isoformat(),
              "audience": audience, **extra}
    config["roots" if audience == "private" else "snapshots"] = entries
    path = directory / "history.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    return path


def entry(path, agent="codex", approved=False):
    result = {"agent": agent, "path": str(path)}
    if approved:
        result["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


@contextmanager
def running(path, unix=False):
    config = service.load_config(str(path))
    server = service.UnixHistoryServer(config) if unix else service.HistoryServer(config)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def request(server, path="/api/health", token=TOKEN, method="GET", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", getattr(server, "server_port", 0), timeout=6)
    if isinstance(server, service.UnixHistoryServer):
        connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.sock.settimeout(6)
        connection.sock.connect(server.server_address)
    all_headers = dict(headers or {})
    if token is not None:
        all_headers["Authorization"] = "Bearer " + token
    connection.request(method, path, headers=all_headers)
    response = connection.getresponse()
    raw = response.read()
    result = response.status, json.loads(raw) if raw else None
    connection.close()
    return result


@pytest.fixture
def mixed(tmp_path):
    public = codex_file(tmp_path / "approved" / "public.jsonl")
    private = codex_file(tmp_path / "private" / "private.jsonl", "private-chat", "/secret/private-project", "CONFIDENTIAL vault passphrase")
    pub_config = config_file(tmp_path, [entry(public, approved=True)], "approved-public")
    private_config = config_file(tmp_path, [entry(public), entry(private)])
    return public, private, pub_config, private_config


def test_every_route_authenticates_without_metadata(mixed):
    with running(mixed[2]) as server:
        for path in ("/api/health", "/api/chats", "/api/chat/private-chat", "/api/search?q=CONFIDENTIAL", "/census", "/api/annotations"):
            missing = request(server, path, token=None)
            wrong = request(server, path, token="wrong")
            assert missing == wrong == (401, {"success": False, "data": [], "meta": {"error": "unauthorized"}})
        assert request(server, "/api/chats", token=None, method="POST")[0] == 401
        assert request(server, token=TOKEN)[0] == 200


def test_expired_credentials_and_default_private(tmp_path):
    history = codex_file(tmp_path / "history.jsonl")
    path = config_file(tmp_path, [entry(history)], expired=True)
    data = json.loads(path.read_text())
    data.pop("audience")
    path.write_text(json.dumps(data))
    assert service.load_config(str(path))["audience"] == "private"
    with running(path) as server:
        assert request(server)[0] == 401


def test_public_capability_filters_metadata_content_counts_and_guessed_ids(mixed):
    with running(mixed[2]) as server:
        for target in ("/api/chats", "/api/search?q=project", "/api/search?q=meteor", "/api/chat/public-chat"):
            status, payload = request(server, target)
            assert status == 200
            assert payload["meta"]["audience"] == "approved-public"
            serialized = json.dumps(payload)
            for forbidden in ("CONFIDENTIAL", "private-chat", "/secret", str(mixed[1]), "source_path"):
                assert forbidden not in serialized
        for selector in ("private-chat", "public", "unknown"):
            assert request(server, "/api/chat/" + selector) == (404, {"success": False, "data": [], "meta": {"error": "not found"}})
        status, payload = request(server, "/api/search?q=CONFIDENTIAL")
        assert status == 200 and payload["data"] == [] and payload["meta"]["total"] == 0
        status, payload = request(server, "/api/chats?project=private-project")
        assert status == 200 and payload["data"] == []
    with running(mixed[3]) as server:
        assert request(server, "/api/chats")[1]["meta"]["total"] == 2
        result = request(server, "/api/chat/private-chat")[1]
        assert result["data"]["workspace"] == "/secret/private-project"
        assert "CONFIDENTIAL" in result["data"]["messages"][0]["content"]
        assert result["data"]["source_path"] == str(mixed[1])


@pytest.mark.parametrize("query", ["audience=private", "roots=/secret", "path=/secret", "regex=true", "q=x&limit=0", "q=x&limit=101", "q=x&limit=1&limit=2", "q=x&offset=-1", "q=%00", "q=%252e%252e", "q=x&sort=bad", "q=x&match=bad"])
def test_search_rejects_extra_or_invalid_fields(mixed, query):
    with running(mixed[2]) as server:
        assert request(server, "/api/search?" + query)[0] == 400


def test_mutations_and_origin_are_rejected(mixed):
    before = mixed[0].read_bytes()
    with running(mixed[2]) as server:
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "CUSTOM"):
            assert request(server, "/api/annotations", method=method)[0] == 405
        assert request(server, headers={"Origin": "https://example.invalid"})[0] == 400
        assert request(server, headers={"Content-Length": "1"})[0] == 400
        assert request(server, "/api/chat/../../etc/passwd")[0] == 400
        assert request(server, "/api/chat/%2e%2e")[0] == 400
        assert request(server, "/api/search?q=" + "x" * 5000)[0] == 413
        assert request(server, headers={"X-Filler": "x" * 9000})[0] == 413
    assert mixed[0].read_bytes() == before


def test_approved_snapshot_appended_or_missing_fails_closed(mixed):
    with running(mixed[2]) as server:
        assert request(server, "/api/chats")[0] == 200
        with mixed[0].open("a") as handle:
            handle.write('{"type":"event_msg","payload":{"type":"user_message","message":"NEW SECRET"}}\n')
        for target in ("/api/chats", "/api/search?q=approved", "/api/chat/public-chat"):
            status, payload = request(server, target)
            assert status == 503
            assert "NEW SECRET" not in json.dumps(payload)
        mixed[0].unlink()
        assert request(server, "/api/chats")[0] == 503
        assert request(server, "/api/health")[0] == 200


def test_snapshot_does_not_enroll_new_files_and_private_reads_append(mixed):
    codex_file(mixed[0].parent / "unapproved.jsonl", "never-approved", text="unapproved secret")
    with running(mixed[2]) as server:
        assert request(server, "/api/chats")[1]["meta"]["total"] == 1
    with running(mixed[3]) as server:
        with mixed[1].open("a") as handle:
            handle.write('{"type":"event_msg","payload":{"type":"user_message","message":"new private message"}}\n')
        assert request(server, "/api/chat/private-chat")[1]["meta"]["total_messages"] == 3


def test_pagination_literal_matching_and_cli_shape(mixed):
    with running(mixed[3]) as server:
        status, result = request(server, "/api/chats?limit=1&offset=1")
        assert status == 200 and len(result["data"]) == 1 and result["meta"]["total"] == 2
        assert {"session_id", "agent", "title", "started_at", "ended_at", "model"} <= result["data"][0].keys()
        result = request(server, "/api/chats?project=private-project")[1]
        assert result["meta"]["project"]["name"] == "private-project"
        result = request(server, "/api/chat/public-chat?last=1&roles=assistant&limit=1")[1]
        assert result["meta"]["total_messages"] == 1 and result["data"]["messages"][0]["role"] == "assistant"
        result = request(server, "/api/search?q=meteor&q=synthetic&match=all")[1]
        assert len(result["data"]) == 1 and result["data"][0]["query_terms"] == ["meteor", "synthetic"]
        assert result["data"][0]["id"] == hashlib.sha256(b"/approved/project").hexdigest()[:8]
        assert request(server, "/api/search?q=" + urlencode({"": "(a+)+$"})[1:])[1]["data"] == []


def test_symlink_file_and_parent_are_rejected(tmp_path):
    history = codex_file(tmp_path / "real" / "history.jsonl")
    link = tmp_path / "alias"
    link.symlink_to(history.parent, target_is_directory=True)
    path = config_file(tmp_path, [entry(link / history.name, approved=True)], "approved-public")
    with running(path) as server:
        status, payload = request(server, "/api/chats")
        assert status == 503 and str(tmp_path) not in json.dumps(payload)
    history_link = tmp_path / "history.jsonl"
    history_link.symlink_to(history)
    path = config_file(tmp_path, [entry(history_link)])
    with running(path) as server:
        assert request(server, "/api/chats")[0] == 503


def test_config_requires_protected_owner_and_explicit_corpus(tmp_path):
    history = codex_file(tmp_path / "history.jsonl")
    path = config_file(tmp_path, [entry(history)])
    path.chmod(0o644)
    with pytest.raises(ValueError):
        service.load_config(str(path))
    path.chmod(0o600)
    data = json.loads(path.read_text())
    data["roots"] = []
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        service.load_config(str(path))


def test_explicit_empty_public_corpus_is_available_and_reveals_nothing(tmp_path):
    path = config_file(tmp_path, [], "approved-public")
    config = service.load_config(str(path))
    assert config["audience"] == "approved-public"
    assert config["entries"] == []

    with running(path) as server:
        status, health = request(server, "/api/health")
        assert status == 200
        assert health["meta"]["audience"] == "approved-public"

        for target in ("/api/search?q=unapproved", "/api/chats"):
            status, payload = request(server, target)
            assert status == 200
            assert payload["data"] == []
            assert payload["meta"]["total"] == 0
            assert payload["meta"]["audience"] == "approved-public"

        assert request(server, "/api/chat/guessed-private-id") == (
            404,
            {"success": False, "data": [], "meta": {"error": "not found"}},
        )

    data = json.loads(path.read_text())
    data.pop("snapshots")
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        service.load_config(str(path))


def test_output_file_limits_and_worker_timeout_cleanup(tmp_path, monkeypatch):
    history = codex_file(tmp_path / "history.jsonl", text="x" * (service.MAX_OUTPUT + 1))
    path = config_file(tmp_path, [entry(history)])
    with running(path) as server:
        assert request(server, "/api/chat/public-chat")[0] == 413
        history.write_bytes(b"x" * (service.MAX_FILE + 1))
        assert request(server, "/api/chats")[0] == 413
    codex_file(history)
    monkeypatch.setattr(service, "WORKER_TIMEOUT", 0.0001)
    with running(path) as server:
        assert request(server, "/api/chats")[0] == 503
        assert list(Path(server.config["temp_dir"]).iterdir()) == []


def test_unix_socket_transport(tmp_path):
    history = codex_file(tmp_path / "history.jsonl")
    path = config_file(tmp_path, [entry(history)])
    data = json.loads(path.read_text())
    # pytest's temporary path may exceed macOS's unix-socket limit.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="pj-sock-", dir="/private/tmp") as directory:
        socket_path = str(Path(directory) / "s")
        data["unix_socket"] = socket_path
        path.write_text(json.dumps(data))
        with running(path, unix=True) as server:
            assert stat.S_IMODE(os.stat(socket_path).st_mode) == 0o600
            assert request(server)[0] == 200
            assert request(server, "/api/chat/public-chat")[0] == 200
            assert request(server, token=None)[0] == 401
        assert not os.path.exists(socket_path)


def test_claude_branch_and_legacy_parser_preservation(tmp_path):
    tree = tmp_path / "claude" / "tree.jsonl"
    tree.parent.mkdir()
    events = []
    for uuid, parent, role, text in (("root", None, "user", "branch lesson"), ("old", "root", "assistant", "abandoned answer"), ("new", "root", "assistant", "active answer")):
        events.append({"uuid": uuid, "parentUuid": parent, "type": role, "sessionId": "tree-chat", "cwd": "/approved/claude", "message": {"role": role, "content": text}})
    tree.write_text("\n".join(json.dumps(e) for e in events))
    legacy = tree.parent / "legacy.jsonl"
    legacy.write_text(json.dumps({"type": "user", "sessionId": "legacy-chat", "cwd": "/approved/claude", "message": {"role": "user", "content": "legacy lesson"}}))
    path = config_file(tmp_path, [entry(tree.parent, "claude_code")])
    with running(path) as server:
        result = request(server, "/api/chat/tree-chat")[1]
        assert [m["content"] for m in result["data"]["messages"]] == ["branch lesson", "active answer"]
        result = request(server, "/api/chat/tree-chat?all_branches=true")[1]
        assert len(result["data"]["messages"]) == 3
        assert request(server, "/api/chat/legacy-chat")[1]["data"]["messages"][0]["content"] == "legacy lesson"


def test_auth_rechecked_between_requests_and_no_discovery_imports(mixed):
    with running(mixed[2]) as server:
        assert request(server)[0] == 200
        server.config["expires"] = time.time() - 1
        assert request(server)[0] == 401
    source = Path(service.__file__).read_text()
    assert "from . import discover" not in source
    assert "from . import census" not in source
    assert "detect_roots(" not in source


def test_duplicate_auth_and_disallowed_roles(mixed):
    with running(mixed[2]) as server:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.putrequest("GET", "/api/health")
        connection.putheader("Authorization", "Bearer " + TOKEN)
        connection.putheader("Authorization", "Bearer " + TOKEN)
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 401
        response.read()
        connection.close()
        for query in ("roles=root", "roles=", "include_tools=1", "all_branches=yes", "last=0", "last=1001"):
            assert request(server, "/api/chat/public-chat?" + query)[0] == 400


def test_request_deadline_includes_incomplete_headers(mixed, monkeypatch):
    monkeypatch.setattr(service, "REQUEST_TIMEOUT", 0.15)
    with running(mixed[2]) as server:
        connection = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
        connection.sendall(b"GET /api/health HTTP/1.1\r\nHost: localhost\r\n")
        started = time.monotonic()
        result = connection.recv(1024)
        assert time.monotonic() - started < 1
        assert result == b""  # Incomplete request never produces corpus data.
        connection.close()


def test_cyclic_claude_parser_is_killed_and_private_copy_cleaned(tmp_path):
    path = tmp_path / "cycle.jsonl"
    path.write_text(json.dumps({"uuid": "self", "parentUuid": "self", "type": "user", "sessionId": "cycle-chat", "message": {"role": "user", "content": "synthetic cycle"}}))
    config = config_file(tmp_path, [entry(path, "claude_code")])
    with running(config) as server:
        started = time.monotonic()
        assert request(server, "/api/chats")[0] == 503
        assert time.monotonic() - started < 5
        assert list(Path(server.config["temp_dir"]).iterdir()) == []
        assert request(server)[0] == 200


def test_corpus_bounds_fail_without_partial_results(tmp_path, monkeypatch):
    first = codex_file(tmp_path / "one.jsonl")
    second = codex_file(tmp_path / "two.jsonl", "two")
    path = config_file(tmp_path, [entry(first), entry(second)])
    config = service.load_config(str(path))
    monkeypatch.setattr(service, "MAX_CORPUS", len(first.read_bytes()))
    with pytest.raises(service.RequestError) as raised:
        service._capture(config)
    assert raised.value.status == 413
    monkeypatch.setattr(service, "MAX_FILES", 1)
    with pytest.raises(service.RequestError) as raised:
        service._capture(config)
    assert raised.value.status == 413


def test_empty_filtered_tree_does_not_resurrect_abandoned_messages(tmp_path):
    path = tmp_path / "tree.jsonl"
    events = []
    for uid, parent, role, text in (("root", None, "user", "initial"), ("old", "root", "assistant", "abandoned answer"), ("new", "root", "user", "new request")):
        events.append({"uuid": uid, "parentUuid": parent, "type": role, "sessionId": "tree-chat", "message": {"role": role, "content": text}})
    path.write_text("\n".join(json.dumps(e) for e in events))
    config = config_file(tmp_path, [entry(path, "claude_code")])
    with running(config) as server:
        status, payload = request(server, "/api/chat/tree-chat?roles=assistant")
        assert status == 200 and payload["data"]["messages"] == []
        assert payload["meta"]["total_messages"] == 0
        payload = request(server, "/api/chat/tree-chat?roles=assistant&all_branches=true")[1]
        assert payload["data"]["messages"][0]["content"] == "abandoned answer"
