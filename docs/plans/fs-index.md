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
  size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
  session_id TEXT, workspace TEXT, title TEXT,
  started_at INTEGER, ended_at INTEGER, activity_ms INTEGER NOT NULL);
CREATE VIRTUAL TABLE messages USING fts5(
  content, role UNINDEXED, file_id UNINDEXED, ord UNINDEXED,
  tokenize = 'trigram');
```

A `schema_version` other than the code's constant counts as a missing index.

### Search: index narrows, Python decides

Parity comes by construction, not by matching SQLite's semantics to Python's:

1. FTS5 `MATCH` on the term gives candidate files (a superset).
2. For each candidate, in `fs_store`'s order (`activity_ms`, then path), run
   `fs_store`'s own `_count_occurrences` / `_extract_snippet` over the stored
   message text, and build the same result dicts.
3. Stop at `limit`, exactly where `fs_store` stops.

The trigram filter cannot be trusted in two cases. In both, skip step 1 and
scan the stored text with `instr(lower(content), ?)`. That still avoids JSON
parsing:

- terms shorter than 3 characters (`"go"`);
- terms with non-ASCII characters, where SQLite case folding may differ from
  `str.lower()` (`"café"`, `"İ"`).

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

Appended sessions are re-indexed whole. That costs one file parse, the same as
`fs_store` pays for that file today.

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
`~/.local/share/pj` before building the index on the host.

## Rollout (principle #21)

1. Build behind `PJ_BACKEND=index`; the default stays `fs_store`.
2. Pass the gate: `tests/test_fs_index.py` with every `@pending` removed, then
   `PJ_BENCH_REAL=1 python3 -m pytest -q -s tests/test_fs_index_worth_it.py`
   on the host. Record the printed numbers in `pj-8qn`.
3. Use the CLI with `PJ_BACKEND=index` for a few days.
4. Set `PJ_BACKEND=index` in the query service's plist. Compare its `metric`
   lines with the baseline: `pj_ms` and scan `ms` for `cache_hit=false`.
5. Changing the default is a separate decision.

## Out of scope for phase 1: the scan path

`search.search` goes through `_scan_sessions`, not `search_content`, when the
search is project-scoped, regex, or multi-term `match=all`. Phase 1 does not
speed that path up: `fs_index.load_session_file` delegates to `fs_store`.
Possible phase 2: serve `load_session_file` from stored messages, or narrow
`_scan_sessions` with the index. Decide from the baseline. The service logs
`project` and `match` per request, so the share of scoped searches can be
counted.

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
2. `paths.index_path()`; a `fs_index` skeleton that delegates everything to
   `fs_store`. The protocol, missing-index and env-selection tests pass.
3. `refresh()`: schema, full build, atomic replace, 0600, lock.
4. `search_content` and `search_sessions` through the index with the Python
   recheck: the parity and limit tests.
5. The dirty set: the new, appended and deleted tests.
6. Corrupt and wrong-schema fallback; idempotent refresh.
7. Remove every `@pending`; run the full suite.
8. Host benchmark (rollout step 2). The nono check (privacy gate).
9. README: document the scaling threshold of fs search and of the index.
