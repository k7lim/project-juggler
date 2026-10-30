"""Pi native and protected-launcher history participates in both search backends."""
import json
from dataclasses import replace

import pytest

from pj import cli, fs_index, fs_store, resume, session_store
from pj.parsers import pi
from tests.test_fs_index import home  # noqa: F401


def write_session(root, sid="pi-session"):
    path = root / "--w-project--" / f"2026-10-01_{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        {"type": "session", "version": 3, "id": sid, "cwd": "/w/project",
         "timestamp": "2026-10-01T00:00:00Z"},
        {"type": "model_change", "modelId": "test-model"},
        {"type": "message", "id": "u1", "parentId": None,
         "timestamp": "2026-10-01T00:00:01Z",
         "message": {"role": "user", "content": "First Pi prompt\nsecond line"}},
        {"type": "message", "id": "a1", "parentId": "u1",
         "timestamp": "2026-10-01T00:00:02Z",
         "message": {"role": "assistant", "model": "answer-model", "content": [
             {"type": "thinking", "thinking": "private-thought"},
             {"type": "text", "text": "Pi conversation needle café"},
             {"type": "toolCall", "name": "bash", "arguments": {"command": "ls"}},
         ]}},
        {"type": "message", "id": "t1", "parentId": "a1",
         "message": {"role": "toolResult", "timestamp": 1790812803000,
                     "content": [{"type": "text", "text": "tool-output-needle"},
                                 {"type": "image", "data": "binary-image-data"}]}},
        {"type": "compaction", "summary": "state-only-summary"},
        {"type": "message", "id": "u2", "parentId": "u1",
         "message": {"role": "user", "content": [{"type": "text", "text": "branch-needle"}]}},
        {"type": "session_info", "name": "Named Pi session",
         "timestamp": "2026-10-01T00:00:04Z"},
    ]
    path.write_text("\n".join(json.dumps(e) for e in entries) + '\nnull\n[]\n{"partial":')
    return path


def test_parse_history_and_metadata(tmp_path):
    path = write_session(tmp_path)
    session = pi.parse_session(str(path))
    assert session.session_id == "pi-session"
    assert session.workspace == "/w/project"
    assert session.title == "Named Pi session"
    assert session.model == "answer-model"
    assert session.ended_at - session.started_at == 4000
    assert [m.role for m in session.messages] == ["user", "assistant", "tool", "user"]
    assert session.messages[1].content == "Pi conversation needle café\n[Tool: bash]"
    assert session.messages[2].content == "tool-output-needle"
    assert session.messages[2].created_at == 1790812803000
    assert session.messages[-1].parent_uuid == "u1"
    assert pi.parse_metadata(str(path)) == replace(session, messages=[])


def test_invalid_and_header_only(tmp_path):
    path = tmp_path / "session.jsonl"
    assert pi.parse_session(str(path)) is None
    for data in ('{}', '{"type":"session"}', 'null\n{"type":"message"}'):
        path.write_text(data)
        assert pi.parse_session(str(path)) is None
    path.write_text('{"type":"session","id":"empty","cwd":"/w"}')
    assert pi.parse_session(str(path)).messages == []


@pytest.mark.parametrize("backend", [fs_store, fs_index])
def test_host_and_sandbox_search(home, monkeypatch, capsys, backend):
    for key in ("PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR"):
        monkeypatch.delenv(key, raising=False)
    host = home / ".pi/agent/sessions"
    sandbox = home / ".local/share/yolobox/home/.pi/agent/sessions"
    write_session(host, "native")
    write_session(sandbox, "sandbox")
    monkeypatch.setattr(session_store, "_store", backend)
    if backend is fs_index:
        backend.refresh()
    for query in ("conversation needle", "tool-output-needle", "branch-needle", "café"):
        hits = backend.search_content(query)
        assert {h["session_id"] for h in hits} == {"native", "sandbox"}
        assert {h["agent"] for h in hits} == {"pi"}
    assert len(backend.search_sessions("Named Pi")) == 2
    assert not backend.search_content("private-thought")
    assert not backend.search_content("binary-image-data")
    assert backend.get_session("native")["messages"]
    cli.main(["search", "conversation needle"])
    output = capsys.readouterr().out
    assert '"native"' in output and '"sandbox"' in output
    # Newly created Pi files are picked up without rebuilding an existing index.
    write_session(host, "later")
    assert len(backend.search_content("conversation needle")) == 3


def test_custom_roots_and_alias_deduplication(home, monkeypatch):
    config = home / "custom-pi"
    root = config / "sessions"
    write_session(root)
    direct = home / "direct-sessions"
    write_session(direct, "direct")
    alias = home / "alias"
    alias.symlink_to(root, target_is_directory=True)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config))
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(direct))
    monkeypatch.setenv("PJ_SOURCES", f"pi-raw:{alias}:pi:{root}")
    roots = fs_store._configured_roots()
    assert roots == [(str(root), pi), (str(direct), pi)]
    assert len(fs_store.search_content("conversation needle")) == 2


def test_pi_resume_uses_session_flag():
    assert resume.resume_command("pi", "abc") == "pi --session abc"
    assert resume.resume_command("pi-raw", "abc") == "pi-raw --session abc"
