# Plan: `fs_index` — Opt-in SQLite Session Index

Beads: `pj-8qn` (implementation), gate from `pj-nd2`.
Status: planned 2026-10-16. No index code yet.

## Context

`fs_store` parses session files on every search. At today's history size that
is too slow:

| Measure (host, 2026-10-16)        | Value                                   |
|-----------------------------------|-----------------------------------------|
| History on disk                   | 8.3 GB, 7,030 session files             |
| Searchable message text           | 197 MB, 250k messages, 6,544 sessions   |
| `fs_store` search, common term    | 21–109 s (`pj search Dicklesworthstone`: 89 s) |
| FTS5 trigram spike: build         | 91 s, 0.76 GB file                      |
| FTS5 trigram spike: search        | 2–806 ms, same session sets on 9 terms  |
| Stat every session file           | 0.29 s                                  |

The slowness hits the `pj` CLI, agents outside nono, and the pj query
service. The first two are short-lived processes, so an in-memory cache in the
service is not enough (it was measured and rejected: 398 MB data, 50–65 s cold
load per restart). CASS is not an option (see the handoff for pj-t1b / CASS
issues). The fix is a pj-owned on-disk index.

Baseline: the query service logs one `metric` JSON line per request and per
background scan (yolobox-pattern `174f9ad`, live 2026-10-16) in
`~/Library/Logs/yolobox-pj-query-server.log`. Collect a few days before
switching the service to the index. The first sample: `/search` 32.6 s =
23.9 s pj scan + 8.7 s redaction.

### Cheaper fixes already tried (complexity guide #2)

| Fix | Result | Why it is not enough |
|-----|--------|----------------------|
| Remove quadratic paths (`pj-t1b`) | Done, `be86122`, `d5abdb4` | Still parses every candidate session in full |
| `rg` prefilter (service only, `pj-f20`) | Live in the query service | 2026-10-16 log: 1 term, 551 candidates, 23.9 s scan; 7 terms, 8,002 candidates, 117 s scan, client got 504 at 90 s. Common terms match most files |
| Decode only lines containing the term | 30.8 s -> 28 s | Under 10% faster, and it changed result fields; reverted |
| In-memory cache in the service | 398 MB, 50-65 s cold load | Does not help the CLI or other short-lived processes |
| Lower `backend_limit` | Not tried | Fewer results is a behaviour change, not a speedup |

The cost is reading 8.3 GB of JSON to search 197 MB of text. Every fix
above still pays it. An index pays it once.

### Trade (complexity guide #4)

The index makes content and title search cheap (seconds to milliseconds).
In exchange we own:
- a 0.76 GB second copy of every session's text, as sensitive as the history;
- a second search path whose parity with `fs_store` must be kept;
- schema versions, a lock, and rebuilds.

## Decision

Add `pj/fs_index.py`, a `SessionStore` backend backed by one SQLite file with
an FTS5 trigram table over message text. It is selected with
`PJ_BACKEND=index` and is a drop-in for `fs_store`: the same results, the same
freshness, and `fs_store` results whenever the index cannot answer.

The interface is the one the gate already encodes (`tests/test_fs_index.py`):

- `pj.fs_index` — module implementing the whole `SessionStore` protocol
- `pj.fs_index.refresh()` — bring the index up to date with the session files
- `pj.paths.index_path()` — `data_dir() / "session_index.sqlite3"`
- `PJ_BACKEND=index` — `get_store()` returns `pj.fs_index`

### Protocol change (option a, chosen by the user 2026-10-16)

