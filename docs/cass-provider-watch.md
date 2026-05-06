# CASS Provider Watch

`pj` is filesystem-first today because direct local session parsing gives immediate freshness. A new Claude or Codex session can appear in `pj list`, `pj search`, and `pj show` without waiting for an indexer.

CASS should stay on the roadmap as a richer provider, not as a discarded experiment. It can still be valuable when it offers data that direct parsing should not own:

- broader agent coverage than the native `pj/parsers/` set
- stable cross-agent normalization for metadata, tool calls, branches, and subagents
- fast indexed search over very large archives
- durable provider contracts that can replace ad hoc file-format knowledge

The boundary is `pj/session_store.py`. Filesystem parsing lives in `pj/fs_store.py`; CASS integration lives in `pj/cass_facade.py`. Keep that provider line intact so a better CASS backend can be selected with `PJ_BACKEND=cass` without changing CLI commands or scheduler code.

Watch items:

- CASS freshness: does it discover new projects and new dotfile roots without long reindex cycles?
- CASS stability: does the SQLite/index layer avoid corruption or migration churn?
- Coverage: which agents have complete session, branch, and resume identifiers?
- Contract quality: can `pj` query project summaries, session lists, and content matches without leaking CASS-specific shapes?

Until those are true, direct filesystem scan remains the baseline provider.
