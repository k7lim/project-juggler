"""Claude conversation ancestry with interleaved parallel tool results."""
import json

import pytest

from pj.parsers import claude_code


def event(uuid, parent, role, content):
    return {
        "uuid": uuid,
        "parentUuid": parent,
        "type": role,
        "sessionId": "parallel-tools",
        "cwd": "/w/alpha",
        "timestamp": "2026-10-21T00:00:00Z",
        "message": {"role": role, "content": content},
    }


def write_session(tmp_path, events):
    path = tmp_path / "parallel-tools.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in events))
    return str(path)


def test_latest_continuation_survives_interleaved_tool_result(tmp_path):
    path = write_session(tmp_path, [
        event("question", None, "user", "Continue the work"),
        event("call-a", "question", "assistant", [
            {"type": "tool_use", "id": "a", "name": "Read", "input": {}},
        ]),
        event("call-b", "call-a", "assistant", [
            {"type": "tool_use", "id": "b", "name": "Read", "input": {}},
        ]),
        event("result-a", "call-a", "user", [
            {"type": "tool_result", "tool_use_id": "a", "content": "first result"},
        ]),
        event("result-b", "call-b", "user", [
            {"type": "tool_result", "tool_use_id": "b", "content": "second result"},
        ]),
        event("answer", "result-b", "assistant", "Here is the completed work"),
    ])

    session = claude_code.parse_session_tree(path, include_tools=False)

    assert [m.content for m in session.messages] == [
        "Continue the work", "Here is the completed work",
    ]


def test_abandoned_fork_stays_excluded_and_annotated(tmp_path):
    path = write_session(tmp_path, [
        event("question", None, "user", "Question"),
        event("old-answer", "question", "assistant", "Discarded answer"),
        event("old-followup", "old-answer", "user", "Discarded followup"),
        event("new-answer", "question", "assistant", "Replacement answer"),
    ])
    active = claude_code.parse_session_tree(path)
    assert [m.uuid for m in active.messages] == ["question", "new-answer"]
    full = claude_code.parse_session_tree(path, all_branches=True)
    assert [(m.uuid, m.branch) for m in full.messages] == [
        ("question", "active"), ("old-answer", "abandoned"),
        ("old-followup", "abandoned"), ("new-answer", "active"),
    ]


def test_late_auxiliary_record_does_not_select_an_old_branch(tmp_path):
    path = write_session(tmp_path, [
        event("question", None, "user", "Question"),
        event("old-answer", "question", "assistant", "Old answer"),
        event("new-answer", "question", "assistant", "New answer"),
        event("late-attachment", "old-answer", "attachment", ""),
    ])
    session = claude_code.parse_session_tree(path)
    assert [m.uuid for m in session.messages] == ["question", "new-answer"]


@pytest.mark.parametrize("parent", [None, "unavailable-parent"])
def test_newest_conversation_can_start_a_disconnected_root(tmp_path, parent):
    path = write_session(tmp_path, [
        event("old-root", None, "user", "Earlier conversation"),
        event("new-root", parent, "user", "Resumed conversation"),
        event("attachment", "new-root", "attachment", ""),
        event("answer", "attachment", "assistant", "Latest answer"),
    ])
    session = claude_code.parse_session_tree(path)
    assert [m.uuid for m in session.messages] == ["new-root", "answer"]


def test_roles_and_tools_do_not_change_the_active_ancestry(tmp_path):
    path = write_session(tmp_path, [
        event("question", None, "user", "Question"),
        event("old-answer", "question", "assistant", "Discarded answer"),
        event("new-question", "question", "user", "Edited question"),
        event("call", "new-question", "assistant", [
            {"type": "tool_use", "id": "a", "name": "Read", "input": {}},
        ]),
        event("result", "call", "user", [
            {"type": "tool_result", "tool_use_id": "a", "content": "result"},
        ]),
    ])
    session = claude_code.parse_session_tree(path, include_tools=False, roles={"user"})
    assert [m.uuid for m in session.messages] == ["question", "new-question"]
    assert claude_code.parse_session_tree(
        path, include_tools=False, roles={"assistant"},
    ) is None
    with_tools = claude_code.parse_session_tree(path, roles={"assistant"})
    assert [m.uuid for m in with_tools.messages] == ["call"]


def test_cycles_in_damaged_parent_links_terminate(tmp_path):
    path = write_session(tmp_path, [
        event("question", "answer", "user", "Question"),
        event("answer", "question", "assistant", "Answer"),
    ])
    session = claude_code.parse_session_tree(path)
    assert [m.uuid for m in session.messages] == ["question", "answer"]