`search.py` reaches `sessions_by_project` and `load_session_file` through
`hasattr`, outside the protocol (principle #23). Make them protocol methods:

- `SessionStore.sessions_by_project(workspace_paths) -> dict[str, list[dict]]`
- `SessionStore.load_session_file(source_path, agent) -> dict | None`
- `cass_facade`: `sessions_by_project` loops `project_sessions`;
  `load_session_file` delegates to `fs_store`, as `get_session` already does.
- `search._scan_sessions` calls both directly; the `hasattr` branches go.
- `fs_index` implements both (phase 1: delegate to `fs_store`).

### Protocol change: narrowing the scan path (choice 1, chosen 2026-10-17)

Usage study (2026-10-17, 1,121 past searches; 679 outside pj dev and
benchmarks): 61% of real searches are plain. Almost all of the rest use
`--project` / `--here` and go through `search._scan_sessions`, and their share
grew from 20% in May to about 50% in Sep–Oct. `match=all` is 9%, regex 3%. So
the index must also narrow the scan path. Add one method:

```python
def candidate_sessions(terms: list[str], match: str,
                       workspace_paths: list[str]) -> set[str] | None:
    """Source paths of sessions in these workspaces that may match.

    A superset of the sessions _scan_sessions would match. None means
    "no narrowing": scan every session, as today."""
```

- `fs_store` and `cass_facade` return `None`, so their behaviour does not change.
- `search._scan_sessions` calls it when `regex` is false, and loads only the
  summaries whose `source_path` is in the set. It still parses those files
  in full and runs the same matching code, so results do not change.
- `fs_index` returns `None` if the index is missing, corrupt or over the dirty
  bound, or if any term gives no usable filter (see below).

The set must be a superset, or results get lost. `_scan_sessions` matches
against `" ".join([title, *message_texts])`, at the session level:

- **Per session, not per message.** A term is a candidate hit if the title or
  any message contains it. `match=all` is the intersection of the per-term
  session sets; `match=any` is the union.
- **Phrases can span messages** through the joining space (`"ends with"` +
  `"alpha"` contains `"with alpha"`). So a term with whitespace is filtered by
  each of its words separately (AND). Each word lies inside one message or the
  title, so this is a superset.
- **Unfilterable words.** A word shorter than 3 characters gives no trigram
  filter. For `match=all`, drop it from the intersection. For `match=any`,
  return `None`. Non-ASCII words use the exact scan described below.
- **Dirty files** (changed since the last write-back) are always in the set.
- **Titles** are matched against `files.title`.
- Update the protocol list in
  `test_index_implements_the_whole_session_store_protocol` and the header of
  `tests/test_fs_index.py`, which says "no new SessionStore methods".

## Design

### Schema

```sql
-- meta: schema_version, built_at
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE files(
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL, agent TEXT NOT NULL,
  size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, list_pos INTEGER NOT NULL,
  -- from parse_metadata (title search, sessions_by_project, ordering)
  session_id TEXT, workspace TEXT, title TEXT,
  started_at INTEGER, ended_at INTEGER, activity_ms INTEGER NOT NULL,
  model TEXT,  -- list_projects(detail=True), session summaries (pj-8qn.18)
  -- from parse_session (content search results)
  s_session_id TEXT, s_workspace TEXT, s_title TEXT,
  s_started_at INTEGER, s_ended_at INTEGER,
  -- this file's messages rowids (NULL when it has none); see below
  first_rowid INTEGER, last_rowid INTEGER);
CREATE VIRTUAL TABLE messages USING fts5(
  content, role UNINDEXED, file_id UNINDEXED, ord UNINDEXED,
  tokenize = 'trigram');
```

A `schema_version` other than the code's constant counts as a missing index.

Schema v2 (pj-8qn.18). `file_id` is UNINDEXED, so `WHERE file_id IN (...)`
scans all of `messages`. Each file's messages are inserted with consecutive
explicit rowids above the current maximum, and `files` records the range.
Only the write-lock holder inserts, in one transaction, so a range cannot
interleave. Deletes and the verify read use `rowid BETWEEN first_rowid AND
last_rowid`. Search still maps MATCH hits to files through `file_id`.

The two column groups are separate because `parse_metadata` (head and tail
lines) and `parse_session` (whole file) disagree on title, workspace and
timestamps; fs_store uses each for different searches. Found in ticket QA,
2026-10-17.

### Search: index narrows, Python decides

Parity comes by construction, not by matching SQLite's semantics to Python's:

1. FTS5 `MATCH` on the term gives candidate files (a superset).
2. For each candidate, in `fs_store`'s order (a stable sort by `activity_ms`
   over the `list_sessions` enumeration order; ties are common, so never
   break them by path), run `fs_store`'s own `_count_occurrences` /
   `_extract_snippet` over the stored message text, with the same
   `agent:session_id` de-duplication, and build the same result dicts.
3. Stop at `limit`, exactly where `fs_store` stops.

