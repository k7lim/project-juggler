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
| `pj next` | current local scheduling implementation | call `GET /api/next` |

`PJ_REMOTE_URL` should affect read commands only. Actuators should stay local
until there is an explicit remote-write design with dry-run and auth.

## HTTP Service

Use the existing census server as the host read service. It already exposes the
needed read endpoints:

- `GET /api/health`
- `GET /api/next?limit=5`
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

Before documenting `--host 0.0.0.0` as a normal workflow, define and implement a
remote exposure policy. This is now more than read auth: the census server also
has annotation POST endpoints, so non-loopback serving must not silently expose
actuators.

Likely shape:

- server reads a read token such as `PJ_READ_TOKEN`
- server may read a separate write token such as `PJ_WRITE_TOKEN`
- client reads `PJ_REMOTE_TOKEN` for read calls
- client sends `Authorization: Bearer <token>`
- if no server token is configured, keep loopback-only usage as the safe default

Input validation should live at the CLI/API boundary:

- reject control characters in identifiers and query parameters
- reject `?`, `#`, and `%` in identifiers such as project refs and session IDs
- keep query strings as query values, not pre-encoded user input
- preserve the existing envelope shape for success and error responses

Mutation endpoints already exist on the census server for annotations. They
should not be part of the sandbox-agent contract unless a separate remote-write
policy is intentionally designed and implemented.

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

1. Decide remote exposure/auth policy, skill packaging, and `/api/ports`
   contract.
2. Add the project-juggler skill file with the sensor/actuator contract.
3. Add `pj health` with the standard envelope and a health-only remote path.
4. Add the remote exposure/auth policy for the census server and remote client.
5. Route `search`, `show`, `chats`, `chat`, and `next` through remote mode as
   separate vertical slices.
6. Add docs for host setup and sandbox setup.
7. Later: add `pj schema` or `pj --describe` for runtime introspection.

## Ambiguity Flattening

The task graph below intentionally does not ask implementation agents to make
product or architecture decisions. It turns the remaining choices into explicit
HITL decision tasks:

- Remote exposure policy: the same census server now has read endpoints and
  annotation write endpoints, so non-loopback serving needs a formal auth/write
  policy before it is documented as normal.
- Skill packaging: the plan needs a chosen location and distribution mechanism
  before an AFK agent can add a usable skill without guessing.
- `/api/ports` contract: README documents `GET /api/ports`, while the current
  server exposes port data through `GET /api/census?include_ports=1`; choose
  implement-versus-defer before assigning the code/doc alignment task.

This graph assumes remote mode is explicit through `PJ_REMOTE_URL`; automatic
`host.docker.internal` probing is out of scope for the first implementation.
It also assumes field selection is deferred; first-pass context control uses
existing `limit`, `offset`, `last`, `no_tools`, and `roles` parameters.

## Task Breakdown

The following tasks are written for conversion into tracker issues. Each task is
atomic, independently verifiable, and includes enough onboarding for an agent
with no prior conversation context.

### Task A: Decide Remote Exposure and Auth Policy

Type: HITL

#### Objective

Choose and record the security policy for using the census server as a host-side
service reachable from disposable Docker/yolobox sandboxes. The decision must
cover reads, annotation writes, control endpoints, token names, and behavior for
non-loopback binds.

#### Context

- Source: `docs/plans/pj-sandbox-agent-access.md#security-and-safety-rails`
- Files:
  - `pj/census_server.py` - current HTTP routes and unauthenticated annotation
    POST handling
  - `pj/census_process.py` - background server lifecycle and control token
    handling
  - `README.md` - documented web API contract and web UX maintenance rules
  - `tests/test_census.py` - existing endpoint tests
- Discovery:
  - `rg "PJ_CENSUS_CONTROL_TOKEN|annotations|do_POST|do_GET" pj/census_server.py pj/census_process.py`
  - `rg "GET /api|POST /api|web API contract|annotations" README.md`
  - `rg "annotation|control|health|search endpoint" tests/test_census.py`
- Scratchpad/plan: `docs/plans/pj-sandbox-agent-access.md`

#### Scope

In:
- Decide whether all non-loopback serving requires auth.
- Decide whether reads and writes use separate tokens.
- Decide whether annotation writes are disabled remotely by default or gated.
- Decide how unsafe `pj census serve/start --host 0.0.0.0` behaves.

Out:
- Implementing the policy in code.
- Designing remote annotation writes beyond the minimum safe exposure rule.

