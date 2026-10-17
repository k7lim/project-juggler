# Engineering Principles

Agent-first prototyping guidance. Primary audience: agents building with you.

Secondary: humans reviewing what agents built.

**Version 2.0** · reviewed 2026-10-13 · next volatile review due 2027-01

**North star**: build for agents and automation. A tool that composes cleanly in a script composes cleanly for an agent; humans get a readable mode on top.

**Mental model**: your tool is a **sensor** (state -> data an agent reads), an **actuator** (action an agent triggers), or both. Know which before designing commands.

**Stability tags.** Every principle is **[E] evergreen**: settled SWE/OPS practice, so change it only on strong evidence. Paragraphs marked **[V]** record current agent-tooling practice. That practice turns over with harness and protocol releases, so re-check it each quarter. Volatile notes collect in [Agent Surface Notes](#agent-surface-notes-v) at the end.

---

## 1. CLI First [E]

CLI is the prototype. No browser required.

```bash
mytool search --query "dune" --type book
mytool post --user alice --title "Dune" --flavor "hooked at page 100"
mytool feed --user alice --limit 10
```

Litmus: if you can't demo it from a terminal, you built UI before product.

Be a well-behaved part of a larger system:
- data goes to stdout, messages and progress to stderr;
- exit codes carry success or failure;
- every interactive prompt has a non-interactive flag;
- no spinners or pagers when stdout is not a terminal.

For server-side systems (APIs, Workers, serverless functions), "CLI first" means providing a CLI client that wraps the service's API. The service is the actuator; the CLI is the agent interface. If you can't `curl` every capability before building a UI, you built the UI before the product.

Operational scripts (deploy, provision, teardown) are actuators. They need the same envelope and `--dry-run` as any other command.

**[V] Output default.** This house defaults to JSON out, with `--pretty` for humans (render as markdown). The wider CLI tradition (clig.dev) defaults to human output and adds `--json`. Either is acceptable if the machine mode is complete and stable. Never make an agent scrape human output.

**[V] CLI and MCP.** The CLI is the core. Add an MCP server as a thin surface over the same core when you need any of these:
- auth kept outside the agent's context (OAuth);
- a remote or team-distributed tool;
- org governance.

Never build MCP-only logic.

---

## 2. One Envelope [E]

One shape for every command, every source, success or failure. Version it, and never break it: additive changes only.

```json
{"success": true, "data": [...], "meta": {"total": 12, "source": "openlibrary", "query": "dune"}}
```

```json
{"success": false, "data": [], "meta": {"error": {"type": "upstream_timeout", "detail": "openlibrary did not answer in 5s", "retryable": true}}}
```

- `meta` carries what the caller needs to branch without a round-trip: counts and pagination, plus a structured error with type, detail and retry guidance. Borrowing RFC 9457 field names for errors is fine.
- `source`, `query` echo, `latency_ms` and `request_id` are optional diagnostics. Include them where they help debugging, not as ceremony.
- `success` must agree with the exit code (CLI) or HTTP status (service). The envelope supplements the transport signal; it never replaces it.

**[V] Spend tokens on `data`.** Agent output is read under a token budget:
- Default to a minimal field set, and let `--fields` expand it.
- Truncate with a size hint (`"truncated": true, "total": 87`).
- Precompute the counts the agent would otherwise compute.
- Put copyable next-step commands in `meta.next` where the next action is predictable.

How the payload is designed matters more than the envelope or the format around it.

---

## 3. Contract Before Code [E]

Write the interface contract before implementing: commands, args, output format, workflows. The man page comes before the program interface. Today that contract is a SKILL.md.

```markdown
---
name: mytool
description: One-line what-it-does and when to use it
---
## Sensors (read, no side effects)
mytool search --query <text> --type <book|film|tv|podcast>
mytool feed --user <id> [--type <type>] [--limit <n>]

## Actuators (mutate, have side effects)
mytool post --user <id> --title <text> --type <type> [--flavor <text>]

## Output: JSON (envelope)

## Workflows
1. Log something: sense (search) -> confirm -> actuate (post)
2. Browse feed: sense (feed) -> present -> actuate on interest
```

Sensor/actuator grouping tells an agent which commands are safe to call speculatively and which have side effects.

The contract forces interface design before implementation. An agent can use the tool the moment the CLI matches the contract.

**[V] Skill practice:**
- **Treat the first SKILL.md as v0.** Rewrite it after an agent has run the real task with it.
- **Keep it thin.** It should say when to use the tool, give the workflows, and point to `--help` / `mytool schema` for flag detail, so the skill can't drift from the CLI (see #24).
- **The description is paid for on every turn**, even when the skill never fires. Keep it to one or two sentences and prune unused skills.
- **Declare required tools and env vars**, and list required auth (never the secrets).

---

## 4. Facade Externals [E]

Wrap every upstream API. Never leak their shapes. When upstream changes, only one file changes, and callers never know.

```
agent -> your facade (your envelope) -> upstream API (their shapes)
```

The facade measures latency per call. Surprises (rate limits, shape changes, downtime) are contained to one file.

This includes platform bindings (R2, S3, GCS, queues, caches). A storage binding is an external dependency with the same risks as any API: vendor lock-in, shape changes, behavioral differences. Even a thin facade (`storage.ts` wrapping `bucket.put`/`bucket.get`) enables testing without the platform and makes migration feasible.

---

## 5. Paginate Every List [E]

`limit`, `offset` in. `total`, `offset`, `limit` out. Day one. No exceptions.

```bash
mytool feed --user alice --limit 10 --offset 20
# {"success": true, "data": [...], "meta": {"total": 87, "offset": 20, "limit": 10}}
```

Agents iterate. Unpaginated = broken at 100 records.

---

## 6. Flat Modules [E]

Each domain = a directory with few files. No inheritance. No shared base classes.

```
books/
  model.py     # data shape
  store.py     # read/write
  commands.py  # CLI entry points
```

Need a second domain? Copy-paste, rename. Extract shared patterns after three domains, not before. Duplication is cheaper than the wrong abstraction.

Flat is not shallow. Each module should hide real complexity behind a small interface. A pile of one-function files moves the complexity into the wiring.

Copies drift when agents edit them: an agent changes one copy and never learns the sibling exists. When you copy, name the siblings in a header comment (`# mirrors films/store.py`). Treat the third copy as the trigger to extract.

---

## 7. Validate Edges, Trust Core [E]

Enums at the boundary. No re-validation inside.

```python
class MediaType(str, Enum):
    BOOK = "book"
    FILM = "film"
    TV = "tv"
    PODCAST = "podcast"

# inside: just use the value
def create_entry(user_id: str, title: str, media_type: MediaType):
    store.insert({"user_id": user_id, "title": title, "type": media_type})
```

Unknown flags, params and enum values fail loudly with a message naming the valid options. Agents hallucinate flags, and a silently ignored flag is the worst outcome: the agent believes it acted.

Validate for security, not just for type. Treat all string inputs as untrusted:
- Reject control characters (ASCII < 0x20), path traversal sequences (`..`) and pre-encoded strings (`%2e%2e`) at the boundary.
- Expect agent inputs to fail differently from human typos: agents embed query syntax in IDs, pre-encode URLs, and confuse path segments.

---

## 8. Privacy in Schema [E]

Visibility, consent and hide-flags belong in the data model, not in app-layer if-statements.

```python
class Entry:
    visibility: str = "private"  # public | followers | private
    hide_rating: bool = False
```

Agents will aggregate and surface data in ways you didn't plan. Schema-level privacy can't be bypassed by a new view.

---

## 9. Append-Only [E]

INSERT, don't UPDATE. "User logged a book" = event. "Reading list" = projection rebuilt from events. Undo = new event.

Agents replay events to build views you haven't imagined. Mutable state requires per-view sync logic.

Not all storage backends support append-only patterns. For mutable stores (object stores, WebDAV, shared buckets):
- use versioning or soft-delete;
- use idempotent writes;
- record mutations for auditability.

In multi-writer systems sharing a datastore, append-only matters even more: it stops one writer from silently overwriting another's data.

For multi-step mutations (copy-then-delete, bulk moves), document the partial-failure state and the recovery strategy. The intermediate state after a crash is the system's actual behavior, so design for it.

---

## 10. No Agent Infrastructure [E]

No agent framework. No registry. No agent-to-agent protocol. No prompts in backend.

You need:
- a CLI returning JSON
- a SKILL.md documenting it
- a stable envelope
- exit codes / HTTP status codes for branching

The agent is the orchestration layer. Your job is good tools, not orchestration. Choose boring technology: the tools your agents touch should be ones models already know.

Litmus: more agent-infra code than product code = overengineered. Delete it.

**[V] Permitted exceptions.** Adopt a framework only when you have outgrown the SDK:
- multi-provider routing;
- multi-step orchestration with human approval gates;
- tracing and observability across runs.

Deterministic hooks are allowed as guardrails. A hook enforces what a prompt only requests. Hooks must be opt-in and visible. A tool must never silently write agent or harness config.

---

## 11. Shared State Contracts [E]

When two or more components share a data store (a bucket, a database, a filesystem), define the shared schema as a versioned contract before writing code. The contract specifies key formats, metadata fields, invariants, and which component is the authority for each data shape.

Without this, coordination logic scatters across services, and you have to read several codebases to understand one use case. The contract is the choreography documentation.

Include a format indicator (e.g. `schema_version = '1'`) in stored data from day one. When the schema evolves, readers can handle old and new formats during migration instead of needing a big-bang rewrite.

- Configuration files mutated by scripts are shared state. Define the format contract before writing several scripts that modify the same file. After each mutation, validate with the consuming tool's parser.
- The contract must state forward-compatibility rules: readers MUST ignore unrecognized fields; writers MUST preserve all existing fields on rewrite, unless the contract explicitly says otherwise.
- The shared schema contract is the system's Canonical Data Model. When adding a new service to the shared store, verify compatibility against the canonical model before writing code.

---

## 12. Secrets Never in Source [E]

Credentials, API keys and tokens always live in environment variables or a secret store. Never hardcode them in source. Rotation must not require code changes or redeployment.

CLI tools should document which env vars they read. SKILL.md should list required auth without including actual secrets.

---

## 13. Characterize Before You Change [E]

Before modifying any external dependency, upstream code or unfamiliar codebase, write characterization tests that document its actual behavior. Not "it should do this": tests that capture what it actually does right now.

These become your safety net. If your changes subtly alter behavior in paths you didn't consider, the characterization tests catch it. This matters most for Principle 4 (Facade Externals): you need to know what the external actually does before you can safely wrap it.

Heuristic: use the code in a test harness. Write an assertion you know will fail. Let the failure tell you what the behavior is. Change the test to expect that behavior. Repeat.

---

## 14. Document the Concurrency Model [E]

When two or more processes write to the same data store, state the isolation level or conflict-resolution strategy explicitly. Enumerate the race conditions you accept.

If your store is last-write-wins with no transactions (like an object store), say so. Name the specific data-loss scenarios: concurrent rename + sync, concurrent delete + read, concurrent creates from different writers. State whether each is an accepted risk or has a mitigation.

"rclone handles it" is not a concurrency model.

---

## 15. Normalize on Write, Accept on Read [E]

When integrating systems with different data representations, the write path should produce only the canonical format. The read path should accept the canonical format and all legacy formats.

This prevents representation variants from multiplying. If you write in both old and new formats at once, you've created a third variant that no other system produces, which makes the situation worse.

---

## 16. Input Hardening [E]

Treat agent and user input identically: as untrusted. Apply the rigor you'd use for a public web API.

| Threat | Mitigation |
|--------|-----------|
| Path traversal (`../../.ssh`) | Canonicalize and sandbox paths |
| Control characters (< ASCII 0x20) | Reject on input |
| Embedded query params in IDs (`fileId?fields=name`) | Reject `?` and `#` in identifiers |
| Double URL encoding (`%2e%2e`) | Reject `%` in resource names; encode at HTTP layer only |

Validate at the CLI/API boundary. Rejecting bad input is cheaper than recovering from a bad downstream call.

---

## 17. Safety Rails [E]

Every actuator (mutating command) supports a preview that validates inputs and shows what would happen without executing. Destructive operations need explicit intent: `--dry-run` for previews, and for dangerous commands consider preview-by-default with `--commit` to execute.

Build dry-run into the architecture, not into scattered `if dry_run:` checks:
- model actions as data;
- execute them in one place;
- let the preview print the plan that the executor would run.

For multi-step or production changes, review the plan before applying it. Confirmation prompts become a formality.

Agents "think out loud" before acting, and the preview output is what they show the user. The cost of a hallucinated parameter is data loss, not just a bad error message.

For shared-state systems:
- **Soft-delete by default.** Hard deletes in multi-writer systems are dangerous, because one client may be reading what another is deleting. Move deleted items to a trash prefix with TTL cleanup.
- **Document the rollback path** for any feature that writes a new data format to a shared store: how to drain or migrate the data if the feature is removed.

**[V] Untrusted content.** Don't rely on sanitizing prompt injection as a primary defense. Classifiers miss payloads that arrive through tool output. Defend structurally instead:
- least privilege;
- separate read and write credentials;
- sandboxed execution;
- untrusted tool output labeled as data.

---

## 18. Single-Goal Edits [E]

Each commit or patch should make exactly one behavioral change. If you need to change a key format AND add metadata, those are two separate changes, each independently verifiable and independently reversible.

This prevents "thrashing", where a bug could be in either change and you can't isolate which. It also keeps code review tractable and rollback safe.

---

## 19. Edit-Test Cadence [E]

When modifying existing code, run the full test suite after each behavioral change, not just at the end. Document expected test counts at each stage. The edit-test loop should take minutes, not hours.

---

## 20. Tenant Isolation [E]

When serving multiple tenants from the same codebase, enforce data isolation at the infrastructure level (separate stores, credentials, bindings), not in application logic. Document the isolation guarantee and what happens if the deployment topology changes.

---

## 21. Staged Deployment [E]

When a shared codebase serves multiple tenants, deploy to the operator's own tenant first, verify, then roll out to the other tenants. Never deploy to all tenants at once without a verification gate.

---

## 22. Scaling Thresholds [E]

Document the scaling threshold for your current architecture. State the metric (tenant count, request rate, data volume) and the value at which the current approach breaks. Name the migration path.

---

## 23. API Boundary Discipline [E]

Task requires changing a public interface, adding params, extending a type, or reaching through an abstraction? **Stop. Describe the tension. Wait for approval.**

- Implement within existing boundaries first
- No new methods/fields/params on public interfaces without asking
- No internal access to bypass an abstraction that doesn't fit
- No escape hatches: optional any-typed params, overly broad generics, loosened constraints
- No "thin wrappers" that just re-expose internals differently
- If the abstraction is wrong for the task, say so; don't patch around it

Models default to treating boundaries as obstacles. The failure compounds:
1. The boundary doesn't quite fit, so the agent adds a small hole.
2. That repeats across tasks.
3. The abstraction becomes leaky, inconsistent, and load-bearing in conflicting directions.
4. The next refactor hits a wall of accumulated damage.

Instead: "This task needs X but the current interface only exposes Y. Options: (a) extend interface to add X, (b) rethink the boundary, (c) redesign the approach to only need Y. Which direction?"

---

## 24. One Source for the Interface [E]

Define each command once, as a schema (name, args, types, defaults, side-effect class, examples). Generate everything else from it:
- `--help`;
- the SKILL.md command list;
- MCP tool definitions;
- docs and test stubs.

Expose it at runtime (`mytool schema [command]`), so agents can introspect instead of guessing from prose.

Parallel hand-written representations drift. A CLI with no machine-readable schema makes agents parse `--help`, and that is where they hallucinate. Which artifact is the source matters less than having exactly one; generator quality is the usual trap, so test the generated output.

---

## 25. Move Files by Reference [E]

Never route file bytes through model tokens (base64 in arguments, pasted content). Pass paths, upload handles or URLs, and return a digest (size + hash) so the receiver can verify. Large outputs go to a file, and the command returns its path and a summary.

Model-relayed bytes corrupt silently: every layer reports success while the payload is wrong.

---

## Path to Prototype 1

Ordered: do the top first.

1. **SKILL.md v0**: design the interface
2. **Command schema**: one definition per command (#24)
3. **CLI**: implement commands matching the contract
4. **Facades**: one per upstream, returning the envelope
5. **Characterization tests**: tests against each facade that document actual upstream behavior
6. **Store**: flat modules, append-only, paginated reads
7. **Edge validation**: enums/schemas at the boundary, input hardening at the boundary, loud failure on unknown flags
8. **Agent test**: hand SKILL.md to an agent and see whether it can use the tool end to end. Run the same task without the skill too: the skill must earn its per-turn token cost. Then rewrite SKILL.md from what the agent actually did.

Done when: the agent reads SKILL.md, calls the CLI, parses the output, and chains commands into a workflow. Ship. UI, auth, deploy and scale come after that proof.

---

## Quick Reference

| # | Principle | Rule | Tag |
|---|-----------|------|-----|
| 1 | CLI First | terminal before browser; stdout data, stderr messages; MCP is a thin surface over the CLI core | E, V notes |
| 2 | One Envelope | one versioned shape, agrees with exit/HTTP status; spend tokens on `data` | E, V notes |
| 3 | Contract Before Code | contract before code; SKILL.md v0, thin, rewritten after first real run | E, V notes |
| 4 | Facade Externals | wrap upstream + platform bindings, never leak shapes | E |
| 5 | Paginate Always | limit/offset/total day one | E |
| 6 | Flat Modules | copy-paste > abstraction until 3+ domains; flat ≠ shallow; name siblings | E |
| 7 | Edge Validation | validate boundary, fail loudly on unknown input, trust inside | E |
| 8 | Privacy in Schema | data model enforces, not app | E |
| 9 | Append-Only | events > mutations; for mutable stores: version, soft-delete, audit | E |
| 10 | No Agent Infra | good CLI + envelope > agent framework; opt-in hooks as guardrails | E, V notes |
| 11 | Shared State Contracts | shared store = versioned schema contract before code | E |
| 12 | Secrets Never in Source | env vars or secret stores; rotation without redeploy | E |
| 13 | Characterize Before You Change | tests documenting actual behavior before modifying upstream | E |
| 14 | Document the Concurrency Model | state isolation level, enumerate accepted races | E |
| 15 | Normalize on Write, Accept on Read | write canonical only; read all legacy formats | E |
| 16 | Input Hardening | reject traversals, control chars, pre-encoded strings at boundary | E |
| 17 | Safety Rails | preview for actuators, built into the architecture; soft-delete; least privilege over sanitizing | E, V notes |
| 18 | Single-Goal Edits | one behavioral change per commit, independently reversible | E |
| 19 | Edit-Test Cadence | run full test suite after each behavioral change, not just at end | E |
| 20 | Tenant Isolation | enforce data isolation at infrastructure level, not application logic | E |
| 21 | Staged Deployment | deploy to operator's tenant first, verify, then roll to others | E |
| 22 | Scaling Thresholds | document the metric and value where current architecture breaks | E |
| 23 | API Boundary Discipline | implement within existing boundaries; stop and ask before changing public interfaces | E |
| 24 | One Source for the Interface | one command schema generates help, skill, MCP defs, docs; `schema` introspection | E |
| 25 | Move Files by Reference | paths/handles + digests, never bytes through model tokens | E |

---

## Agent Surface Notes [V]

Current practice as of **2026-10-13**. These track agent harness and protocol releases. Re-check them by **2027-01** and re-date this section when you do. Evidence: `research/agent-native-engineering/social-proof-2026-10-13/REPORT.md`.

- **CLI vs MCP is settled as layered, not either/or.** The March 2026 "MCP is dead" wave was walked back after the stateless MCP spec (2026-07-28). Use a CLI core for tools models already know, and MCP for auth isolation, remote/team distribution and governance.
- **Token arguments age fastest.** Tool search, prompt caching and codemode keep shrinking the token cost of MCP. Re-measure before citing a number, and prefer the mechanism (lazy loading, minimal fields) over the figure.
- **Compact formats (TOON etc.):** not canonical.
  - Independent measurements found 3–7% end-to-end savings and lower extraction accuracy.
  - Acceptable only as an opt-in read-side format for uniform tabular output.
  - Never use one for anything the agent must write back.
  - Keep JSON as the contract.
- **AXI-style ideas worth keeping:** minimal default fields, precomputed counts, truncation hints, next-step hints. They are now folded into #2. Its session hooks are not kept (see #10).
- **Skills:**
  - Measure lift (with vs without the skill).
  - Declare dependencies.
  - Watch the per-turn description cost.
  - Treat third-party skills as supply chain: review them before installing.
- **Capability gradient (experimental):** hide destructive flags from default `--help`, and reveal them only in the safe preview's output.

---

## Changelog

- **2.0, 2026-10-13**:
  - Added stability tags (E/V), the Agent Surface Notes, #24 One Source for the Interface, and #25 Move Files by Reference.
  - #1 split the output default and CLI/MCP layering into [V] notes.
  - #2 made the `meta` diagnostics optional, required agreement with exit/HTTP status, added a structured error, and added output-budget guidance.
  - #3 retitled to Contract Before Code, with skill practice as [V].
  - #6 added flat ≠ shallow and sibling naming.
  - #7 added loud failure on unknown flags.
  - #10 added framework exceptions and the opt-in hooks rule.
  - #17 added preview-by-default, dry-run as architecture, plan review, and structural injection defense.
  - Path to Prototype gained the schema step and the skill-lift test.
  - Basis: a 7-lane multilingual practitioner review (155 cards).
- **1.x, ≤ 2026-08-04**: the 23 principles as previously published.

---

## Project-specific

Local additions carried over from this project's fork of v1. `#N` references point to the v2 principles above unless a note says otherwise. Lines marked **Conflict with v2** keep the local text where it disagrees with v2; reconcile them deliberately.

### #7 Validate Edges, Trust Core: postconditions

Postconditions too, not just preconditions. An actuator confirms its effect landed before returning success — catches upstreams that return 200 without completing.

### #14 Document the Concurrency Model: write skew

Write skew: two writers modify different keys off the same read, both commit, invariant breaks. Name the read-modify-write cycles.

### 24. Idempotent Processing

> **Conflict with v2:** v2 numbers its own #24 (One Source for the Interface). Local number kept; renumber before citing "#24" in this project.

Every consumer must be replay-safe. Detect retried or duplicated inputs via a stable key (message hash, correlation id, content address) and skip re-processing.

Reader-side complement to #9 (Append-Only): producers write events; consumers must tolerate seeing an event twice. Mandatory wherever there are retries, webhook fan-out, scheduled re-runs, or at-least-once delivery.

Pick the dedup key at production time, not consumption time. Deduping on arrival timestamp or order breaks under retry.

### 25. Human-Facing Copy Discipline

> **Conflict with v2:** v2 numbers its own #25 (Move Files by Reference). Local number kept; renumber before citing "#25" in this project.

Human-facing text is a contract too. Error messages, PR comments, notifications, onboarding — self-evident in one sentence, omit needless words, primary action first, no happy talk.

#1 optimizes the machine interface; this optimizes what's left. A clean CLI with vague copy is a failed product.

### 26. Progressive Disclosure

Surface the primary path; gate detail behind explicit calls to action. Docs, help output, and the machine contract (SKILL.md) should be legible at two scales: the top answers 80% of uses; references load on demand.

Operationalizes #3 at a finer grain. The skill is the index; references are the chapters.

### Quick Reference additions

> **Conflict with v2:** the local row 7 replaces v2's row 7 rule ("validate boundary, fail loudly on unknown input, trust inside"). Local text kept.

| # | Principle | Rule |
|---|-----------|------|
| 7 | Edge Validation | validate preconditions + postconditions at boundary, trust inside |
| 24 | Idempotent Processing | consumers detect retried/duplicated inputs via stable key and skip re-processing |
| 25 | Human-Facing Copy Discipline | self-evident, action-first text; treat human copy with API-contract rigor |
| 26 | Progressive Disclosure | primary path up top; detail in references loaded on demand |
