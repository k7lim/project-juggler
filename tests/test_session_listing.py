"""Session listing regressions from the context-token audit (2026-11-13).

pj-6tr: a Codex rollout with no messages was listed by `pj chats`, dated by a
        later session_meta line timestamp instead of payload.timestamp.
pj-8hj: `pj chat` said "not found" for an id `pj chats` listed.
pj-tej: Codex token totals were always null in `pj chats`.
pj-s8u: recency must come from the last entry timestamp, never file mtime.

Every check runs against fs_store and against the index, which must agree.
"""
import os
from datetime import datetime, timezone

import pytest

from pj import fs_store
from pj.parsers import codex
from tests.test_fs_index import (  # noqa: F401  (fixtures)
    _write, claude_session, codex_session, corpus, fs_index, home, sessions_home)


def _iso(day, sec=0):
    return f"2026-09-{day:02d}T00:00:{sec:02d}Z"


def _ms(day, sec=0):
    return int(datetime(2026, 9, day, 0, 0, sec, tzinfo=timezone.utc).timestamp() * 1000)


def _rollout(home, session_id, day, events):
    path = sessions_home(home) / f".codex/sessions/2026/09/{day:02d}/rollout-{session_id}.jsonl"
    return str(_write(path, events))


def _meta(session_id, line_day, payload_day, cwd="/w/gamma"):
    return {"type": "session_meta", "timestamp": _iso(line_day),
            "payload": {"id": session_id, "cwd": cwd, "timestamp": _iso(payload_day)}}


def _msg(role, text, day, sec):
    kind = "input_text" if role == "user" else "output_text"
    return {"type": "response_item", "timestamp": _iso(day, sec),
            "payload": {"type": "message", "role": role,
                        "content": [{"type": kind, "text": text}]}}


def _event(kind, day, sec, **payload):
    return {"type": "event_msg", "timestamp": _iso(day, sec),
            "payload": {"type": kind, **payload}}


@pytest.fixture(params=["fs", "index"])
def store(request, corpus, fs_index):
    """fs_store, or the index after a refresh of whatever the test wrote."""
    if request.param == "fs":
        return fs_store

    class Refreshed:
        def __getattr__(self, name):
            fs_index.refresh()
            return getattr(fs_index, name)
    return Refreshed()


def _ids(store, workspace):
    return [s["session_id"] for s in store.project_sessions(workspace)]


# --- pj-6tr / pj-8hj: list exactly the sessions that can be opened ---

def test_metadata_only_rollout_is_not_listed(store, corpus):
    # The real case: session_meta + thread_settings_applied, nothing else.
    path = _rollout(corpus, "m1", 30, [
        _meta("m1", line_day=30, payload_day=8),
        _event("thread_settings_applied", 30, 1)])
    assert codex.parse_metadata(path) is None
    assert codex.parse_session(path) is None
    assert "m1" not in _ids(store, "/w/gamma")
    assert _ids(store, "/w/gamma") == ["x3", "x2"]


def test_long_rollout_without_messages_is_not_listed(store, corpus):
    events = [_meta("m2", 29, 29)] + [_event("token_count", 29, i % 60, info=None)
                                      for i in range(80)]
    path = _rollout(corpus, "m2", 29, events)
    assert codex.parse_metadata(path) is None
    assert "m2" not in _ids(store, "/w/gamma")


def test_rollout_whose_first_message_is_past_the_header_is_listed(store, corpus):
    events = [_meta("m3", 9, 9)] + [_event("token_count", 9, i % 60, info=None)
                                    for i in range(80)]
    events.append(_msg("user", "late first message", 9, 59))
    _rollout(corpus, "m3", 9, events)
    assert _ids(store, "/w/gamma")[0] == "m3"


def test_every_listed_session_can_be_opened(store, corpus):
    _rollout(corpus, "m4", 30, [_meta("m4", 30, 8), _event("thread_settings_applied", 30, 1)])
    for workspace in ("/w/alpha", "/w/beta", "/w/gamma"):
        for sid in _ids(store, workspace):
            assert store.get_session(sid) is not None, sid
    assert store.get_session("m4") is None


def test_started_at_prefers_session_meta_payload_timestamp(store, corpus):
    # A resumed rollout rewrites session_meta with a later line timestamp.
    path = _rollout(corpus, "m5", 12, [
        _meta("m5", line_day=12, payload_day=7),
        _msg("user", "hello", 12, 1), _msg("assistant", "hi", 12, 2)])
    for parse in (codex.parse_metadata, codex.parse_session):
        assert parse(path).started_at == _ms(7), parse
    [summary] = [s for s in store.project_sessions("/w/gamma") if s["session_id"] == "m5"]
    assert summary["started_at"].startswith("2026-09-07")
    assert summary["ended_at"].startswith("2026-09-12")


# --- pj-tej: Codex token totals ---

def _usage(base):
    return {"input_tokens": base, "cached_input_tokens": base // 2,
            "output_tokens": base // 10, "reasoning_output_tokens": base // 20,
            "total_tokens": base + base // 10}


def _count(day, sec, total, last):
    return _event("token_count", day, sec,
                  info={"total_token_usage": _usage(total), "last_token_usage": _usage(last)})


def test_codex_token_totals_come_from_the_last_token_count(store, corpus):
    # Codex logs a request's token_count AFTER the tool output that request
    # produced, so the session total is the last count with info, even when it
    # follows the last message.  A large trailing output must not hide it.
    path = _rollout(corpus, "t1", 13, [
        _meta("t1", 13, 13),
        _msg("user", "count my tokens", 13, 1),
        {"type": "response_item", "timestamp": _iso(13, 2),
         "payload": {"type": "function_call", "call_id": "c1", "name": "shell",
                     "arguments": "{}"}},
        {"type": "response_item", "timestamp": _iso(13, 3),
         "payload": {"type": "function_call_output", "call_id": "c1", "output": "ok"}},
        _count(13, 4, total=1000, last=1000),           # describes the request that issued c1
        _msg("assistant", "done", 13, 5),
        _count(13, 6, total=2400, last=1400),           # the session total
        _event("token_count", 13, 7, info=None),        # rate-limit only: ignored
        {"type": "response_item", "timestamp": _iso(13, 8),
         "payload": {"type": "function_call_output", "call_id": "c2",
                     "output": "x" * 300_000}},
    ])
    assert codex.parse_metadata(path).tokens == _usage(2400)
    [summary] = [s for s in store.project_sessions("/w/gamma") if s["session_id"] == "t1"]
    assert (summary["input_tokens"], summary["cache_read_tokens"],
            summary["output_tokens"], summary["total_tokens"]) == (2400, 1200, 240, 2640)


def test_sessions_without_token_counts_keep_null_totals(store, corpus):
    for summary in store.project_sessions("/w/gamma") + store.project_sessions("/w/alpha"):
        assert summary["input_tokens"] is None and summary["total_tokens"] is None


# --- pj-s8u: recency is the last entry timestamp, not the file mtime ---

def test_recency_ignores_a_newer_file_mtime(store, corpus):
    old = claude_session(corpus, "delta", "d-old", 10, [("user", "old entries")])
    claude_session(corpus, "delta", "d-new", 20, [("user", "new entries")])
    # An untimestamped write bumps the old file's mtime past everything.
    future = datetime(2026, 10, 31, tzinfo=timezone.utc).timestamp()
    os.utime(old, (future, future))
    sessions = store.project_sessions("/w/delta")
    assert [s["session_id"] for s in sessions] == ["d-new", "d-old"]
    assert sessions[1]["ended_at"].startswith("2026-09-10")