#### Acceptance Criteria

- [ ] The decision is written into this plan or a linked ADR/decision section.
- [ ] Token names and header format are specified.
- [ ] Loopback versus non-loopback behavior is specified.
- [ ] Annotation write behavior is specified separately from read behavior.
- [ ] Downstream implementation tasks no longer need to choose a security model.

#### Testing

- Documentation-only task; no test command required.
- Optional sanity check: `rg "PJ_READ_TOKEN|PJ_WRITE_TOKEN|Authorization|non-loopback" docs README.md`

#### Dependencies

- Blocked by: None - can start immediately.
- Blocks: Tasks C, E, and F.

#### Notes

The current control endpoint already uses `PJ_CENSUS_CONTROL_TOKEN`; do not
weaken that path. Treat browser, sandbox-agent, and user input as untrusted at
the HTTP boundary.

### Task B: Decide Skill Location and Distribution

Type: HITL

#### Objective

Choose where the project-juggler agent skill should live and how it should be
installed or discovered. The decision must let an implementation agent add the
skill without inventing project-specific packaging conventions.

#### Context

- Source: `docs/plans/pj-sandbox-agent-access.md#agent-skill-contract`
- Files:
  - `README.md` - user-facing install and command documentation
  - `AGENTS.md` - project-local agent instructions
  - `docs/plans/pj-sandbox-agent-access.md` - draft skill contract
- Discovery:
  - `rg -n "SKILL.md|skill|AGENTS.md|project-juggler" README.md AGENTS.md docs engineering_core.md`
  - `rg --files | rg '(^|/)(SKILL.md|skills/|AGENTS.md)$'`
- Scratchpad/plan: `docs/plans/pj-sandbox-agent-access.md`

#### Scope

In:
- Choose one skill location and distribution path.
- State whether the skill is project-local docs only, a packaged Codex skill, or
  both.
- State any required frontmatter/name constraints.

Out:
- Writing the skill content.
- Implementing installer or marketplace behavior.

#### Acceptance Criteria

- [ ] The chosen skill path is written down.
- [ ] The chosen distribution/install expectation is written down.
- [ ] The decision says whether `AGENTS.md` should reference the skill.
- [ ] The decision is recorded in this plan under Task B or a linked decision
      section, and the skill-location open question is resolved or points to
      that decision.
- [ ] Downstream skill implementation does not need to choose a location.

#### Testing

- Documentation-only task; no test command required.
- Optional sanity check: `rg "project-juggler|SKILL.md|skills/" docs README.md AGENTS.md`

#### Dependencies

- Blocked by: None - can start immediately.
- Blocks: Task C.

#### Notes

If choosing a packaged Codex skill, specify the exact skill path, required
`SKILL.md` frontmatter fields (`name: project-juggler`, `description: ...`),
and whether install/discovery is project-local, packaged, or both.

### Task C: Add Project-Juggler Agent Skill

Type: AFK

#### Objective

Add the project-juggler agent skill at the location chosen in Task B. The skill
must teach agents how to use `pj` as a sensor, how to detect/use host-backed
remote reads from a sandbox, and which commands are actuators requiring explicit
user intent.

#### Context

- Source: `docs/plans/pj-sandbox-agent-access.md#agent-skill-contract`
- Files:
  - final skill path recorded by Task B; read the closed Task B decision before
    editing
  - `docs/plans/pj-sandbox-agent-access.md` - source contract and sandbox
    workflow
  - `README.md` - current CLI and web API behavior
  - `pj/cli.py` - current command names and flags
- Discovery:
  - `rg "pj search|pj show|pj chats|pj chat|pj next" README.md pj/cli.py`
  - `rg "PJ_REMOTE_URL|PJ_REMOTE_TOKEN|PJ_READ_TOKEN|PJ_WRITE_TOKEN" docs README.md pj`
  - `rg "note|tag|prioritize|archive" README.md pj/cli.py pj/annotate.py`
- Scratchpad/plan: `docs/plans/pj-sandbox-agent-access.md`

#### Scope

In:
- Skill frontmatter and concise body.
- Sensor list: `search`, `show`, `chats`, `chat`, and `next`. Do not include
  `ports` unless this task is updated to depend on the ports contract decision.
- Actuator list: `note`, `tag`, `prioritize`, `archive`.
- Sandbox workflow using explicit `PJ_REMOTE_URL`.

