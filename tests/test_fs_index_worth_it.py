"""Is the session index worth its complexity?  Measured on the real history (pj-nd2).

Opt-in and slow (several minutes): runs only with PJ_BENCH_REAL=1.

    PJ_BENCH_REAL=1 python3 -m pytest -q -s tests/test_fs_index_worth_it.py

Two paths are measured: fs_store.search_content against fs_index.search_content,
and the scoped CLI path (`pj search <term> --project <name>`, i.e.
search.search(term, project=...)) under each store.  PJ_BENCH_PROJECT picks the
project; it defaults to the one with the most sessions.

The index earns its place only if, against today's fs_store search:
  - results are identical for sessions that did not change during the run;
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

pytestmark = pytest.mark.skipif(
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


def _stable(hits, since):
    """Drop sessions whose activity is newer than `since` (they changed mid-run)."""
    return [h for h in hits if (h.get("ended_at") or h.get("started_at") or "") < since]


def _scoped_sessions(results, since):
    """(agent, session_id) of the matching sessions that did not change since `since`."""
    sessions = [s for project in results for s in project.get("matching_sessions", [])]
    return sorted((s.get("agent", ""), str(s.get("session_id", ""))) for s in _stable(sessions, since))


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


def test_index_is_worth_it_on_real_history(built_index):
    from pj import fs_index

    started, build_s, noop_s = built_index
    rows = []
    for term in TERMS:
        fs_hits, fs_s = _timed(fs_store.search_content, term, limit=100, sort="newest")
        ix_hits, ix_s = _timed(fs_index.search_content, term, limit=100, sort="newest")
        rows.append((term, fs_s, ix_s, _stable(fs_hits, started) == _stable(ix_hits, started)))

    print(f"\nbuild {build_s:.0f}s, no-op refresh {noop_s:.2f}s")
    for term, fs_s, ix_s, same in rows:
        print(f"  {term:16} fs {fs_s:6.1f}s  index {ix_s * 1000:7.0f}ms  same: {same}")

    assert all(same for *_, same in rows), "index results differ from fs_store"
    p95 = _p95(ix_s for _, _, ix_s, _ in rows)
    assert p95 <= INDEX_P95_BUDGET_S, f"index p95 {p95:.2f}s over budget"
    speedup = statistics.median(fs_s / max(ix_s, 1e-3) for _, fs_s, ix_s, _ in rows)
    assert speedup >= MIN_SPEEDUP, f"median speedup {speedup:.0f}x below {MIN_SPEEDUP:.0f}x"
    assert noop_s <= NOOP_REFRESH_BUDGET_S, f"freshness check {noop_s:.2f}s over budget"


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
            fs_sessions = _scoped_sessions(fs_res, started)
            rows.append((term, fs_s, ix_s, len(fs_sessions),
                         fs_sessions == _scoped_sessions(ix_res, started)))
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