The trigram filter cannot be trusted in three cases. In all of them, skip
step 1 and scan the stored text with `fs_store._count_occurrences` registered
as a SQL function (`pj_count(content, ?) > 0`). That still avoids JSON
parsing. (`instr(lower(content), ?)` was the first idea, but SQLite's
`lower()` folds only ASCII: `'CAFÉ'` becomes `'cafÉ'`. pj-8qn.6, 2026-10-17.)

- terms shorter than 3 characters (`"go"`);
- terms with non-ASCII characters, where SQLite case folding may differ from
  `str.lower()` (`"café"`, `"İ"`);
- ASCII terms ending in `i`: `"İ".lower()` is `i` plus a combining dot, so
  `"abi"` matches `"ABİ"` in Python but not in the trigram index. İ is the
  only code point that does this.

`search_sessions` (titles) is served from `files`, with the same rules. Add a
parity test for it next to the content tests.

### Freshness: the index is never trusted for a changed file

On every search:

1. List the session files and stat each one (0.29 s today).
2. Compare (size, mtime_ns) with `files`. New or changed files and deleted
   files form the dirty set.
3. Parse the dirty files in memory and merge them into the results. Results
   from deleted files are dropped.
4. If the write lock is free, write the dirty set back in one transaction.
   This is only an optimization: correctness never depends on winning the lock.
   A file whose parse returned `None` during a search stays dirty and is never
   written back: inside the query service, `_guard_parse` returns `None` for
   files that are not rg candidates, and storing that would make the index
   trust an empty session. Any exception rolls the transaction back.

Appended sessions are re-indexed whole. That costs one file parse, the same as
`fs_store` pays for that file today.

