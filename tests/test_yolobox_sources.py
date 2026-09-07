"""Exercise automatic sandbox discovery through search and census consumers."""
import json
from pathlib import Path

import pytest

from pj import census, cli, fs_store, session_store
from pj.parsers import claude_code, codex, kimi


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for parser in (claude_code, codex, kimi):
        monkeypatch.setattr(parser, "_DEFAULT_ROOT", str(tmp_path / "missing" / parser.agent_slug))
    for key in ("PJ_SOURCES", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PJ_DATA_DIR", str(tmp_path / "pj-data"))
    monkeypatch.setattr(session_store, "_store", fs_store)
    return tmp_path


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_yolobox_session_search_and_census(home, agent, capsys):
    sandbox = home / ".local/share/yolobox/home"
    workspace = str(home / "project")
    if agent == "claude":
        path = sandbox / ".claude/projects/project/session.jsonl"
        events = [{"type": "user", "sessionId": "sandbox-session", "cwd": workspace,
                   "timestamp": "2026-09-06T00:00:00Z",
                   "message": {"role": "user", "content": "hidden-yolobox-needle"}}]
    else:
        path = sandbox / ".codex/sessions/2026/09/06/rollout-session.jsonl"
        events = [
            {"type": "session_meta", "timestamp": "2026-09-06T00:00:00Z",
             "payload": {"id": "sandbox-session", "cwd": workspace}},
            {"type": "response_item", "timestamp": "2026-09-06T00:00:01Z",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "hidden-yolobox-needle"}]}},
        ]
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")

    assert len(fs_store.search_content("hidden-yolobox-needle")) == 1
    cli.main(["search", "hidden-yolobox-needle"])
    assert "sandbox-session" in capsys.readouterr().out
    snapshot = census.snapshot()
    assert [(row["path"], row["sessions"]) for row in snapshot["rows"]] == [(workspace, 1)]


def test_yolobox_roots_deduplicate_aliases(home, monkeypatch):
    root = home / ".local/share/yolobox/home/.claude/projects"
    root.mkdir(parents=True)
    alias = home / ".claude-alias"
    alias.symlink_to(root.parent, target_is_directory=True)
    monkeypatch.setenv("PJ_SOURCES", f"claude:{root}")
    assert fs_store._configured_roots() == [(str(root.resolve()), claude_code)]


def test_missing_yolobox_home_is_ignored(home):
    assert fs_store._configured_roots() == []


def test_new_yolobox_root_invalidates_cached_census(home):
    from pj import cache

    cache.save([])
    assert cache.load() == []
    (home / ".local/share/yolobox/home/.claude/projects").mkdir(parents=True)
    assert cache.load() is None