Out:
- CLI implementation changes.
- Remote server auth implementation.
- Long tutorial docs duplicating README.

#### Acceptance Criteria

- [ ] Skill identifies read sensors and annotation actuators.
- [ ] Skill tells agents to prefer JSON envelopes and reserve `--pretty` for
      human-facing output.
- [ ] Skill explains that local sandbox `pj` may not see host chat history.
- [ ] Skill shows `PJ_REMOTE_URL=http://host.docker.internal:8765`.
- [ ] Skill reflects the token names/header behavior chosen in Task A.

#### Testing

- Documentation-only focused validation: `rg "PJ_REMOTE_URL|host.docker.internal|Actuators|Sensors" <skill-path>`
- Broader validation: `python3 -m pytest tests/test_pj.py::test_cli_search_help_teaches_query_strategy`

#### Dependencies

- Blocked by: Tasks A and B.
- Blocks: Task L.

#### Notes

Do not include agent framework prompts, orchestration protocols, or behavior
that would mutate annotations unless the user explicitly asks for that mutation.

### Task D: Decide `/api/ports` HTTP Contract

Type: HITL

#### Objective

Choose whether `GET /api/ports` should be implemented on the census server or
removed/deferred from the documented web API contract. This resolves the current
README/server mismatch before implementation.

#### Context

- Source: `docs/plans/pj-sandbox-agent-access.md#http-service`
- Files:
  - `README.md` - documents `GET /api/ports`
  - `pj/census_server.py` - current server routes
  - `pj/runtime_ports.py` - local runtime port sensor
  - `pj/cli.py` - `pj ports` command
  - `tests/test_runtime_ports.py` and `tests/test_census.py` - existing coverage
- Discovery:
  - `rg "api/ports|include_ports|pj ports|runtime_ports" README.md pj tests`
  - `rg "def _cmd_ports|ports_p|runtime_ports.ports" pj/cli.py pj/runtime_ports.py`
  - `rg "parsed.path == \"/api/ports\"|include_ports" pj/census_server.py`
- Scratchpad/plan: `docs/plans/pj-sandbox-agent-access.md`

#### Scope

In:
- Decide implement-now versus document-defer.
- If implementing, define query parameters and response envelope by mapping to
  `pj ports`.
- If deferring, define the replacement documentation text pointing users to
  `GET /api/census?include_ports=1`.

Out:
- Implementing the chosen behavior.
- Changing port detection semantics.

#### Acceptance Criteria

- [ ] The chosen direction is written in this plan or a linked decision note.
- [ ] If implementing, endpoint parameters and envelope metadata are specified.
- [ ] If deferring, README wording is specified.
- [ ] Downstream implementation does not need to choose between code and docs.

#### Testing

- Documentation-only task; no test command required.
- Optional sanity check: `rg "api/ports|include_ports" README.md docs/plans/pj-sandbox-agent-access.md`

#### Dependencies

- Blocked by: None - can start immediately.
- Blocks: Task G.

#### Notes

This is independent of host-backed chat search. It is included because a remote
CLI contract should not advertise an endpoint the server does not route.

### Task E: Add `pj health` and Health-Only Remote Client Path

Type: AFK

#### Objective

Add `pj health` as the first vertical remote-read slice. Local mode should
report local capability and local census service status; remote mode should call
`GET /api/health` using `PJ_REMOTE_URL` and return a standard `pj` envelope.

#### Context

- Source: `docs/plans/pj-sandbox-agent-access.md#cli-behavior`
- Files:
  - `pj/cli.py` - argparse setup and command dispatch
  - `pj/census_process.py` - local background server status helper
  - `pj/census_server.py` - `GET /api/health`
  - `pj/envelope.py` - standard envelope helpers
  - `tests/test_pj.py` - CLI tests
  - `tests/test_census.py` - existing `pj census status` regression test
- Discovery:
  - `rg "census status|_cmd_census|census_process.status|api/health" pj tests`
  - `rg "def build_parser|elif args.command|envelope.ok|envelope.err" pj/cli.py pj/envelope.py`
  - `rg "PJ_REMOTE_URL|urlopen|Request|urllib" pj tests`
- Scratchpad/plan: `docs/plans/pj-sandbox-agent-access.md`

#### Scope

In:
- `pj health` parser command.
- Local status envelope.
- Remote `GET /api/health` call when `PJ_REMOTE_URL` is set.
- Token header support using names chosen in Task A.
- Focused tests with mocked HTTP/status calls.