**Bound (complexity guide #7).** A search parses at most `MAX_DIRTY_FILES`
(start at 200) or `MAX_DIRTY_BYTES` (start at 500 MB) of dirty files. Above
that, the index is too stale to help, so the search returns `fs_store` results
and warns "run `pj index refresh`". A burst of new history (October added
2.8 GB) then cannot make one search slower than today's search.

**Divergence check (complexity guide #5).** Size plus mtime misses a file
rewritten with the same size and mtime. That is rare, so it is not checked per
search. It is caught by `refresh(verify=True)`, which compares a sample of
indexed sessions (default 50) with a fresh parse, reports any mismatches in
`mismatched`, and re-indexes them. The worth-it
benchmark is the full version of that check.

### Concurrency model (principle #14)

- **Single writer.** `fcntl.flock(LOCK_EX | LOCK_NB)` on
  `index_path().with_suffix(".lock")`. `refresh()` waits for the lock; a search
  only tries it once and does not wait.
- **Readers never block.** WAL mode. A reader sees the last committed state
  and covers any newer changes through its own dirty set.
- **Full builds are atomic.** A first build or rebuild writes
  `session_index.sqlite3.tmp`, then `os.replace`s it. Open readers keep the old
  inode until they finish.
- **Accepted races.** (a) A file changes between stat and parse: the next
  search sees the new mtime and fixes it. (b) A session appended during a
  search is missed by that search, as with `fs_store` today. (c) Two processes
  parse the same dirty file: the work is wasted but the results are correct.

### Fallback

Any of these gives `fs_store` results for that call, plus one stderr warning
per process:

- the index file is missing (no implicit 91 s build inside a search);
- the schema version is wrong;
- `sqlite3.DatabaseError` (corrupt file);
- FTS5 is missing from the SQLite build.

`refresh()` deletes and rebuilds an index that is corrupt or has the wrong
schema version. Only an explicit `refresh()` builds an index (later:
`pj index refresh`, and the query service at warm-up).

### Privacy and placement

The index holds the text of every session in one file, as sensitive as the raw
history. The file, its `-wal`, `-shm` and `.lock` files are 0600 (create with
`umask 0o077`). `data_dir()` is created 0700.

The index must stay host-only. No yolobox-pattern config or doc mounts
`~/.local/share/pj` (checked 2026-10-16), but that is not proof. **Rollout
gate:** confirm that the installed nono profile cannot read
`~/.local/share/pj` before building the index on the host. Then make that
check automatic (complexity guide #8): add an assertion to yolobox-pattern's
`tests/test-pj-private-boundary.sh` that a sandbox read of the index path
fails.

## Rollout (principle #21)

1. Build behind `PJ_BACKEND=index`; the default stays `fs_store`.
2. Pass the gate: `tests/test_fs_index.py` with every `@pending` removed, then
   `PJ_BENCH_REAL=1 python3 -m pytest -q -s tests/test_fs_index_worth_it.py`
   on the host. Record the printed numbers in `pj-8qn`.
3. Use the CLI with `PJ_BACKEND=index` for a few days.
4. Set `PJ_BACKEND=index` in the query service's plist. Compare its `metric`
   lines with the baseline: `pj_ms` and scan `ms` for `cache_hit=false`.
5. Changing the default is a separate decision.

### Gate result (pj-8qn.13, host, 2026-10-17): FAIL

Worth-it benchmark (`search_content`, limit 100, newest; fs then index per
term, never in parallel). Results were identical for all 9 terms.

| term | fs | index |
|---|---|---|
| auth | 17.7 s | 1251 ms |
| middleware | 29.8 s | 273 ms |
| nono | 20.8 s | 1690 ms |
| launchd | 46.7 s | 1620 ms |
| rate limit | 23.4 s | 1797 ms |
| pytest | 45.3 s | 1636 ms |
| classifier | 28.3 s | 1632 ms |
| yolobox | 18.9 s | 2035 ms |
| zyzzyva-absent | 62.2 s | 2387 ms |

Build 90 s, index 0.74 GB, no-op refresh 0.55 s (budget 1 s: pass).
p95 2.39 s (budget 1 s: FAIL). Median speedup about 17x (needs 20x: FAIL).

Where index time goes (profiled, one search, live sessions dirty):
- With no dirty files, a search takes 0.1 s (an absent term's MATCH takes 2 ms).
- `_matching_messages` loads every matching message for a common term:
  0.8 s for "auth", although the loop stops at `limit`.
- Write-back takes 0.32 s, 0.25 s of it in `DELETE ... file_id IN`
  (`file_id` is UNINDEXED, so the delete is a full scan of `messages`).
- Parsing dirty files: 0.1 s and up. Live sessions grow during the 20-60 s
  fs run, so every index search in the benchmark had a dirty set.

Scoped CLI search (`pj search <t> --project project-juggler`, run one after
the other): fs 31.1/30.7/31.2 s against index 31.2/29.4/31.4 s for pytest,
nono and "rate limit". No gain. A profile shows about 30 s spent parsing every
file's metadata: `discover` -> `list_projects` (15.3 s) and
`sessions_by_project` (15.1 s). Both still delegate to fs_store, and this
plan never moved them onto the index. Narrowing saves only session loads,
which are a small part of the time.

## Whole path (complexity guide #11)

For the CLI, search is the whole wait, so the index is the whole win. For the
query service, a request waits on the pj scan and then on redaction. In the
first sample, redaction took 8.7 s of 32.6 s. With a 1 s index, that request
would take about 10 s, and redaction would become the slowest step. The parked
classifier-speed options (a shorter reply format, a dedicated model instance)
are next for the service. Its `redact_ms` metric shows when.

## Out of scope: regex and common words on the scan path

`candidate_sessions` does not help regex searches (3% of searches), which still
scan every session. It helps less for common words in large projects, because
the candidate set is large. Serving `load_session_file` from the index would fix
both, but the index would then have to store full sessions (tool calls,
branches) and keep every field identical to the files. That was rejected on
2026-10-17. Revisit it if the service's `metric` lines show scoped scans that
are still slow after rollout.

## Scaling threshold (principle #22)

- `fs_store`: has already broken. Latency grows with history size, and 8.3 GB
  gives 21–109 s.
- `fs_index`: the per-search stat check grows with file count. It is 0.29 s at
  7k files and reaches the 1 s no-op budget at about 24k files. The index is
  about 4x the message text (0.76 GB for 197 MB).
- **Revisit** at 20k session files or an index over 3 GB. Ways forward: a
  per-root directory mtime pre-check before the per-file stat, or pruning old
  sessions from the index.
- **Delete the index** if the worth-it gate fails: keep `fs_store`.

## Steps (TDD against the existing gate)

1. Protocol change (a): `session_store`, `cass_facade`, `search.py`, tests.
   Add `candidate_sessions` to the protocol in the same step (one commit per
   method, principle #18).
2. `paths.index_path()`; a `fs_index` skeleton that delegates everything to
   `fs_store`. The protocol, missing-index and env-selection tests pass.
3. `refresh()`: schema, full build, atomic replace, 0600, lock.
4. `search_content` and `search_sessions` through the index with the Python
   recheck: the parity and limit tests.
5. The dirty set: the new, appended and deleted tests, plus a test that a
   dirty set over the bound falls back to `fs_store`. Then `refresh(verify=True)`.
5b. `candidate_sessions`: first add `fs_store`'s `None` and the
   `_scan_sessions` call (no behaviour change, full suite green). Then the index
   version, gated by new parity tests. `search.search` with `project=`, with
   `match="all"`, and with a phrase that spans two messages must return the
   same results under `fs_store` and `fs_index`. Also test that a
   2-character word under `match=any` gives `None`.
6. Corrupt and wrong-schema fallback; idempotent refresh.
7. Remove every `@pending`; run the full suite.
8. Host benchmark (rollout step 2). The nono check (privacy gate).
9. README: document the scaling threshold of fs search and of the index.

## Iteration 2 (after the failed gate, approved 2026-10-17)

The first gate run showed where the time goes (see "Gate result" above). The
index already answers in 0.1 s when nothing is dirty. Iteration 2 removes the
three measured costs, fixes one correctness bug the run exposed, and adds the
scoped CLI search to the benchmark. Keep/delete is decided again on the
rerun. Revisit condition: if the rerun still fails, delete.

1. **Directory sessions go stale (bug).** kimi's `list_sessions` yields
   session directories. `os.stat(dir)` does not change when `wire.jsonl`
   inside grows, so the index trusts an outdated kimi session. The file
   signature (size, mtime_ns) becomes the aggregate of
   `fs_store._signature_paths(path)`: summed sizes, newest mtime. That is the
   same rule `fs_store.cache_signatures` already uses. It applies everywhere
   the index stats a session path: refresh, the search dirty check, verify
   and status.
2. **Leftover tmp files** (pj-8qn.16).
3. **Schema v2: delete by rowid, and store `model`.**
   `DELETE FROM messages WHERE file_id IN (...)` scans the whole FTS table
   (0.25 s per write-back). Each `files` row records the rowid range of its
   messages (`first_rowid`, `last_rowid`). Inserts are contiguous under the
   single writer. Deletes and the verify read then use
   `rowid BETWEEN ? AND ?`. `files` also gains `model` (from
   parse_metadata), which `list_projects(detail=True)` and the session
   summaries need. `SCHEMA_VERSION = "2"`, so an existing v1 index counts as
   outdated, and `refresh()` rebuilds it (pj-8qn.8).
4. **Load message text lazily.** `_matching_messages` currently reads every
   matching message's content up front (0.8 s for "auth"). It will first
   collect only the matching `file_id`s. A file's matching messages are read
   (rowid range plus the same MATCH or pj_count filter) only when
   `search_candidates` reaches that file. The loop stops at `limit`, so a
   common term reads only the files it returns.
5. **Serve metadata from the index.** `list_projects`,
   `recent_session_counts`, `sessions_by_project` and `project_sessions` are
   rebuilt from the stored metadata columns for clean files, plus
   `parse_metadata` for dirty ones. They stay in fs_store's
   `list_sessions` order, through fs_store's own `_group_by_workspace` /
   `_session_summary` / `_sort_summaries`, the same way search reuses
   `search_candidates`. Results must equal fs_store's. This removes the
   ~30 s each scoped CLI search spends parsing metadata (`discover` 15 s plus
   `sessions_by_project` 15 s). The dirty bound and fallback apply as in
   search.
6. **Benchmark the CLI path.** `test_fs_index_worth_it.py` gains a scoped
   case: `search.search(term, project=...)` under `set_store(fs_store)` and
   then `set_store(fs_index)`, never in parallel, with the same identical-
   results rule and the same p95 and speedup budgets. Then rerun on the host
   (pj-8qn.13).

All of these touch `pj/fs_index.py`, so they run one at a time in this order.
