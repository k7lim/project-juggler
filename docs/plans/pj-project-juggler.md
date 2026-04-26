# Plan: `pj` — Project Juggler

## Context

Kevin juggles ~30 active projects across multiple AI coding agents (Claude Code, Codex, OpenCode, Amp, Hermes, Kimi Code, etc.). He can't keep all sessions open. His current workflow — forked claude-run with text search → copy-paste resume commands — works but doesn't help him **decide what to work on next**.

CASS (Dicklesworthstone/coding_agent_session_search) already solves the hard problem: multi-agent session discovery, indexing, and search across 19 providers. `pj` doesn't reimplement that. Instead, `pj` facades over CASS's CLI output and adds the missing layer: **scheduling, prioritization, and context-switching decisions**.

```
pj (scheduler)
  └── facades over:
      ├── cass CLI (cass search/list --robot → JSON)  ← multi-agent session data
      ├── agent dotfiles directly (stat only)          ← lightweight timestamp signals
      └── pj's own annotations.jsonl                   ← priority, notes, tags, blockers
```

CASS is the sensor. `pj` is the scheduler. Per engineering_core.md #4 (Facade Externals): wrap CASS, never leak its shapes. One file changes if CASS is replaced.

## Design: SKILL.md Summary

### Sensors (read-only)

| Command | Purpose |
|---|---|
| `pj list [--state active\|stale\|dormant] [--sort last-active\|priority] [--limit N]` | All projects with derived state |
| `pj status <project>` | Deep view: sessions, memory, notes, resume command |
| `pj next [--limit N]` | Scheduling heuristic — what to work on next, with scores and reasons |
| `pj search <query>` | Substring search across names, summaries, prompts, notes |
| `pj resume <project>` | Output `cd DIR && <agent> --resume ID` for most recent session (agent-aware: claude, codex, etc.) |

### Actuators (append-only writes to `~/.local/share/pj/annotations.jsonl`)

| Command | Purpose |
|---|---|
| `pj note <project> "text"` | Free-text note (next steps, blockers) |
| `pj prioritize <project> high\|medium\|low\|none` | Set priority |
| `pj archive <project>` | Exclude from list/next |
| `pj tag <project> <tag>` | Grouping/filtering |

### Output

JSON envelope everywhere. `--pretty` for humans. `--config-dir` or `CLAUDE_CONFIG_DIR` for dual-dotfile support.

```json
{"success": true, "data": [...], "meta": {"total": 178, "offset": 0, "limit": 20, "source": "~/.claude", "latency_ms": 340}}
```

### State Machine

- **active**: last activity ≤7 days
- **stale**: 7–30 days (the "don't let it slip" zone)
- **dormant**: 30+ days
- **archived**: user explicit
- **blocked**: latest note starts with "blocked:"

### Scheduling Heuristic (v1)

| Factor | Weight | Signal |
|---|---|---|
| User priority | 0.35 | high=1.0, medium=0.6, low=0.2, none=0.4 |
| Recency | 0.25 | `1.0 / (1 + days * 0.3)` |
| Momentum | 0.20 | Sessions in last 7d / max across projects |
| Staleness boost | 0.10 | 3–7 day old projects get 0.8 (nudge before they rot) |
| Has next-step note | 0.10 | Latest note is actionable = 1.0 |

## Data Sources

| Source | What pj extracts | Cost |
|---|---|---|
| `cass list --robot --fields minimal` | project path, agent type, session count, last active, session IDs | Subprocess call, ~200ms |
| `cass search <query> --robot --limit N` | session matches with content context | Subprocess call, ~300ms |
| Agent dotfiles (stat only) | file mtimes for recency signals | ~10ms filesystem walk |
| `~/.local/share/pj/annotations.jsonl` | priority, notes, archive, tags, blockers | pj's own state |

Cache: `~/.cache/pj/project_index.json` validated by CASS index mtime + annotations mtime. Rebuilds when either changes.

### Why facade over CASS instead of reading dotfiles directly

- CASS already handles 19 agents' storage formats, path encoding, subagent linking
- CASS has an incremental index — no re-scanning thousands of JSONL files per invocation
- CASS's `--robot` JSON output is a stable contract we can depend on
- If CASS adds agent #20, pj gets it for free
- If we replace CASS later, only `pj/cass_facade.py` changes

## Module Structure

```
pj/
  __main__.py       # python -m pj
  cli.py            # argparse, --pretty, dispatch
  envelope.py       # {"success", "data", "meta"} builder
  cass_facade.py    # subprocess calls to cass CLI, parse --robot JSON, normalize to pj's types
  discover.py       # merge CASS data + annotations into unified project list
  state.py          # derive active/stale/dormant/blocked/archived
  schedule.py       # scoring heuristic for "next"
  search.py         # delegates to cass search, overlays pj annotations
  resume.py         # build cd + <agent> --resume command (agent-aware)
  annotate.py       # append-only JSONL writer (notes, priority, archive, tag)
  cache.py          # project_index.json with CASS index mtime validation
  pretty.py         # --pretty table rendering
```

Python 3.11+, zero dependencies (stdlib only). ~800-1200 LOC estimated.

## Implementation Phases

1. **Foundation** — `envelope.py`, `discover.py`, `state.py`, `cache.py`, `cli.py` with `list` command
2. **Status + Resume** — `resume.py`, `status` and `resume` subcommands
3. **Actuators** — `annotate.py`, `note`/`prioritize`/`archive`/`tag` subcommands
4. **Search + Next** — `search.py`, `schedule.py`, `search` and `next` subcommands
5. **Polish** — `--pretty` rendering, fuzzy project resolution, `--tag` filter

## Verification

1. `pj list` returns JSON envelope with projects from all detected agents, sorted by last-active
2. `pj list --pretty` renders readable table with agent column (claude, codex, etc.)
3. `pj status jam-sesh` shows sessions across agents, resume command with correct agent binary
4. `pj resume jam-sesh` outputs valid `cd ... && <agent> --resume ...` (agent-appropriate)
5. `pj note jam-sesh "test note"` appends to annotations.jsonl
6. `pj next` returns scored projects with reasons
7. Without CASS installed: `pj list` gracefully degrades to annotations-only
8. Agent test: hand SKILL.md to Claude, see if it can call `pj next`, parse JSON, reason about priorities

## Key Decisions

- **CASS is the session layer** — pj never parses JSONL directly. CASS handles 19 agents' formats.
- **Agent-agnostic resume** — `resume.py` maps agent type → resume command (`claude --resume`, `codex --resume`, etc.). New agents = add a line to the mapping, not a new module.
- **Append-only annotations** — per engineering_core.md #9. Current state = replay projection.
- **project_id = SHA-256(path)[:8]** — stable, deterministic, short for CLI use.
- **CASS as external dep** — installed separately (`cargo install cass` or binary). pj checks for it on startup, gives clear error if missing.
- **Graceful degradation** — if CASS is unavailable, pj can still show annotations-only view (user notes, priorities). Just no session data.