Out:
- Routing other read commands through remote mode.
- Starting/stopping the census server.
- Auto-detecting Docker host URLs.

#### Acceptance Criteria

- [ ] Local `pj health` returns
      `envelope.ok({"mode": "local", "census": census_process.status(), "remote_url": None})`.
- [ ] `PJ_REMOTE_URL=... pj health` calls `/api/health`.
- [ ] Remote `pj health` returns the remote server envelope as JSON.
- [ ] Remote invalid JSON, URL errors, and non-2xx HTTP errors return
      `envelope.err(..., source="remote")` with `meta.error`.
- [ ] After Task A is closed, use its chosen client env var/header names
      exactly; do not invent auth names in this task.
- [ ] Existing `pj census status` behavior is unchanged.

#### Testing

- Focused: `python3 -m pytest tests/test_pj.py tests/test_census.py -k "health or census_status"`
- Broader: `python3 -m pytest tests/test_pj.py tests/test_census.py`

#### Dependencies

- Blocked by: Task A.
- Blocks: Tasks H, I, J, and K.

#### Notes

Keep this slice small. A tiny remote HTTP helper may be introduced here, but it
only needs to support health until later slices extend it.

### Task F: Enforce Remote Server Exposure Policy

Type: AFK

#### Objective

Implement the remote exposure/auth policy chosen in Task A on the census server
and process start/serve paths. Non-loopback serving must not silently expose
annotation actuators.

#### Context

- Source: closed Task A decision and
  `docs/plans/pj-sandbox-agent-access.md#security-and-safety-rails`. Do not
  start this task unless Task A records token names, Authorization/header
  format, loopback versus non-loopback behavior, and annotation write policy. If
  any of those are missing, stop as blocked; do not choose policy here.
- Files:
  - `pj/census_server.py` - HTTP route handling, auth checks, control endpoint,
    annotation endpoints
  - `pj/census_process.py` - background start/status state
  - `pj/cli.py` - `pj census serve/start` flags and errors
  - `tests/test_census.py` - server endpoint tests
  - `tests/test_pj.py` - CLI/process behavior tests if CLI output changes
- Discovery:
  - `rg "PJ_CENSUS_CONTROL_TOKEN|PJ_READ_TOKEN|PJ_WRITE_TOKEN|Authorization|X-PJ" pj tests`
  - `rg "def do_GET|def do_POST|_handle_annotation|api/control/stop" pj/census_server.py`
  - `rg "def start|command = \\[|PJ_CENSUS_BACKGROUND" pj/census_process.py`
- Scratchpad/plan: `docs/plans/pj-sandbox-agent-access.md`

#### Scope

In:
- Server-side enforcement for read/write/control surfaces.
- Startup behavior for unsafe non-loopback serving as chosen in Task A.
- Structured envelope errors where requests reach the API layer.
- Focused tests for loopback default, non-loopback auth, bad token, good token,
  and write-gating behavior.

Out:
- Remote CLI read command routing.
- Remote annotation write CLI support.
- TLS, user accounts, or multi-tenant auth.

#### Acceptance Criteria

- [ ] Non-loopback serving follows the Task A policy.
- [ ] Implementation exactly follows the closed Task A policy for read auth,
      write auth/disablement, header format, and unsafe non-loopback bind
      behavior.
- [ ] Read endpoints and annotation endpoints follow the separate read/write
      handling specified by Task A.
- [ ] Control endpoint remains protected by its control token.
- [ ] Unauthenticated and unauthorized requests fail predictably.
- [ ] Tests cover both safe defaults and configured remote exposure.

#### Testing

- Focused: `python3 -m pytest tests/test_census.py -k "health or annotation or control or auth"`
- Broader: `python3 -m pytest tests/test_census.py tests/test_pj.py`
- UX/browser: because this changes the census HTTP surface used by the
  dashboard, run Playwright validation per README web UX rules, or explicitly
  document why the final code path does not affect rendered UI/browser
  interactions.

#### Dependencies

- Blocked by: Task A.
- Blocks: Task L.

#### Notes

Prefer a small helper for auth checks in `pj/census_server.py`; avoid scattering
token parsing across every handler.

### Task G: Align `/api/ports` Contract With Server

Type: AFK

#### Objective

Apply the closed Task D decision exactly so the documented web API and census
server behavior agree about port data. Do not choose implement-versus-defer in
this task. If Task D does not specify the direction and exact contract/doc
wording, stop and return to Task D.

