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

# Run the census dashboard in the background
pj census start
pj census status
pj census stop

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
pj search --here sport --pretty
pj search football soccer --project epic-odds --pretty
pj search sport --sort relevance --pretty
pj search soccer --sort oldest --pretty
pj search "foot(ball)?|soccer" --regex --project epic-odds
```

Search accepts multiple terms. By default, multiple terms mean "any term";
use `--match all` to require every term. `--project` restricts the search to a
project name, path, or ID prefix; `--here` infers that project from your current
working directory. `--sort` can be `newest`, `relevance`, or `oldest`. For
exploratory searches, prefer separate terms over quoted phrases:
`pj search sports broadcast fan excitement` is broader than
`pj search "sports broadcast fan excitement"`, which looks for that exact
substring. Use `--regex` for alternatives, stems, and spelling variants.

```
Search: "groatnola" — 1 result(s)

NAME                          STATE     MATCH
----------------------------------------------------------------------
cooking                       stale     content
    Dr Greger's "How not to Die" Groatnola recipe  (11d ago, 5ee76e61-d22)
      cd /Users/kevin/sandbox/cooking && claude --resume 5ee76e61-d22f-4f92-...
```

Each matching session shows a ready-to-paste resume command.

For the proposed sandbox-agent workflow, where disposable Docker/yolobox agents
use the same `pj` CLI while reading from a host-side `pj` service, see
[`docs/plans/pj-sandbox-agent-access.md`](docs/plans/pj-sandbox-agent-access.md).

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

### `pj census` - Project dashboard

Generate the project census as JSON, run the live dashboard in the foreground,
or manage it as a background process:

```bash
pj census                    # JSON census snapshot
pj census --include-ports    # JSON census snapshot with local runtime ports
pj census serve              # Foreground web server
pj census start              # Background web server
pj census status             # JSON status: pid, URL, health, state/log files
pj census stop               # Graceful shutdown via local control endpoint
```

`pj census start/status/stop` are designed for both humans and agents: output is
structured JSON, the server metadata is stored under `PJ_DATA_DIR`, and `stop`
uses a per-process local control token instead of parsing logs or guessing ports.

### `pj ports` - Local runtime port discovery

`pj ports` reports local development servers and other runtime ports that can be
associated with discovered projects. It is a CLI-first contract; any web API or
dashboard view must reuse the same discovery behavior and response fields rather
than adding browser-only behavior.

Live port links are an optional runtime sensor, not part of core project
discovery. Project discovery answers "what projects exist and where are they?";
port discovery answers "what local listeners appear to be related right now?"
using transient process metadata. `pj ports` only inspects local listening TCP
ports and process details such as PID, command, and working directory when the
platform exposes them. It does not start, stop, restart, connect to, or mutate
processes or sockets.

This keeps the boundary explicit:

| Boundary | pj behavior |
|----------|-------------|
| Sensor, not actuator | Report observed local runtime endpoints; never manage the runtime lifecycle. |
| CLI first | `pj ports` is the source of truth; dashboards and APIs surface the same records. |
| Optional enrichment | `pj census --include-ports` adds runtime hints without changing default census discovery. |
| Best effort | Missing tools, permissions, or process metadata lower confidence or produce warnings instead of redefining projects. |
| Local only | Discovery is limited to the current machine's listening ports and local process metadata. |

Because port records can expose local URLs, process commands, and working
directories, callers should treat the output as local developer telemetry. pj
does not publish these records remotely by default, and UI surfaces should avoid
sharing them outside the local operator's context without an explicit user
action.

```bash
pj ports                         # JSON array of local runtime port records
pj ports --project myproject      # Filter records to one project query
pj census --include-ports         # Attach matching port records to census rows
```

All JSON output uses the standard envelope:

```json
{
  "success": true,
  "data": [],
  "meta": {}
}
```

`pj ports` returns one record per discovered local runtime endpoint in `data`.
Readers must ignore unknown fields so the contract can grow without breaking
older clients.

Port record fields:

| Field | Meaning |
|-------|---------|
| `project_id` | Matched project identifier when a project can be associated with the port; otherwise `null`. |
| `path` | Matched project path when available; otherwise `null`. |
| `live_urls` | Array of usable local URLs for the endpoint, such as `http://127.0.0.1:3000/`. |
| `pid` | Owning process ID when the platform can report it; otherwise `null`. |
| `port` | Numeric TCP port. |
| `host` | Bound host or address when available; otherwise a best-effort local host such as `127.0.0.1` or `localhost`. |
| `command` | Process command line or executable name when available; otherwise `null`. |
| `cwd` | Process working directory when available; otherwise `null`. |
| `confidence` | Match confidence: `high`, `medium`, `low`, or `unknown`. |
| `source` | Discovery source, for example `lsof`, `netstat`, `ss`, `procfs`, or `unknown`. |

