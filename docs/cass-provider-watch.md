# CASS Provider Watch

`pj` keeps CASS as a first-class `SessionStore` provider, even while the default backend is direct filesystem reads.

## Current Position

- `PJ_BACKEND=fs` is the default because it discovers new local sessions immediately and has no indexing step.
- `PJ_BACKEND=cass` remains supported through `pj/cass_facade.py`.
- Callers use the `SessionStore` protocol in `pj/session_store.py`, so switching providers should not change CLI command behavior or JSON shapes.

## Why Keep Watching CASS

CASS can become the better data provider when it exposes data that direct file scanning cannot reasonably reproduce:

- richer normalized provider metadata across more agents
- indexed full-text search with snippets and ranking
- multi-machine/source synchronization
- token, model, tool, and cost analytics computed at index time
- durable schema compatibility across agent log format changes

Direct scan should remain the freshness and no-dependency baseline. CASS should win when its indexed data is materially richer or faster for a workflow.

## Evaluation Checklist

Run these checks whenever CASS is updated:

1. `PJ_BACKEND=fs python3 -m pj list --detail --limit 1000`
2. `PJ_BACKEND=cass python3 -m pj list --detail --limit 1000`
3. Compare project count, agent coverage, latest-session freshness, model fields, token fields, and search snippets.
4. Run `scripts/smoke-test.sh` with and without `PJ_BACKEND=cass`.
5. If CASS is richer without losing freshness, keep `fs` as fallback and consider changing `get_store()` selection logic to prefer CASS when its index is fresh.

## Boundary

Only `pj/cass_facade.py` should know CASS table names, CLI flags, DB paths, or output formats. Any CASS shape change should be absorbed there and returned as the stable `SessionStore` dictionaries.
