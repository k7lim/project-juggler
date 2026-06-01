# Plan: Host-Backed `pj` for Sandbox Agents

## Context

`pj` should be useful from two places:

- the outer host, where the full project/session index and annotations live
- disposable yolobox or Docker sandboxes, where agents need that broader memory but
  should not own or rebuild the index

Running the full `pj` stack inside a sandbox is misleading. The sandbox usually
sees only mounted project files and its own in-container dotfiles, so local
search can miss most host-side chats. The simpler model is location-transparent
read commands: host `pj` owns the data, sandbox `pj` can become a remote read
client when configured.

## Design Goal

Keep one CLI UX while changing the read backend by environment:

```text
host pj
  reads local pj/CASS/session stores
  can serve read APIs over localhost/Docker host networking

sandbox pj
  uses the same commands
  calls the host pj read service when PJ_REMOTE_URL is set
```

The host service is a facade over the broader chat index. The sandbox is a
client, not a second source of truth.

## Agent Skill Contract

Ship a `SKILL.md` before adding more behavior. The skill should teach agents
which commands are sensors, which are actuators, and how to handle sandbox
network boundaries.

Draft skill:

````markdown
---
name: project-juggler
description: Use pj to search and inspect local or host-side coding-agent project history, especially from disposable sandboxes that need broader chat memory.
---

# Project Juggler

## Model

`pj` is a project/session memory sensor. Read commands are safe to call
speculatively. Mutation commands are actuators and require explicit user intent.

When running inside yolobox or another disposable Docker sandbox, local `pj` may
only see sandbox-mounted data. If `PJ_REMOTE_URL` is set, use that host-side pj
service for reads.

## Sensors

Use these freely:

```bash
pj search <terms...> --limit 10
pj show <project> --sessions 10
pj chats <project> --limit 20
pj chat <session_id> --last 40 --no-tools
```

Expected output is the pj JSON envelope unless `--pretty` is requested:

```json
{"success": true, "data": [], "meta": {"total": 0, "latency_ms": 0}}
```

## Sandbox Workflow

1. Check whether remote pj is available:

```bash
pj health
```

2. If unavailable and inside Docker, try:

```bash
export PJ_REMOTE_URL=http://host.docker.internal:8765
pj health
```

3. Search broadly before asking the user for remembered project context:

```bash
pj search auth middleware --limit 8
```

4. Drill down only as needed:

```bash
pj show <project-id-or-name> --sessions 5
pj chat <session_id> --last 30 --no-tools
```

## Actuators

These mutate pj annotations:

```bash
pj note <project> <text>
pj tag <project> <tag>
pj prioritize <project> <high|medium|low|none>
pj archive <project>
```

Do not call them unless the user asked to update pj state.
````

## CLI Behavior

Read commands should choose their backend at the boundary:

| Command | Local host behavior | Sandbox/remote behavior |
|---|---|---|
| `pj health` | report local capability and census service status | call `GET /api/health` |
| `pj search` | current local search implementation | call `GET /api/search` |
| `pj show` | current local project detail implementation | call `GET /api/show` |
| `pj chats` | current local session listing implementation | call `GET /api/chats` |
| `pj chat` | current local session rendering implementation | call `GET /api/chat/<session_id>` |

`PJ_REMOTE_URL` should affect read commands only. Actuators should stay local
until there is an explicit remote-write design with dry-run and auth.

## HTTP Service

Use the existing census server as the host read service. It already exposes the
needed read endpoints:

- `GET /api/health`
- `GET /api/search?q=term&q=other&limit=20&sort=newest&project=name&match=any&regex=0`
- `GET /api/show?project=name-or-id&sessions=10`
- `GET /api/chats?project=name-or-id&limit=20`
- `GET /api/chat/<session_id>?no_tools=1&roles=user,assistant&all_branches=0&last=10&offset=0&limit=5`

For Docker sandboxes, the host can serve on an address reachable through the
Docker local URL convention:

```bash
pj census start --host 0.0.0.0 --port 8765
```

Then the sandbox can use:

```bash
export PJ_REMOTE_URL=http://host.docker.internal:8765
```

## Security and Safety Rails

Before documenting `--host 0.0.0.0` as a normal workflow, add read auth:

- server reads `PJ_READ_TOKEN`
- client reads `PJ_REMOTE_TOKEN`
- client sends `Authorization: Bearer <token>`
- if no server token is configured, keep loopback-only usage as the safe default

Input validation should live at the CLI/API boundary:

- reject control characters in identifiers and query parameters
- reject `?`, `#`, and `%` in identifiers such as project refs and session IDs
- keep query strings as query values, not pre-encoded user input
- preserve the existing envelope shape for success and error responses

Mutation endpoints already exist on the census server for annotations, but they
should not be part of the sandbox-agent contract yet.

## Output and Context Discipline

The CLI should keep the existing one-envelope JSON contract:

```json
{"success": true, "data": [], "meta": {"total": 0, "limit": 10, "latency_ms": 12}}
```

Remote mode should also support the same context-window controls as local mode:

- `--limit`
- `--offset` where the underlying command supports it
- `--last` for chat tails
- `--no-tools` for prompt-sized chat reads
- `--roles` for role-filtered chat reads

`--pretty` remains a human rendering layer over the same data.

## Implementation Order

1. Add the project-juggler skill file with the sensor/actuator contract.
2. Add `pj health` with the standard envelope.
3. Add a tiny remote HTTP client facade used only when `PJ_REMOTE_URL` is set.
4. Route `search`, `show`, `chats`, and `chat` through that facade in remote mode.
5. Add read-token support for the census server and remote client.
6. Add docs for host setup and sandbox setup.
7. Later: add `pj schema` or `pj --describe` for runtime introspection.

## Non-Goals

- Do not sync host session indexes into each sandbox.
- Do not require CASS or all agent dotfiles inside the sandbox.
- Do not introduce agent frameworks, registries, or agent-to-agent protocols.
- Do not make remote annotation writes available until dry-run and auth semantics
  are designed.

## Open Questions

- Should remote mode be explicit only (`PJ_REMOTE_URL`) or should `pj` try
  `host.docker.internal` automatically when it detects yolobox?
- Should read auth be mandatory for any non-loopback bind, or only warned?
- Where should the shipped skill live: project root `SKILL.md`, `skills/pj/`,
  or a packaged Codex skill directory?
- Should `/api/search` grow field selection, or is `limit` plus drill-down enough
  for the first sandbox-agent workflow?