Confidence levels describe the project association, not whether the port is
open:

| Confidence | Meaning |
|------------|---------|
| `high` | Process `cwd` is inside a discovered project or otherwise maps directly to a known project path. |
| `medium` | Command, environment, or path evidence points to a project but does not prove the runtime directory. |
| `low` | The port is live and has weak project evidence, such as a name-only match. |
| `unknown` | The port is live but cannot be associated with a project. |

`pj ports --project <query>` applies the same project query semantics as other
project commands: name, id, or path-like query. If the query does not match a
known project, the command returns `"success": false`, `data: []`, and
`meta.error`. If the query matches a project but no runtime ports are found, it
returns `"success": true` with empty `data`.

Platform support is best effort and stdlib-compatible from pj's side. Discovery
may use platform tools when available, but missing tools, permission limits, or
unsupported process metadata must degrade by returning `null` fields or lower
confidence instead of failing the whole command. A command failure is reserved
for invalid arguments, unreadable project state, or an unexpected discovery
error; partial discovery failures should be reported in `meta.warnings` when
practical.

Port discovery should become a separate tool instead of pj core if it needs to
act on processes, probe remote hosts, require privileged agents, maintain a
long-lived watcher, collect historical telemetry, or perform protocol-specific
health checks. Those behaviors are runtime management or observability concerns;
pj should only surface the current local signal needed to enrich project
navigation.

`pj census --include-ports` keeps census rows as the primary data shape and adds
port information without changing the default `pj census` contract. Each census
row may include a `ports` array containing the same port records returned by
`pj ports --project <query>`, and census metadata may include
`ports_included: true`, `ports_total`, and `ports_sources`. Readers must ignore
unknown census row fields and unknown port record fields.

#### Census web API contract

The census server is a local, stdlib-only HTTP wrapper around the CLI-first
JSON contract. Every API response is an envelope:

```json
{
  "success": true,
  "data": {},
  "meta": {}
}
```

Errors use the same envelope shape with `"success": false`, an empty `data`
array, and `meta.error`.

Read endpoints:

| Endpoint | CLI equivalent | Data shape | Metadata |
|----------|----------------|------------|----------|
| `GET /api/health` | `pj census status` health check | `{ "status": "running" }` | none |
| `GET /api/census?refresh=1` | `pj census` | array of normalized census rows | census summary fields such as `total`, `state_counts`, `category_counts`, `origin_counts`, `session_total`, `duration_hrs_total`, `generated_at` |
| `GET /api/census?refresh=1&include_ports=1` | `pj census --include-ports` | array of normalized census rows with optional `ports` arrays | census summary fields plus optional `ports_included`, `ports_total`, `ports_sources`, and `warnings` |
| `GET /api/next?limit=5` | `pj next --limit 5` | array of scored project recommendations | `total`, `limit`, `latency_ms` |
| `GET /api/ports?project=name-or-id-or-path` | `pj ports [--project <query>]` | array of port records | `total`, optional `project`, `sources`, `warnings`, `latency_ms` |
| `GET /api/search?q=term&q=other&limit=20&sort=newest&project=name&match=any&regex=0` | `pj search ...` | array of project search matches | `query`, `project`, `match`, `regex`, `sort`, `total`, `limit`, `latency_ms`, optional `hint` |
| `GET /api/show?project=name-or-id&sessions=10` | `pj show <project>` | project object with `sessions` and `resume_cmd` | `latency_ms` |
| `GET /api/chats?project=name-or-id&limit=20` | `pj chats <project>` | array of session summaries | `project`, `total`, `limit`, `latency_ms` |
| `GET /api/chat/<session_id>?no_tools=1&roles=user,assistant&all_branches=0&last=10&offset=0&limit=5` | `pj chat <session_id>` | session object with paginated `messages` | `total_messages`, `offset`, `limit`, `latency_ms` |

