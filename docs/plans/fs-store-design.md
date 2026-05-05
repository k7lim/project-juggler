# Plan: `fs_store` — Filesystem-First Session Store

## Context

pj v1 facades over CASS (coding_agent_session_search) for session data. Direct filesystem reads were added because CASS had practical limitations at the time:
- **Slow full reindex** to discover new project directories
- **SQLite/index instability** in some local datasets
- **No freshness guarantee** when watch mode only monitors known dirs
- **Dual-dotfile hack** needed for sandbox sessions (`~/.claude-yolobox`)

claude-run (repos/claude-run) proves the alternative: direct filesystem reads give instant discovery with zero index lag.

## Decision

Use direct filesystem parsing as the default freshness baseline. Keep CASS as a first-class optional provider via the `SessionStore` Protocol (already implemented in `pj/session_store.py`) and re-evaluate it when its indexed data is richer than direct scans.

```
pj/
  session_store.py      # Protocol (done) — get_store() / set_store()
  cass_facade.py        # CASS backend (keep, can come back)
  fs_store.py           # NEW: filesystem backend (primary)
  parsers/
    __init__.py
    base.py             # AgentParser protocol + NormalizedSession/Message types
    claude_code.py      # ~/.claude/projects/ JSONL parser
    codex.py            # ~/.codex/sessions/ JSONL parser
    hermes.py           # ~/.hermes/sessions/ JSONL parser
    opencode.py         # .opencode/storage/ JSON tree parser
    gemini.py           # ~/.gemini/tmp/<hash>/chats/ JSON parser
    amp.py              # ~/.amp/cache/ JSON parser
    pi_agent.py         # ~/.pi/agent/sessions/ JSONL parser
    kimi.py             # ~/.kimi/sessions/ JSONL parser
```

## Agent Session Formats

### Claude Code
- **Location**: `~/.claude/projects/<encoded-path>/`
- **Format**: JSONL, one file per session
- **Naming**: `<uuid>.jsonl`
- **Workspace**: Encoded in directory name (`-Users-kevin-Foo-Bar` → `/Users/kevin/Foo/Bar`)
- **Session ID**: `sessionId` field in each line
- **Messages**: Lines with `type: "user"` or `type: "assistant"`, content in `message.content`
- **Model**: `message.model` field
- **Timestamps**: `.timestamp` field (ISO 8601)
- **Title**: First user message, first line, truncated to 100 chars
- **Env override**: `CLAUDE_CONFIG_DIR`

### Codex
- **Location**: `~/.codex/sessions/YYYY/MM/DD/` (or `$CODEX_HOME`)
- **Format**: JSONL, event-based
- **Naming**: `rollout-<timestamp>-<uuid>.jsonl`
- **Workspace**: `session_meta` event → `payload.cwd`
- **Session ID**: From `session_meta` event → `payload.id`
- **Messages**: `response_item` events with `payload.role`, content in `payload.content[].text`
- **Events**: `session_meta`, `response_item`, `event_msg`, `turn_context`
- **Model**: `turn_context` → `model` field
- **Timestamps**: `.timestamp` field (ISO 8601)
- **Title**: First user message content

### Hermes
- **Location**: `~/.hermes/sessions/`
- **Format**: JSONL, OpenAI chat format with top-level `role`
- **Naming**: `YYYYMMDD_HHMMSS_<hash>.jsonl`
- **Workspace**: None (agent-level, not project-level)
- **Session ID**: Derived from filename
- **Messages**: Lines with `role: "user"`, `role: "assistant"`, `role: "tool"`
- **Content**: Top-level `content` field (string)
- **Model**: `session_meta` line → `model` field
- **Platform**: `session_meta` line → `platform` field (cli, discord, telegram, etc.)
- **Reasoning**: `reasoning` field on assistant messages
- **Tool calls**: `tool_calls` array on assistant messages, `tool_call_id` on tool results
- **Timestamps**: `.timestamp` field (ISO 8601)
- **Title**: First user message content

### OpenCode
- **Location**: `.opencode/storage/` (per-project, under project root)
- **Format**: JSON file tree (NOT JSONL)
- **Structure**:
  ```
  .opencode/storage/
  ├── session/{projectID}/{sessionID}.json     # metadata
  ├── message/{sessionID}/{messageID}.json     # message headers
  └── part/{messageID}/{partN}.json            # content parts
  ```