#### Context

- Source: closed Task D decision plus this Task G plan
- Files:
  - `README.md` - web API contract table
  - `pj/census_server.py` - server route table and handlers
  - `pj/runtime_ports.py` - runtime port sensor
  - `pj/cli.py` - `pj ports` command
  - `tests/test_runtime_ports.py` and `tests/test_census.py` - test coverage
- Discovery:
  - `rg "api/ports|include_ports|pj ports|runtime_ports" README.md pj tests`
  - `rg "def _cmd_ports|runtime_ports.ports|ports_p" pj/cli.py pj/runtime_ports.py`
  - `rg "ThreadingHTTPServer|_api_request|include_ports" tests/test_census.py`
- Scratchpad/plan: `docs/plans/pj-sandbox-agent-access.md`

#### Scope

In:
- The chosen code/doc change from Task D.
- Tests for the chosen endpoint or documentation contract.

Out:
- Changing runtime port detection heuristics.
- Remote CLI support for `pj ports` unless explicitly chosen by Task D.

#### Acceptance Criteria

- [ ] README and server behavior agree.
- [ ] The implementation/doc change matches the explicit closed Task D
      decision; this task makes no new contract choice.
- [ ] If implemented, `/api/ports` returns the standard envelope and wraps
      existing `pj ports` semantics.
- [ ] If deferred, README states port data is available via
      `/api/census?include_ports=1`.
- [ ] Focused tests cover the chosen behavior.

#### Testing

- If implemented: `python3 -m pytest tests/test_census.py tests/test_runtime_ports.py`
- If docs-only defer: `python3 -m pytest tests/test_runtime_ports.py` and
  `rg "api/ports|include_ports" README.md docs/plans/pj-sandbox-agent-access.md`

#### Dependencies

- Blocked by: Task D.
- Blocks: Task L.

#### Notes

Do not let this task grow into a broader port-detection refactor.

### Task H: Route `pj search` Through Remote Mode

Type: AFK

#### Objective

Make `pj search` use the host service when `PJ_REMOTE_URL` is set while
preserving local behavior, JSON envelope shape, query semantics, and `--pretty`
rendering.

#### Context

- Source: `docs/plans/pj-sandbox-agent-access.md#cli-behavior`
- Files:
  - `pj/cli.py` - `_cmd_search`
  - `pj/search.py` - local search semantics
  - `pj/census_server.py` - `/api/search` query contract
  - `pj/pretty.py` - `print_search`
  - remote client helper/path added by Task E - URL building, token headers,
    connection errors, and envelope errors
  - `tests/test_pj.py` and `tests/test_census.py` - current search tests
- Discovery:
  - `rg "def _cmd_search|SEARCH_HELP|looks_like_regex|print_search" pj tests`
  - `rg "_handle_search|/api/search|query=query" pj/census_server.py tests/test_census.py`
  - `rg "test_cli_search|test_census_server_search" tests`
  - `rg "PJ_REMOTE_URL|PJ_REMOTE_TOKEN|urlopen|Request|remote" pj tests`
- Scratchpad/plan: `docs/plans/pj-sandbox-agent-access.md`

#### Scope

In:
- Remote mode for `pj search` only.
- URL/query encoding for `q`, `limit`, `sort`, `project`, `match`, and `regex`.
- Preserve existing `--here` behavior in remote mode: resolve the current
  project before the request, reject `--project` with `--here`, error outside a
  discovered project, and send the resolved path as the `project` query
  parameter.
- Preserve local mode when `PJ_REMOTE_URL` is absent.
- Preserve `--pretty` output by rendering remote `data`.

Out:
- Remote mode for other commands.
- New search ranking or field selection.
- Automatic Docker host discovery.

#### Acceptance Criteria

- [ ] Local `pj search` tests still pass unchanged.
- [ ] With `PJ_REMOTE_URL`, `pj search` calls `/api/search`.
- [ ] Multi-term queries become repeated `q` parameters.
- [ ] Remote `--here` sends `project=<resolved path>` and JSON mode preserves
      `meta.here` and `meta.project`.
- [ ] Remote server envelopes with `success: false` print the envelope and exit
      nonzero; connection/HTTP/JSON failures also return `success: false` with
      `meta.error`.
- [ ] Implementation reuses the remote HTTP helper/client introduced by Task E;
      do not create a second ad hoc remote stack.
- [ ] `--pretty` renders remote results.

#### Testing