Annotation endpoints append events through `pj.annotate`; they never rewrite
annotation state directly:

| Endpoint | CLI equivalent | JSON body |
|----------|----------------|-----------|
| `POST /api/annotations/note` | `pj note <project> <text>` | `{ "project": "name-or-id-or-path", "text": "next step" }` |
| `POST /api/annotations/prioritize` | `pj prioritize <project> <level>` | `{ "project": "name-or-id-or-path", "level": "high" }` |
| `POST /api/annotations/tag` | `pj tag <project> <tag>` | `{ "project": "name-or-id-or-path", "tag": "backend" }` |
| `POST /api/annotations/archive` | `pj archive <project>` | `{ "project": "name-or-id-or-path" }` |

#### Web UX maintenance rules

The web UX served by `pj census serve` is a local view over pj's CLI-first
contracts. Future web features should preserve the same layering:

1. CLI command or documented contract defines the behavior and JSON envelope.
2. Server endpoint wraps that command/contract, validates browser input at the
   HTTP edge, and returns the same envelope shape.
3. Browser UI calls the server endpoint and renders the returned data.

Do not put project, session, port, search, or annotation behavior only in the
browser. A web/API capability should map to an existing or proposed CLI
command/contract, as the endpoints above do for `pj census`, `pj ports`,
`pj search`, `pj show`, `pj chats`, `pj chat`, and annotation commands. If a
future web feature needs new behavior, define the CLI or contract boundary
first, then add the endpoint and UI.

The browser must not maintain a parallel source of truth. After a write, append
through the annotation endpoint or other actuator endpoint, then refresh or
rederive UI state from census, project, session, port, search, or annotation
read endpoints. Optimistic display is acceptable only as temporary feedback
while the canonical endpoint response is pending.

Contribution checklist for web UX changes:

- Keep pj CLI-first: every new capability has a CLI command or documented
  contract before it becomes browser behavior.
- Preserve the standard envelope: `{ "success": ..., "data": ..., "meta": ... }`
  for API responses and errors.
- Validate untrusted browser input at the endpoint boundary; keep core logic on
  normalized values.
- Append events for mutations; do not rewrite annotation state from the UI.
- Avoid agent infrastructure in the web layer: no prompts, agent registries, or
  browser-only orchestration protocols.
- Refresh from canonical endpoints after writes instead of maintaining duplicate
  UI state.
- Update this contract when adding, renaming, or removing endpoints.

Run Playwright browser validation when a change affects rendered web UI,
browser interaction, routing, client-side state transitions, or the
`pj census serve` HTTP surface used by the page. Documentation-only changes and
CLI-only changes do not require Playwright unless they also alter web behavior.

### `pj show` - Deep dive into a project

```bash
pj show myproject --pretty
```

Shows project metadata, recent sessions with models and durations, and a resume command for the latest session.

`pj status` remains as a hidden compatibility alias.

### `pj chats` - List chats for a project

```bash
pj chats --here --pretty
pj chats myproject --limit 50
pj chat list --here --pretty
```

Lists the session IDs and titles for a project. With no project argument,
`pj chats` infers the project from the current working directory; `pj chat list`
is an alias for the same listing command.

### `pj resume` — Jump back in

```bash
eval $(pj resume myproject)
```

Outputs `cd <path> && <agent-specific resume command>` for the most recent session.

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
| Codex | `codex resume <id>` |
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
