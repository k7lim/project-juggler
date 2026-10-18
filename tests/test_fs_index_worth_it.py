"""Is the session index worth its complexity?  Measured on the real history (pj-nd2).

The two benchmarks are opt-in and slow (several minutes): they run only with
PJ_BENCH_REAL=1.  The parity helper's unit test always runs.

    PJ_BENCH_REAL=1 python3 -m pytest -q -s tests/test_fs_index_worth_it.py

Two paths are measured: fs_store.search_content against fs_index.search_content,
and the scoped CLI path (`pj search <term> --project <name>`, i.e.
search.search(term, project=...)) under each store.  PJ_BENCH_PROJECT picks the
project; it defaults to the one with the most sessions.

The index earns its place only if, against today's fs_store search:
  - results are identical for sessions that did not change during the run,
    up to the limit boundary (see _same_up_to_tail);
  - p95 search latency is at most INDEX_P95_BUDGET_S;
  - the median speedup is at least MIN_SPEEDUP;
  - an up-to-date refresh (the per-search freshness check) costs at most
    NOOP_REFRESH_BUDGET_S.
If any of these fail, keep fs_store and delete the index.
Spike baseline 2026-10-16 (6,544 sessions, 8.3 GB): fs 21-109 s, index
2-806 ms, freshness check 0.29 s.
"""
import os
import statistics
import time

import pytest

from pj import cache, fs_store, search, session_store

real_history = pytest.mark.skipif(
    os.environ.get("PJ_BENCH_REAL") != "1", reason="real-history benchmark; set PJ_BENCH_REAL=1")

TERMS = ["auth", "middleware", "nono", "launchd", "rate limit", "pytest",
         "classifier", "yolobox", "zyzzyva-absent"]
INDEX_P95_BUDGET_S = 1.0
MIN_SPEEDUP = 20.0
NOOP_REFRESH_BUDGET_S = 1.0
SCOPED_TERMS = ["pytest", "nono", "rate limit"]


def _timed(fn, *args, **kwargs):
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, time.perf_counter() - start


def _activity(hit):
    return hit.get("ended_at") or hit.get("started_at") or ""


def _stable(hits, since):
    """Drop sessions whose activity is newer than `since` (they changed mid-run)."""
    return [h for h in hits if _activity(h) < since]


def _session_key(hit):
    return (hit.get("agent", ""), str(hit.get("session_id", "")))


def _unstable_count(a, b, since):
    """Distinct sessions that _stable drops from either result list."""
    return len({_session_key(h) for h in [*a, *b] if _activity(h) >= since})


def _same_up_to_tail(a, b, unstable):
    """Stable lists `a` and `b` (in result order) agree, tolerating the limit boundary.

    fs and index run seconds apart.  A session that starts matching in between
    can push the oldest entry out of one side's top `limit`; _stable drops the
    newcomer but cannot restore what it displaced.  So up to `unstable` entries
    may be missing from the tail of either list: compare the first
    max(len) - unstable entries, which both lists must have.
    """
    n = max(len(a), len(b)) - unstable
    return len(a) >= n and len(b) >= n and a[:n] == b[:n]


def _same_content_hits(fs_hits, ix_hits, since):
    return _same_up_to_tail(_stable(fs_hits, since), _stable(ix_hits, since),
                            _unstable_count(fs_hits, ix_hits, since))


def _scoped_sessions(results):
    return [s for project in results for s in project.get("matching_sessions", [])]


def _scoped_stable_keys(sessions, since):
    """(agent, session_id) of the stable sessions, newest start first.

    The scoped scan walks sessions by started_at, newest first, and stops at
    `limit`, so a displaced session is the oldest start: it sits at the tail.
    """
    stable = sorted(_stable(sessions, since),
                    key=lambda s: (s.get("started_at") or "", _session_key(s)), reverse=True)
    return [_session_key(s) for s in stable]


def _same_scoped(fs_res, ix_res, since):
    fs_sessions, ix_sessions = _scoped_sessions(fs_res), _scoped_sessions(ix_res)
    return _same_up_to_tail(_scoped_stable_keys(fs_sessions, since),
                            _scoped_stable_keys(ix_sessions, since),
                            _unstable_count(fs_sessions, ix_sessions, since))