- Focused: `python3 -m pytest tests/test_pj.py -k "search and remote"`
- Add focused CLI tests for remote URL construction, repeated `q`, remote error
  envelope handling, remote `--pretty`, and remote `--here`.
- Regression: `python3 -m pytest tests/test_pj.py::test_cli_search_json tests/test_census.py::test_census_server_search_endpoint_maps_cli_query_semantics`

#### Dependencies

- Blocked by: Task E.
- Blocks: Task L.

#### Notes

Keep regex hint behavior consistent. If the server provides `meta.hint`, the CLI
should preserve it in JSON mode and render equivalent human guidance in pretty
mode.

### Task I: Route `pj show` Through Remote Mode

Type: AFK

#### Objective

Make `pj show` use the host service when `PJ_REMOTE_URL` is set while
preserving local project resolution behavior, JSON envelope shape, session
limit handling, and `--pretty` rendering.

#### Context

- Source: `docs/plans/pj-sandbox-agent-access.md#cli-behavior`
- Files:
  - `pj/cli.py` - `_cmd_show`
  - `pj/project_sessions.py` - local project detail helper
  - `pj/census_server.py` - `/api/show`
  - `pj/pretty.py` - project detail rendering
  - remote client helper/path added by Task E - URL building, token headers,
    connection errors, and envelope errors
  - `tests/test_pj.py` and `tests/test_census.py` - show/project detail tests
- Discovery:
  - `rg "def _cmd_show|resolve_project_detail|print_status|/api/show" pj tests`
  - `rg "test_cli_status|test_cli_show|test_census_server_show" tests`
  - `rg "project_session_data|resolve_project_detail" pj/project_sessions.py`
- Scratchpad/plan: `docs/plans/pj-sandbox-agent-access.md`

#### Scope

In:
- Remote mode for `pj show` only.
- `project` and `sessions` query mapping.
- The hidden `pj status` alias may share this implementation path; preserve its
  existing local behavior and cover or explicitly document remote behavior.
- Preserve local behavior when `PJ_REMOTE_URL` is absent.
- Preserve `--pretty` rendering from remote `data`.

Out:
- Remote mode for `pj chats` or `pj chat`.
- Changing project fuzzy resolution semantics.

#### Acceptance Criteria

- [ ] Local `pj show` behavior is unchanged.
- [ ] With `PJ_REMOTE_URL`, `pj show <project> --sessions N` builds a
      structured request to `/api/show` with URL-encoded `project=<project>` and
      `sessions=N`.
- [ ] Remote 400/404 or `success: false` responses print a standard envelope
      and exit `1`; connection failures use the remote client error behavior
      introduced by Task E.
- [ ] `--pretty` renders remote project details.
- [ ] Focused tests cover JSON and pretty remote paths.

#### Testing

- Focused: `python3 -m pytest tests/test_pj.py -k "show and remote"`
- Add focused mocked-HTTP CLI tests named to match `-k "show and remote"`; keep
  existing `test_cli_status_*` alias regression coverage.
- Regression: `python3 -m pytest tests/test_census.py -k "show"`

#### Dependencies

- Blocked by: Task E.
- Blocks: Task L.

#### Notes

Do not make `--here` for `show`; current CLI does not expose that flag.

### Task J: Route `pj chats` and `pj chat` Through Remote Mode

Type: AFK

#### Objective

Make `pj chats` and `pj chat` use the host service when `PJ_REMOTE_URL` is set,
so a sandbox agent can list sessions for a host project and fetch bounded chat
content without mounting the host session store.

#### Context

- Source: `docs/plans/pj-sandbox-agent-access.md#sandbox-workflow`
- Files:
  - `pj/cli.py` - `_cmd_chats`, `_cmd_chat`, aliases, flags
  - `pj/census_server.py` - `/api/chats`, `/api/chat`, `/api/chat/<session_id>`
  - `pj/pretty.py` - session and chat renderers
  - remote client helper/path added by Task E - URL building, token headers,
    connection errors, and envelope errors
  - `tests/test_pj.py` and `tests/test_census.py` - current chat tests
- Discovery:
  - `rg "def _cmd_chats|def _cmd_chat|chat list|--no-tools|--roles|--last" pj/cli.py tests/test_pj.py`
  - `rg "_handle_chats|_handle_chat|/api/chat" pj/census_server.py tests/test_census.py`
  - `rg "print_chat|print_sessions" pj/pretty.py`
