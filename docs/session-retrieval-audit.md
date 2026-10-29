# Session retrieval audit — 2026-10-29

The installed CLI points at this checkout. The host has a schema-v4 index with
7,256 files and only two dirty files at inspection, yet the default backend
was filesystem scanning. An indexed `pj search project-juggler --limit 10000`
returned 85 projects and 570 session references in 2,132 ms. The same query
with the new default took 2,372 ms. The raw filesystem run remained active
for several minutes. These are single-run observations, not percentile claims.

A second reproduced failure: discovery attempted to write its optional cache
under ~/.cache/pj and raised PermissionError, aborting search. Cache write
failure now leaves the successful read usable. A regression test failed before
the fix and passes afterward.

The default now uses the existing index backend. Missing, outdated, or corrupt
indexes retain its filesystem fallback; changed sessions retain its dirty-file
parsing. This does not automatically build an index. Fresh installations still
need `pj index refresh` for acceleration. Explicit PJ_BACKEND=fs remains available.

Historical evidence: session 01a07a26-85a4-7ba2-8308-9215c1cd1472 records a user
report of missing yolobox history, its source-discovery fix, and sandbox limits
on restarting census. Read with `pj chat 01a07a26 --no-tools --roles user,assistant`.
That failure was coverage, separate from search latency.

## Memory design assessment

Keep transcripts as evidence and the SQLite index as a disposable retrieval
accelerator. A matching transcript is not necessarily an accepted decision:
it may contain a proposal, superseded decision, injected instructions, or an
agent's unsupported claim. Durable memory should record the decision, scope,
source session/message, date, and what supersedes it. Project decisions belong
in versioned project documentation; task state belongs in beads.

The 570 references are candidates, not a certified inventory of actual usage.
Skill catalogs themselves mention project-juggler. A full usage audit must
classify actual tool invocations and distinguish permission failures, missing
sources, zero results, timeouts, and retrieval of irrelevant instruction text.
Keyword search alone cannot prove exhaustive coverage. No private transcript
contents are checked into this repository.
