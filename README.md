# pj - Project Juggler

A CLI that knows what you've been working on across AI coding agents. It scans your session files, tracks your annotations, and tells you what to pick up next.

**Zero dependencies.** Python 3.9+, stdlib only. No database required.

## Why

If you use Claude Code, Codex, or other AI coding agents, you accumulate dozens of session files across projects. `pj` reads those files and gives you:

- A single view of every project you've touched
- Full-text search across all your conversations
- One-liner resume commands to jump back into any session
- A scoring heuristic that recommends what to work on next

## Install

```bash
git clone https://github.com/k7lim/project-juggler.git
cd project-juggler

# Run directly
python3 -m pj list --pretty

# Or install an editable command
python3 -m pip install -e .
pj list --pretty
```

No runtime packages are required. Editable install only creates the `pj` command.

## Quick start

```bash
# See all your projects
pj list --pretty

# What should I work on?
pj next --pretty

# Deep dive into a project
pj show myproject --pretty

# Search across all sessions
pj search "authentication" --pretty

# Resume a specific session from search results
# (copy the command from search output)
cd /path/to/project && claude --resume abc123
```

## Commands

### `pj list` — See your projects

```bash
pj list --pretty
```

```
ID        STATE     NAME                          AGENTS            SESS  PRI     LAST ACTIVE
---------------------------------------------------------------------------------------------
a1b2c3d4  active    my-feature                    claude_code         23  high    2h ago
e5f6g7h8  stale     refactor-auth                 claude_code         15  medium  5d ago
i9j0k1l2  dormant   old-dashboard                 claude_code          8  none    45d ago
```

Filter and sort:

```bash
pj list --state active              # Only active projects
pj list --tag backend               # Only tagged "backend"
pj list --sort priority             # Sort by priority instead of recency
pj list --detail --pretty           # Include hours worked, models used
```

### `pj search` — Find any conversation

Searches project names, paths, notes, tags, session titles, and message content.

```bash
pj search "groatnola" --pretty
```

```
Search: "groatnola" — 1 result(s)

NAME                          STATE     MATCH
----------------------------------------------------------------------
cooking                       stale     content
    Dr Greger's "How not to Die" Groatnola recipe  (11d ago, 5ee76e61-d22)
      cd /Users/kevin/sandbox/cooking && claude --resume 5ee76e61-d22f-4f92-...
```

Each matching session shows a ready-to-paste resume command.

### `pj next` — What to work on

Scores projects by priority, recency, momentum, staleness, and whether you left a note with a clear next step.

```bash
pj next --pretty
```

```
#    SCORE  NAME                          STATE     PRI     REASON
-------------------------------------------------------------------
1    0.89   my-feature                    active    high    high priority; recent momentum
2    0.71   refactor-auth                 stale     medium  needs attention soon
3    0.45   old-dashboard                 dormant   none    baseline score
```

### `pj show` - Deep dive into a project

```bash
pj show myproject --pretty
```

Shows project metadata, recent sessions with models and durations, and a resume command for the latest session.

`pj status` remains as a hidden compatibility alias.

### `pj resume` — Jump back in

```bash
eval $(pj resume myproject)
```

Outputs `cd <path> && <agent> --resume <session_id>` for the most recent session.

### Annotations

Organize your projects without modifying the underlying repos:

```bash
pj prioritize myproject high        # Set priority (high/medium/low/none)
pj note myproject "finish the API"  # Add a note (shows in listings)
pj tag myproject backend            # Add a tag (filterable)
pj archive myproject                # Hide from recommendations
```

Notes starting with `blocked:` mark the project as blocked and exclude it from `pj next`.

## How It Works

`pj` reads session data through a small `SessionStore` provider boundary.

**Filesystem store** (default): reads local session files directly. No background process, no database, no indexing step.
- `~/.claude/projects/` — Claude Code
- `~/.claude-*/projects/` — alternate Claude installs
- `~/.codex/sessions/` — Codex
- `~/.kimi/sessions/` — Kimi CLI

**CASS store** (watched provider): if you have a CASS database, set `PJ_BACKEND=cass` to read its normalized index. CASS stays behind `pj/cass_facade.py`; if it becomes richer than direct file scans for project metadata, snippets, token analytics, or multi-agent coverage, `pj` can prefer it without changing CLI output.

See `docs/cass-provider-watch.md` for the evaluation checklist.

### Project states

States are derived automatically from session activity:

| State | Meaning |
|-------|---------|
| **active** | Session in the last 7 days |
| **stale** | 7-30 days since last session |
| **dormant** | 30+ days since last session |
| **blocked** | Latest note starts with "blocked:" |
| **archived** | Manually archived via `pj archive` |

### Output format

Every command outputs JSON by default (for piping to `jq` or agents). Add `--pretty` for colored, human-readable tables.

## Supported agents

Session parsing is implemented for Claude Code and Codex. Resume commands are templated for 13 agents:

| Agent | Resume command |
|-------|---------------|
| Claude Code | `claude --resume <id>` |
| Codex | `codex --resume <id>` |
| AMP | `amp --resume <id>` |
| Aider | `aider --resume <id>` |
| Cursor | `cursor --resume <id>` |
| Cline | `cline --resume <id>` |
| Roo Code | `roo-code --resume <id>` |
| Gemini CLI | `gemini --resume <id>` |
| Copilot | `copilot --resume <id>` |
| Hermes | `hermes --resume <id>` |
| Kimi Code | `kimi-code --resume <id>` |
| OpenCode | `opencode --resume <id>` |

To add a new agent: create a parser in `pj/parsers/` and add a resume template in `pj/resume.py`.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `PJ_BACKEND` | `fs` | `cass` to use CASS SQLite backend |
| `PJ_SOURCES` | auto-detected | Extra session roots. Format: `agent:path:agent:path` |
| `PJ_DATA_DIR` | `~/.local/share/pj` | Where annotations and cache live |
| `PJ_CASS_DBS` | auto-detected | Colon-separated CASS database paths |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude Code config directory |
| `CODEX_HOME` | `~/.codex` | Codex config directory |

## Using with agents

`pj` outputs JSON by default, making it easy for AI agents to consume. An agent can:

```bash
# Find relevant projects
pj search "auth middleware"

# Check what's recommended
pj next --limit 3

# Get a resume command
pj resume myproject

# Get full project context
pj show myproject --sessions 5
```

The `--pretty` flag is for humans. Omit it when piping to agents or scripts.