- Scratchpad/plan: `docs/plans/pj-sandbox-agent-access.md`

#### Scope

In:
- Remote mode for `pj chats`.
- Remote mode for `pj chat`.
- When `PJ_REMOTE_URL` is set, `pj chat list ...` follows the same remote path
  as `pj chats ...` because the alias rewrite happens before dispatch.
- When `PJ_REMOTE_URL` is set, require an explicit project for `pj chats` unless
  `--here` can be mapped by existing local project resolution; do not invent
  host-side CWD mapping.
- Query mapping for `project`, `limit`, `session_id`, `roles`, `no_tools`,
  `all_branches`, `last`, and `offset`.
- Preserve local mode and `chat list` alias when `PJ_REMOTE_URL` is absent.

Out:
- Remote resume command execution.
- Remote annotation writes.
- Changing local chat parsing/session-store behavior.
- Automatic sandbox CWD to host project mapping for `pj chats --here` or bare
  `pj chats`.

#### Acceptance Criteria

- [ ] With `PJ_REMOTE_URL`, `pj chats <project> --limit N` calls `/api/chats`.
- [ ] With `PJ_REMOTE_URL`, `pj chat <session_id> ...` calls `/api/chat/<id>`.
- [ ] With `PJ_REMOTE_URL`, ambiguous/missing project input for `pj chats`
      returns a standard CLI error rather than silently using sandbox-local
      discovery.
- [ ] Local `pj chats`, `pj chat`, and `pj chat list` behavior is unchanged.
- [ ] `--pretty`, `--no-tools`, `--roles`, `--last`, `--offset`, and `--limit`
      behavior is covered by tests.
- [ ] Remote errors return standard CLI errors.

#### Testing

- Focused: `python3 -m pytest tests/test_pj.py -k "remote and chat"` with new
  tests named like `test_cli_remote_chats_*` and `test_cli_remote_chat_*`.
- Regression: `python3 -m pytest tests/test_pj.py -k "cli_chats or cli_chat" tests/test_census.py -k "chat"`

#### Dependencies

- Blocked by: Task E.
- Blocks: Task L.

#### Notes

For sandbox agents, this is the key drill-down path after `pj search`.

### Task K: Route `pj next` Through Remote Mode

Type: AFK

#### Objective

Make `pj next` use the host service when `PJ_REMOTE_URL` is set while preserving
local scheduling behavior and pretty rendering. This lets sandbox agents inspect
the host-side work queue without owning the host project index.

#### Context

- Source: `docs/plans/pj-sandbox-agent-access.md#cli-behavior`
- Files:
  - `pj/cli.py` - `_cmd_next`
  - `pj/schedule.py` - local scoring heuristic
  - `pj/census_server.py` - `/api/next`
  - `pj/pretty.py` - `print_next`
  - `pj/envelope.py` - standard success/error envelope behavior
  - remote client helper/path added by Task E - URL building, token headers,
    connection errors, and envelope errors
  - `tests/test_pj.py` and `tests/test_census.py` - next tests
- Discovery:
  - `rg "def _cmd_next|print_next|score_projects|/api/next" pj tests`
  - `rg "test_cli_next|test_census_server_next" tests`
  - `rg "next_p.add_argument|--limit" pj/cli.py`
  - `rg "PJ_REMOTE_URL|PJ_REMOTE_TOKEN|urlopen|Request|remote" pj tests`
  - `rg "def _cmd_health|api/health|remote client" pj tests`
- Scratchpad/plan: `docs/plans/pj-sandbox-agent-access.md`

#### Scope

In:
- Remote mode for `pj next`.
- `limit` query mapping.
- Preserve local behavior when `PJ_REMOTE_URL` is absent.
- Preserve `--pretty` rendering from remote `data`.

Out:
- Changing schedule scoring.
- Adding browser next-queue behavior; it already exists.

#### Acceptance Criteria

- [ ] Local `pj next` behavior is unchanged when `PJ_REMOTE_URL` is absent.
- [ ] With `PJ_REMOTE_URL`, `pj next --limit N` calls `/api/next?limit=N` using
      the remote client path from Task E.
- [ ] Remote success JSON prints the returned standard envelope `data`/`meta`
      without local scoring.
- [ ] Remote envelope errors and connection errors follow the standard CLI
      error behavior established by Task E.
- [ ] `pj next --pretty` renders remote `data` through `pretty.print_next`.
- [ ] Focused tests cover remote JSON, remote pretty, remote envelope error,
      and local no-remote regression.

