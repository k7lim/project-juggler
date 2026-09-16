"""Private index tests use only generated histories and real HTTP queries."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from pj import history_index as index
from pj import history_service as service
from tests.test_history_service import codex_file, config_file, entry, request, running


def indexed_config(tmp_path, roots):
    directory = tmp_path / "catalog"
    directory.mkdir(mode=0o700)
    return config_file(tmp_path, roots, index_dir=str(directory))


def build(path):
    return index.build(service.load_config(str(path)))


def test_explicit_index_missing_build_update_delete_and_readonly_sources(tmp_path):
    root = tmp_path / "history"
    first = codex_file(root / "first.jsonl", "first", text="meteor handbook")
    second = codex_file(root / "second.jsonl", "second", text="other lesson")
    path = indexed_config(tmp_path, [entry(root)])
    original = {f: hashlib.sha256(f.read_bytes()).hexdigest() for f in (first, second)}
    with running(path) as server:
        assert request(server, "/api/chats")[0] == 503
    result = build(path)
    assert result["updated"] == 2 and result["sessions"] == 2
    assert build(path)["updated"] == 0
    assert all(hashlib.sha256(f.read_bytes()).hexdigest() == digest for f, digest in original.items())
    with running(path) as server:
        payload = request(server, "/api/search?q=eteor")[1]
        assert payload["meta"]["snapshot"] is True
        assert payload["meta"]["indexed_at"]
        assert payload["data"][0]["matching_sessions"][0]["original_session_id"] == "first"
        codex_file(first, "first", text="changed constellation")
        assert request(server, "/api/chat/first")[0] == 503
        assert request(server, "/api/search?q=meteor")[0] == 503
    assert build(path)["updated"] == 1
    with running(path) as server:
        assert request(server, "/api/search?q=meteor")[1]["data"] == []
        assert request(server, "/api/search?q=constellation")[1]["meta"]["total"] == 1
        second.unlink()
        assert request(server, "/api/chat/second")[0] == 503
    assert build(path)["removed"] == 1
    with running(path) as server:
        assert request(server, "/api/chats")[1]["meta"]["total"] == 1
        assert request(server, "/api/chat/second")[0] == 404


def test_index_literal_multi_term_match_and_pagination_parity(tmp_path):
    root = tmp_path / "history"
    codex_file(root / "first.jsonl", "first", text="Meteor orchard (a+)+$")
    codex_file(root / "second.jsonl", "second", "/approved/elsewhere", text="banana")
    direct = config_file(tmp_path, [entry(root)])
    indexed = indexed_config(tmp_path, [entry(root)])
    build(indexed)
    targets = ("/api/search?q=eteor", "/api/search?q=Meteor&q=synthetic&match=all",
               "/api/search?q=orchard&q=banana&match=all", "/api/search?q=or", "/api/search?q=approved",
               "/api/search?q=%28a%2B%29%2B%24", "/api/chats?limit=1&offset=1",
               "/api/chat/first?last=1&roles=assistant&offset=0&limit=1")
    # indexed_config rewrites the shared test config file, so preserve direct.
    raw = json.loads(indexed.read_text())
    raw.pop("index_dir")
    direct = indexed.parent / "direct.json"
    direct.write_text(json.dumps(raw))
    direct.chmod(0o600)
    with running(direct) as direct_server, running(indexed) as indexed_server:
        for target in targets:
            left = request(direct_server, target)
            right = request(indexed_server, target)
            assert left[0] == right[0] == 200, target
            assert left[1]["meta"]["total"] == right[1]["meta"]["total"], target
            if "/api/search" in target:
                assert {x["path"] for x in left[1]["data"]} == {x["path"] for x in right[1]["data"]}, target
            elif "/api/chat/" in target:
                assert left[1]["data"]["messages"] == right[1]["data"]["messages"], target
                assert left[1]["data"]["session_id"] == right[1]["data"]["original_session_id"]
            else:
                assert [r["session_id"] for r in left[1]["data"]] == [r["original_session_id"] for r in right[1]["data"]], target


def test_index_corpus_binding_and_public_rejection(tmp_path):
    first = codex_file(tmp_path / "first.jsonl")
    second = codex_file(tmp_path / "second.jsonl", "second")
    path = indexed_config(tmp_path, [entry(first)])
    build(path)
    data = json.loads(path.read_text())
    data["roots"] = [entry(second)]
    path.write_text(json.dumps(data))
    with running(path) as server:
        assert request(server, "/api/chats")[0] == 503
    with pytest.raises(index.IndexError):
        build(path)
    data["audience"] = "approved-public"
    data["snapshots"] = [entry(first, approved=True)]
    data.pop("roots")
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        service.load_config(str(path))


def test_claude_index_branch_tools_roles_and_legacy(tmp_path):
    root = tmp_path / "history"
    root.mkdir()
    tree = root / "tree.jsonl"
    events = []
    for uid, parent, role, content in (("root", None, "user", "tree lesson"),
                                      ("old", "root", "assistant", "abandoned secret"),
                                      ("new", "root", "assistant", [{"type": "text", "text": "active answer"}, {"type": "tool_use", "name": "Read"}])):
        events.append({"uuid": uid, "parentUuid": parent, "type": role, "sessionId": "tree", "cwd": "/approved/claude", "timestamp": "2026-01-01T00:00:00Z", "message": {"role": role, "content": content}})
    tree.write_text("\n".join(json.dumps(e) for e in events))
    path = indexed_config(tmp_path, [entry(root, "claude_code")])
    build(path)
    with running(path) as server:
        payload = request(server, "/api/chat/tree")[1]
        assert [m["content"] for m in payload["data"]["messages"]] == ["tree lesson", "active answer\n[Tool: Read]"]
        assert request(server, "/api/search?q=abandoned")[1]["data"] == []
        assert len(request(server, "/api/chat/tree?all_branches=true")[1]["data"]["messages"]) == 3
        payload = request(server, "/api/chat/tree?roles=assistant&include_tools=false&last=1")[1]
        assert payload["data"]["messages"][0]["content"] == "active answer"
        assert payload["meta"]["total_messages"] == 1


def test_failed_index_refresh_keeps_last_committed_generation(tmp_path, monkeypatch):
    root = tmp_path / "history"
    codex_file(root / "first.jsonl", "first")
    path = indexed_config(tmp_path, [entry(root)])
    first = build(path)
    codex_file(root / "second.jsonl", "second")
    def failed(*_args):
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(index, "_ingest", failed)
    with pytest.raises(RuntimeError):
        build(path)
    with running(path) as server:
        payload = request(server, "/api/chats")[1]
        assert payload["meta"]["total"] == 1
        assert payload["meta"]["indexed_at"] == first["indexed_at"]
        assert request(server, "/api/chat/second")[0] == 404


def test_index_larger_than_direct_corpus_limit_streaming(tmp_path):
    root = tmp_path / "history"
    path = codex_file(root / "large.jsonl", "large", text="oversize archive lesson")
    # Exercise >64MiB input without a huge resident transcript or slow FTS data:
    # large ignored system events still exceed the old all-body capture limit.
    event = json.dumps({"type": "response_item", "payload": {"role": "system", "content": "x" * (1024 * 1024)}}) + "\n"
    with path.open("a") as handle:
        for _ in range(65):
            handle.write(event)
    assert path.stat().st_size > service.MAX_CORPUS
    config = indexed_config(tmp_path, [entry(root)])
    assert build(config)["sessions"] == 1
    with running(config) as server:
        status, payload = request(server, "/api/search?q=archive")
        assert status == 200 and payload["meta"]["total"] == 1
        assert request(server, "/api/chat/large")[0] == 200
    catalog = Path(json.loads(config.read_text())["index_dir"]) / "history.sqlite3"
    assert catalog.stat().st_mode & 0o077 == 0
    connection = index._connect(catalog)
    try:
        assert connection.execute("PRAGMA temp_store").fetchone()[0] == 2
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM sessions")
    finally:
        connection.close()


def test_duplicate_native_ids_and_subagents_have_stable_drilldown_ids(tmp_path):
    root = tmp_path / "history"
    first = codex_file(root / "first.jsonl", "shared-id", text="first content")
    codex_file(root / "copy.jsonl", "shared-id", text="second content")
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    for name in ("parent", "agent-child"):
        (claude_root / (name + ".jsonl")).write_text(json.dumps({"type": "user", "sessionId": "claude-parent", "cwd": "/approved/claude", "message": {"role": "user", "content": name + " text"}}))
    path = indexed_config(tmp_path, [entry(root), entry(claude_root, "claude_code")])
    build(path)
    with running(path) as server:
        payload = request(server, "/api/chats")[1]
        ids = [row["session_id"] for row in payload["data"]]
        assert len(ids) == len(set(ids)) == 4
        assert all(value.startswith("hist-") for value in ids)
        assert request(server, "/api/chat/shared-id")[0] == 404
        assert request(server, "/api/chat/claude-parent")[0] == 404
        for sid in ids:
            assert request(server, "/api/chat/" + sid)[0] == 200
    build(path)
    with running(path) as server:
        assert {row["session_id"] for row in request(server, "/api/chats")[1]["data"]} == set(ids)


def test_index_bounds_large_chat_output_and_unicode_literal_search(tmp_path):
    root = tmp_path / "history"
    codex_file(root / "unicode.jsonl", "unicode", "/approved/ÄBC", text="Äther lesson")
    codex_file(root / "large.jsonl", "large", text="z" * (1024 * 1024 + 1))
    path = indexed_config(tmp_path, [entry(root)])
    build(path)
    with running(path) as server:
        assert request(server, "/api/chat/large")[0] == 413
        assert request(server, "/api/search?q=%C3%A4bc")[1]["meta"]["total"] == 1
        assert request(server, "/api/search?q=%C3%A4ther")[1]["meta"]["total"] == 1


def test_image_heavy_record_preserves_searchable_text_without_indexing_image(tmp_path, monkeypatch):
    history = codex_file(tmp_path / "history.jsonl", "image-heavy")
    with history.open("a") as handle:
        handle.write(json.dumps({"type": "response_item", "payload": {
            "role": "user", "content": [
                {"type": "input_text", "text": "visible constellation beside image"},
                {"type": "input_image", "image_url": "data:image/png;base64," + "a" * (23 * 1024 * 1024)},
            ]}}) + "\n")
    original = hashlib.sha256(history.read_bytes()).digest()
    config = indexed_config(tmp_path, [entry(history)])
    assert build(config)["sessions"] == 1
    with running(config) as server:
        assert request(server, "/api/search?q=constellation")[1]["meta"]["total"] == 1
        status, chat = request(server, "/api/chat/image-heavy")
        assert status == 200
        assert chat["data"]["messages"][-1]["content"] == "visible constellation beside image"
    assert hashlib.sha256(history.read_bytes()).digest() == original
    # Bounds remain enforced and a failed refresh leaves the committed snapshot.
    monkeypatch.setattr(index, "MAX_LINE", 1024)
    with history.open("a") as handle:
        handle.write("\n")
    with pytest.raises(index.IndexError):
        build(config)
