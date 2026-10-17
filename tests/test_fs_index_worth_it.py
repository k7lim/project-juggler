"""Is the session index worth its complexity?  Measured on the real history (pj-nd2).

Opt-in and slow (several minutes): runs only with PJ_BENCH_REAL=1.

    PJ_BENCH_REAL=1 python3 -m pytest -q -s tests/test_fs_index_worth_it.py

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

from pj import fs_store

pytestmark = pytest.mark.skipif(
    os.environ.get("PJ_BENCH_REAL") != "1", reason="real-history benchmark; set PJ_BENCH_REAL=1")

TERMS = ["auth", "middleware", "nono", "launchd", "rate limit", "pytest",
         "classifier", "yolobox", "zyzzyva-absent"]
INDEX_P95_BUDGET_S = 1.0
MIN_SPEEDUP = 20.0
NOOP_REFRESH_BUDGET_S = 1.0


def _timed(fn, *args, **kwargs):
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, time.perf_counter() - start


def _stable(hits, since):
    """Drop sessions whose activity is newer than `since` (they changed mid-run)."""
    return [h for h in hits if (h.get("ended_at") or h.get("started_at") or "") < since]


def test_index_is_worth_it_on_real_history():
    from pj import fs_index

    started = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    _, build_s = _timed(fs_index.refresh)
    _, noop_s = _timed(fs_index.refresh)

    rows = []
    for term in TERMS:
        fs_hits, fs_s = _timed(fs_store.search_content, term, limit=100, sort="newest")
        ix_hits, ix_s = _timed(fs_index.search_content, term, limit=100, sort="newest")
        rows.append((term, fs_s, ix_s, _stable(fs_hits, started) == _stable(ix_hits, started)))

    print(f"\nbuild {build_s:.0f}s, no-op refresh {noop_s:.2f}s")
    for term, fs_s, ix_s, same in rows:
        print(f"  {term:16} fs {fs_s:6.1f}s  index {ix_s * 1000:7.0f}ms  same: {same}")

    assert all(same for *_, same in rows), "index results differ from fs_store"
    ix_times = sorted(ix_s for _, _, ix_s, _ in rows)
    p95 = ix_times[min(len(ix_times) - 1, int(0.95 * len(ix_times)))]
    assert p95 <= INDEX_P95_BUDGET_S, f"index p95 {p95:.2f}s over budget"
    speedup = statistics.median(fs_s / max(ix_s, 1e-3) for _, fs_s, ix_s, _ in rows)
    assert speedup >= MIN_SPEEDUP, f"median speedup {speedup:.0f}x below {MIN_SPEEDUP:.0f}x"
    assert noop_s <= NOOP_REFRESH_BUDGET_S, f"freshness check {noop_s:.2f}s over budget"