#### Testing

- Focused: `python3 -m pytest tests/test_pj.py -k "next and remote"`
- Regression: `python3 -m pytest tests/test_pj.py::test_cli_next_json tests/test_census.py::test_census_server_next_endpoint_uses_schedule_flow`

#### Dependencies

- Blocked by: Task E.
- Blocks: Task L.

#### Notes

This is a read-only sensor. Do not add remote annotation actions here.

### Task L: Document Host and Sandbox Setup

Type: AFK

#### Objective

Document the end-to-end workflow for running `pj` on the host and using the
same CLI from a disposable sandbox through `PJ_REMOTE_URL`.

#### Context

- Source: `docs/plans/pj-sandbox-agent-access.md`
- Files:
  - `README.md` - user-facing CLI and web API documentation
  - `docs/plans/pj-sandbox-agent-access.md` - design and task graph
  - `pj/cli.py` - actual flags and command names
  - project-juggler skill path added by Task C - agent-facing workflow
- Discovery:
  - `rg --files | rg '(^|/)SKILL\.md$|project-juggler|skills'`
  - `rg "census start|census serve|PJ_REMOTE_URL|host.docker.internal|pj health" README.md docs`
  - `rg "pj search|pj show|pj chats|pj chat|pj next" README.md docs`
  - `rg "PJ_READ_TOKEN|PJ_WRITE_TOKEN|PJ_REMOTE_TOKEN|Authorization" README.md docs pj`
- Scratchpad/plan: `docs/plans/pj-sandbox-agent-access.md`

#### Scope

In:
- Host setup command.
- Sandbox env vars and example commands.
- Token/auth setup from Task A/Task F.
- Clear note that annotation writes are actuators.
- Troubleshooting for missing host service or bad token.

Out:
- Implementing CLI/server behavior.
- Writing long conceptual docs already covered by this plan.
- Automatic Docker host detection.

#### Acceptance Criteria

- [ ] README includes host setup and sandbox setup.
- [ ] README examples include `pj health`, `pj search`, `pj show`, `pj chat`,
      and `pj next`.
- [ ] README and skill both identify annotation writes as actuators.
- [ ] README and skill reflect the final auth token names and write policy.
- [ ] Skill and README do not contradict each other.

#### Testing

- Focused docs check: `rg "PJ_REMOTE_URL|host.docker.internal|pj health|PJ_REMOTE_TOKEN" README.md docs`
- Skill/docs consistency check: `rg "PJ_REMOTE_URL|host.docker.internal|Actuators|Sensors" <skill-path> README.md`
- Command smoke: `python3 -m pj --help && python3 -m pj health`
- Playwright is not required for docs-only changes unless the edit changes
  rendered web UI, browser behavior, or the `pj census serve` HTTP surface.

#### Dependencies

- Blocked by: Tasks C, F, G, H, I, J, and K.
- Blocks: None known.

#### Notes

Do not document `--host 0.0.0.0` as normal until Task F enforces the exposure
policy.

## Dependency Graph

Use these labels when creating tracker issues, replacing them with real issue
IDs after creation:

- Task A: Decide Remote Exposure and Auth Policy - ready immediately.
- Task B: Decide Skill Location and Distribution - ready immediately.
- Task D: Decide `/api/ports` HTTP Contract - ready immediately.
- Task C: Add Project-Juggler Agent Skill - blocked by Tasks A and B.
- Task E: Add `pj health` and Health-Only Remote Client Path - blocked by Task A.
- Task F: Enforce Remote Server Exposure Policy - blocked by Task A.
- Task G: Align `/api/ports` Contract With Server - blocked by Task D.
- Task H: Route `pj search` Through Remote Mode - blocked by Task E.
- Task I: Route `pj show` Through Remote Mode - blocked by Task E.
- Task J: Route `pj chats` and `pj chat` Through Remote Mode - blocked by Task E.
- Task K: Route `pj next` Through Remote Mode - blocked by Task E.
- Task L: Document Host and Sandbox Setup - blocked by Tasks C, F, G, H, I, J,
  and K.

Ready-queue check: Tasks A, B, and D can run independently. After those
decisions, the skill, health path, auth enforcement, and ports contract can move
without requiring agents to re-plan. Once Task E lands, the command-specific
remote read slices can run independently. Task L is intentionally last so the
docs only publish a workflow that is implemented and safe.

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