def test_parity_tolerates_only_the_limit_boundary():
    since = "2026-10-18T00:00:00"

    def hit(sid, at):
        return {"agent": "claude_code", "session_id": sid, "ended_at": at}

    old = [hit(f"s{i}", f"2026-10-17T0{9 - i}:00:00") for i in range(4)]
    newcomer = hit("new", "2026-10-18T00:00:30")

    assert _same_content_hits(old, list(old), since)
    # The index ran later: a newcomer pushed the oldest session out of its top 4.
    assert _same_content_hits(old, [newcomer, *old[:3]], since)
    assert not _same_content_hits(old, [newcomer, old[0], old[2], old[3]], since)
    assert not _same_content_hits(old, old[:3], since)

    def project(sessions):
        return [{"matching_sessions": [{**s, "started_at": s["ended_at"]} for s in sessions]}]

    assert _same_scoped(project(old), project(old[::-1]), since)
    assert _same_scoped(project(old), project([newcomer, *old[:3]]), since)
    assert not _same_scoped(project(old), project([newcomer, old[0], old[2], old[3]]), since)


def _p95(times):
    times = sorted(times)
    return times[min(len(times) - 1, int(0.95 * len(times)))]


def _bench_project():
    name = os.environ.get("PJ_BENCH_PROJECT")
    if name:
        return name
    biggest = max(fs_store.list_projects(), key=lambda p: p["session_count"])
    return biggest["path"].rstrip("/").rsplit("/", 1)[-1]


@pytest.fixture(scope="module")
def built_index():
    """Build the index once for both tests: (run start, build seconds, no-op refresh seconds)."""
    from pj import fs_index

    started = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    _, build_s = _timed(fs_index.refresh)
    _, noop_s = _timed(fs_index.refresh)
    return started, build_s, noop_s


@real_history
def test_index_is_worth_it_on_real_history(built_index):
    from pj import fs_index

    started, build_s, noop_s = built_index
    rows = []
    for term in TERMS:
        fs_hits, fs_s = _timed(fs_store.search_content, term, limit=100, sort="newest")
        ix_hits, ix_s = _timed(fs_index.search_content, term, limit=100, sort="newest")
        rows.append((term, fs_s, ix_s, _same_content_hits(fs_hits, ix_hits, started)))

    print(f"\nbuild {build_s:.0f}s, no-op refresh {noop_s:.2f}s")
    for term, fs_s, ix_s, same in rows:
        print(f"  {term:16} fs {fs_s:6.1f}s  index {ix_s * 1000:7.0f}ms  same: {same}")

    assert all(same for *_, same in rows), "index results differ from fs_store"
    p95 = _p95(ix_s for _, _, ix_s, _ in rows)
    assert p95 <= INDEX_P95_BUDGET_S, f"index p95 {p95:.2f}s over budget"
    speedup = statistics.median(fs_s / max(ix_s, 1e-3) for _, fs_s, ix_s, _ in rows)
    assert speedup >= MIN_SPEEDUP, f"median speedup {speedup:.0f}x below {MIN_SPEEDUP:.0f}x"
    assert noop_s <= NOOP_REFRESH_BUDGET_S, f"freshness check {noop_s:.2f}s over budget"


@real_history
def test_scoped_cli_search_is_worth_it_on_real_history(built_index, monkeypatch):
    """`pj search <term> --project <name>` under fs_store, then under fs_index.

    The discover cache is disabled for every timed call.  Both stores report the
    same cache_signatures, so with the cache on, the fs run would fill it and the
    index run would skip discover's list_projects, the step this path is about.
    On real history the live sessions change those signatures between searches
    anyway, so a cold discover is what the CLI usually pays.
    """
    from pj import fs_index

    started, _, _ = built_index
    project = _bench_project()
    monkeypatch.setattr(cache, "load", lambda: None)
    monkeypatch.setattr(cache, "save", lambda projects: None)

    original = session_store.get_store()
    rows = []
    try:
        for term in SCOPED_TERMS:
            session_store.set_store(fs_store)
            fs_res, fs_s = _timed(search.search, term, project=project)
            session_store.set_store(fs_index)
            ix_res, ix_s = _timed(search.search, term, project=project)
            n = len(_stable(_scoped_sessions(fs_res), started))
            rows.append((term, fs_s, ix_s, n, _same_scoped(fs_res, ix_res, started)))
    finally:
        session_store.set_store(original)

    print(f"\nscoped search, --project {project}")
    for term, fs_s, ix_s, n, same in rows:
        print(f"  {term:16} fs {fs_s:6.1f}s  index {ix_s * 1000:7.0f}ms  sessions {n:4}  same: {same}")

    assert all(same for *_, same in rows), "scoped index results differ from fs_store"
    p95 = _p95(ix_s for _, _, ix_s, _, _ in rows)
    assert p95 <= INDEX_P95_BUDGET_S, f"scoped index p95 {p95:.2f}s over budget"
    speedup = statistics.median(fs_s / max(ix_s, 1e-3) for _, fs_s, ix_s, _, _ in rows)
    assert speedup >= MIN_SPEEDUP, f"scoped median speedup {speedup:.0f}x below {MIN_SPEEDUP:.0f}x"