- **Workspace**: `session.directory` field
- **Session ID**: `session.id` field
- **Messages**: 3-way join: session → messages (by sessionID) → parts (by messageID)
- **Roles**: `message.role` (user, assistant, system, tool)
- **Content**: Aggregated from parts — text as-is, tool wrapped in `[Tool Output]`, reasoning in `[Reasoning]`
- **Model**: `message.modelID`
- **Timestamps**: `time.created` / `time.updated` (ms since epoch)
- **Title**: `session.title` or first user message

### Gemini
- **Location**: `~/.gemini/tmp/<projectHash>/chats/` (or `$GEMINI_HOME`)
- **Format**: JSON, one session per file
- **Naming**: `session-*.json` or any `.json`
- **Workspace**: Parsed from message content (`# AGENTS.md instructions for <path>` or `Working directory: <path>`), fallback to `projectHash` dir
- **Session ID**: `sessionId` field
- **Messages**: `messages[]` array with `type` ("user" or "model") and `content`
- **Role mapping**: `"model"` → `"assistant"`
- **Timestamps**: `.timestamp` field (ISO 8601) on messages; `startTime`/`lastUpdated` on session
- **Title**: First line of first user message

### Amp
- **Location**: `~/.amp/cache/` (recursive scan)
- **Format**: JSON, one conversation per file
- **Naming**: `thread-*.json`, `conversation-*.json`, or `chat-*.json`
- **Workspace**: Multiple fallback keys: `workspace`, `cwd`, `path`, `project_path`, `repo`, `root` (message-level takes priority)
- **Session ID**: File stem (e.g. `thread-001` from `thread-001.json`), or `id` field
- **Messages**: `messages[]` array; role via `role`/`type`/`speaker` fallback chain; content via `content`/`text`/`body` fallback chain
- **Timestamps**: `created_at`/`createdAt`/`timestamp`/`ts` (supports ISO 8601 and ms integers)
- **Model**: `author`/`sender` fields if present
- **Nested variant**: `thread.messages` array
- **Title**: Explicit `title` field or first message

### Pi Agent
- **Location**: `~/.pi/agent/sessions/<projectName>/` (or `$PI_CODING_AGENT_DIR`)
- **Format**: JSONL, event-based
- **Naming**: `TIMESTAMP_UUID.jsonl` (must contain underscore)
- **Workspace**: `cwd` field in session header
- **Session ID**: `id` field in session header
- **Events**:
  - `session`: header with id, cwd, provider, modelId, thinkingLevel
  - `message`: contains `message` object with role/content
  - `model_change`: updates active model mid-session
  - `thinking_level_change`: updates thinking level
- **Messages**: `message.role` (user, assistant, toolResult); content is string or structured array
- **Content array types**: `text`, `thinking`, `toolCall` (with name/arguments)
- **Tool results**: role `"toolResult"` with `toolCallId`, `toolName`, `content`, `isError`
- **Model**: `modelId` from session header, updated by `model_change` events
- **Timestamps**: Event-level ISO 8601; message-level ms since epoch

### Kimi
- **Location**: `~/.kimi/sessions/<project>/<timestamp>_<uuid>/`
- **Format**: JSONL
- **Naming**: `wire.jsonl` (fixed name at leaf level)
- **Workspace**: Inferred from directory structure (project slug)
- **Session ID**: From directory name or session header
- **Messages**: Similar event-based JSONL to Pi Agent (details in franken_agent_detection crate)
- **Note**: Exact schema not fully documented in CASS test fixtures; implementation in external crate

## Data Types

```python
@dataclass
class NormalizedMessage:
    idx: int                    # Sequential index in conversation
    role: str                   # "user", "assistant", "tool", "system"
    content: str                # Flattened text content
    author: str | None          # Model name or reasoning marker
    created_at: int | None      # Timestamp (ms since epoch)

@dataclass
class NormalizedSession:
    session_id: str             # Unique session identifier
    agent: str                  # "claude_code", "codex", "hermes", etc.
    workspace: str | None       # Project/working directory path
    title: str | None           # First user message or explicit title
    started_at: int | None      # Min timestamp (ms since epoch)
    ended_at: int | None        # Max timestamp (ms since epoch)
    model: str | None           # Primary model used
    source_path: str            # Absolute path to session file
    messages: list[NormalizedMessage]
```

## fs_store Implementation

`fs_store.py` implements `SessionStore` Protocol by:

1. **Reading configured source directories** from env var or config
2. **Detecting agent type** per source dir (by path pattern / file naming)
3. **Delegating to per-agent parser** to produce `NormalizedSession` objects
4. **Serving SessionStore methods** from parsed data:
   - `list_projects()`: group sessions by workspace, count per agent
   - `project_sessions()`: filter by workspace path
   - `search_sessions()`: substring match on titles
   - `search_content()`: substring match on message content (or lazy SQLite FTS)
   - `recent_session_counts()`: filter by timestamp
   - `session_details()`: return model/version info

### Configuration

```bash
# Env var: colon-separated list of agent:path pairs
export PJ_SOURCES="claude:~/.claude/projects:codex:~/.codex/sessions:hermes:~/.hermes/sessions"

# Or auto-detect: scan known dotfile locations
# fs_store detects which agents are present and registers parsers
```

### Caching Strategy

- **Discovery cache**: mtime-based, rebuilt when any source dir changes
- **Content search**: optional lazy SQLite with FTS5
  - Messages cached on first access (e.g. `pj status <project>` or `pj search`)
  - New sessions added incrementally
  - No full reindex needed — new files discovered instantly

### Multi-Host Support

Remote machines sync session files to local mirrors:

```bash
# In PJ_SOURCES or sources config:
# hermes-m1 sessions synced via rsync
rsync user@hermes-m1:~/.hermes/sessions/ ~/.pj/mirrors/hermes-m1/.hermes/sessions/
```

pj reads mirrors identically to local dirs. A `pj sync` command wraps rsync for configured remotes.

```
~/.pj/
  mirrors/
    hermes-m1/.hermes/sessions/     # rsynced from M1
    cloud-1/.hermes/sessions/       # rsynced from cloud
  sources.toml                       # defines remotes + sync commands
  cache.db                           # optional SQLite FTS for search
```

## Migration Path

1. **Phase 1**: Build `fs_store.py` with claude_code + codex parsers (data on sandbox)
2. **Phase 2**: Add hermes parser (data on M1, rsync sample first)
3. **Phase 3**: Add opencode, gemini, amp, pi_agent, kimi parsers
4. **Phase 4**: Wire `fs_store` as default in `session_store.py`, keep cass_facade as option
5. **Phase 5**: Add lazy SQLite FTS cache for content search
6. **Phase 6**: Add `pj sync` for remote mirror management

## Verification

1. `pj list` returns all projects from all detected agents, instantly (no index needed)
2. `pj search <query>` finds content across all agents
3. `pj status <project>` shows sessions with correct model/agent info
4. `pj resume <project>` generates correct agent-specific resume command
5. New project directories appear immediately (no reindex)
6. Contract tests pass with `fs_store` swapped in via `set_store()`
7. Existing 143 tests continue to pass with `cass_facade` as default

## Deferred: Antigravity (Google)

Google's Antigravity agent lives at `~/.gemini/antigravity/` — separate from the older Gemini CLI (`~/.gemini/tmp/<hash>/chats/`). Conversations are stored as **Protocol Buffer binary** (`.pb` files) in `~/.gemini/antigravity/conversations/<uuid>.pb`. Not parseable without the `.proto` schema definition. Defer until format stabilizes or schema is discoverable (`protoc --decode_raw` can reveal field structure).

Other Antigravity dirs of interest: `brain/` (task planning), `knowledge/`, `context_state/`, `browser_recordings/`.

## Key Decisions

- **FS-first for discovery**: `readdir` + parse JSONL metadata = instant `list`, `status`, `next`, `resume`
- **CASS stays as provider**: `SessionStore` Protocol makes swap mechanical if CASS becomes the richer/fresher source
- **Per-agent parsers**: one module per agent, ~60-120 LOC each, no shared base class (principle #6)
- **Lazy cache for search**: SQLite FTS5 built incrementally, not upfront
- **Multi-host via rsync**: no server component, no daemon, just file sync
- **Auto-detect agents**: scan known dotfile locations, register parsers for what's present
