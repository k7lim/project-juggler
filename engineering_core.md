# Engineering Principles

Agent-first prototyping guidance. Primary audience: agents building with you.

Secondary: humans reviewing what agents built.

**North star**: build for agents. Humans benefit from good machine ergonomics as side effect.

**Mental model**: your tool is a **sensor** (state -> data an agent reads), an **actuator** (action an agent triggers), or both. Know which before designing commands.

---

## 1. CLI First

CLI is the prototype. JSON out. `--pretty` for humans (render as markdown). No browser required.

```bash

mytool search --query "dune" --type book

mytool post --user alice --title "Dune" --flavor "hooked at page 100"

mytool feed --user alice --limit 10

```

Litmus: if you can't demo it from a terminal, you built UI before product.

For server-side systems (APIs, Workers, serverless functions): "CLI first" means provide a CLI client that wraps the service's API. The service is the actuator; the CLI is the agent interface. If you can't `curl` every capability before building a UI, you built the UI before the product.

Operational scripts (deploy, provision, teardown) are actuators — they need the same JSON envelope and `--dry-run` as any other command.

---

## 2. One Envelope

Never deviate. Agent parses one shape for every command, every source, success or failure.

```json

{"success": true, "data": [...], "meta": {"source": "openlibrary", "query": "dune", "total": 12, "latency_ms": 340}}

```

```json

{"success": false, "data": [], "meta": {"source": "openlibrary", "query": "dune", "error": "upstream timeout", "latency_ms": 5000}}

```

`meta` carries source, query echo, count, latency. Caller decides next step without round-trip.

---

## 3. SKILL.md Before Code

Machine-readable contract. Commands, args, output format, workflows. Write before implementing.

```markdown

---

name: mytool

description: One-line what-it-does

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

Sensor/actuator grouping tells an agent which commands are safe to call speculatively vs which have side effects.

Forces interface design before implementation. Agent can use the tool the moment CLI matches contract.

---

## 4. Facade Externals

Wrap every upstream API. Never leak their shapes. One file changes when upstream changes. Callers never know.

```

agent -> your facade (your envelope) -> upstream API (their shapes)

```

Facade measures latency per call. Surprises (rate limits, shape changes, downtime) contained to one file.

This includes platform bindings (R2, S3, GCS, queues, caches). A storage binding is an external dependency with the same risks as any API: vendor lock-in, shape changes, behavioral differences. Even a thin facade (`storage.ts` wrapping `bucket.put`/`bucket.get`) enables testing without the platform and makes migration feasible.

---

## 5. Paginate Every List

`limit`, `offset` in. `total`, `offset`, `limit` out. Day one. No exceptions.

```bash

mytool feed --user alice --limit 10 --offset 20

# {"success": true, "data": [...], "meta": {"total": 87, "offset": 20, "limit": 10}}

```

Agents iterate. Unpaginated = broken at 100 records.

---

## 6. Flat Modules

Each domain = directory with few files. No inheritance. No shared base classes.

```

books/

model.py # data shape

store.py # read/write

commands.py # CLI entry points

```

Need a second domain? Copy-paste, rename. Extract shared patterns after three domains, not before.

---

## 7. Validate Edges, Trust Core

Enums at boundary. No re-validation inside.

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

Security validation, not just type validation: treat all string inputs as untrusted. Reject control characters (ASCII < 0x20), path traversal sequences (`..`), and pre-encoded strings (`%2e%2e`) at the boundary. Agent-generated inputs hallucinate differently than human typos — they embed query syntax in IDs, pre-encode URLs, and confuse path segments.

Postconditions too, not just preconditions. An actuator confirms its effect landed before returning success — catches upstreams that return 200 without completing.

---

## 8. Privacy in Schema

Visibility, consent, hide-flags in the data model. Not in app-layer if-statements.

```python

class Entry:

visibility: str = "private" # public | followers | private

hide_rating: bool = False

```

Agents will aggregate and surface data in ways you didn't plan. Schema-level privacy can't be bypassed by a new view.

---

## 9. Append-Only

INSERT, don't UPDATE. "User logged a book" = event. "Reading list" = projection rebuilt from events. Undo = new event.

Agents replay events to build views you haven't imagined. Mutable state requires per-view sync logic.

Not all storage backends support append-only patterns. For mutable stores (object stores, WebDAV, shared buckets): use versioning or soft-delete, use idempotent writes, and record mutations for auditability. In multi-writer systems sharing a datastore, append-only is even more critical — it prevents one writer from silently corrupting another's data by overwriting it.

For multi-step mutations (copy-then-delete, bulk moves), document the partial-failure state and recovery strategy. The intermediate state after a crash is the system's actual behavior — design for it.

---

## 10. No Agent Infrastructure

No agent framework. No registry. No agent-to-agent protocol. No prompts in backend.

You need:

- CLI returning JSON

- SKILL.md documenting it

- stable envelope

- exit codes / HTTP status codes for branching

Agent is the orchestration layer. Your job: good tools, not orchestration.

Litmus: more agent-infra code than product code = overengineered. Delete it.

---

## 11. Shared State Contracts

When two or more components share a data store (a bucket, a database, a filesystem), define the shared schema as a versioned contract before writing code. The contract specifies key formats, metadata fields, invariants, and which component is the authority for each data shape.

Without this, coordination logic scatters across services and you need to read multiple codebases to understand one use case. The contract is the choreography documentation.

Include a format indicator (e.g., `schema_version = '1'`) in stored data from day one. When the schema evolves, readers can handle both old and new formats during migration instead of requiring a big-bang rewrite.

- Configuration files mutated by scripts are shared state. Define the format contract before writing multiple scripts that modify the same file. Validate with the consuming tool's parser after each mutation.

- The contract must state forward-compatibility rules: readers MUST ignore unrecognized fields; writers MUST preserve all existing fields on re-write, unless the contract explicitly states otherwise.

- The shared schema contract is the system's Canonical Data Model. When adding a new service to the shared store, verify compatibility against the canonical model before writing code.

---

## 12. Secrets Never in Source

Credentials, API keys, and tokens are always environment variables or secret store values. Never hardcode in source. Rotation must not require code changes or redeployment.

CLI tools should document which env vars they read. SKILL.md should list required auth without including actual secrets.

---

## 13. Characterize Before You Change

Before modifying any external dependency, upstream code, or unfamiliar codebase, write characterization tests that document its actual behavior. Not "it should do this" — tests that capture what it actually does right now.

These become your safety net. If your changes subtly alter behavior in paths you didn't consider, characterization tests catch it. This is especially important for Principle 4 (Facade Externals): you need to know what the external actually does before you can safely wrap it.

Heuristic: use the code in a test harness. Write an assertion you know will fail. Let the failure tell you what the behavior is. Change the test to expect that behavior. Repeat.

---

## 14. Document the Concurrency Model

When two or more processes write to the same data store, state the isolation level or conflict-resolution strategy explicitly. Enumerate the race conditions you accept.

If your store is last-write-wins with no transactions (like an object store), say so. Name the specific data-loss scenarios: concurrent rename + sync, concurrent delete + read, concurrent creates from different writers. State whether each is an accepted risk or has a mitigation.

Write skew: two writers modify different keys off the same read, both commit, invariant breaks. Name the read-modify-write cycles.

"rclone handles it" is not a concurrency model.

---

## 15. Normalize on Write, Accept on Read

When integrating systems with different data representations, the write path should produce only the canonical format. The read path should accept both canonical and all legacy formats.

This prevents proliferation of representation variants. If you write in both old and new format simultaneously, you've created a third variant that no other system produces — making the situation worse, not better.

---

## 16. Input Hardening

Treat agent and user input identically: as untrusted. Apply the same rigor you'd use for a public web API.

| Threat | Mitigation |

|--------|-----------|

| Path traversal (`../../.ssh`) | Canonicalize and sandbox paths |

| Control characters (< ASCII 0x20) | Reject on input |

| Embedded query params in IDs (`fileId?fields=name`) | Reject `?` and `#` in identifiers |

| Double URL encoding (`%2e%2e`) | Reject `%` in resource names; encode at HTTP layer only |

Validate at the CLI/API boundary. It's cheaper to reject bad input than recover from a bad downstream call.

---

## 17. Safety Rails

Every actuator (mutating command) should support `--dry-run` that validates inputs and shows what would happen without executing. Destructive operations require explicit confirmation.

Agents "think out loud" before acting — `--dry-run` output is what they show the user. The cost of a hallucinated parameter is data loss, not just a bad error message.

For shared-state systems: soft-delete by default. Hard deletes in multi-writer systems are dangerous because one client may be reading what another is deleting. Move to a trash prefix with TTL cleanup.

For any feature that writes a new data format to a shared store, document the rollback path: how to drain or migrate the data if the feature is removed.

---

## 18. Single-Goal Edits

Each commit or patch should accomplish exactly one behavioral change. If you need to change a key format AND add metadata, those are two separate changes — independently verifiable and independently reversible.

This prevents "thrashing" where a bug could be in either change and you can't isolate which. It also makes code review tractable and rollback safe.

---

## 19. Edit-Test Cadence

When modifying existing code, run the full test suite after each behavioral change, not just at the end. Document expected test counts at each stage. The edit-test loop should be minutes, not hours.

---

## 20. Tenant Isolation

When serving multiple tenants from the same codebase, enforce data isolation at the infrastructure level (separate stores, credentials, bindings), not application logic. Document the isolation guarantee and what happens if the deployment topology changes.

---

## 21. Staged Deployment

When a shared codebase serves multiple tenants, deploy to the operator's own tenant first, verify, then roll to other tenants. Never deploy simultaneously to all tenants without a verification gate.

---

## 22. Scaling Thresholds

Document the scaling threshold for your current architecture. State the metric (tenant count, request rate, data volume) and the value at which the current approach breaks. Name the migration path.

---

## 23. API Boundary Discipline

Task requires changing a public interface, adding params, extending a type, or reaching through an abstraction? **Stop. Describe the tension. Wait for approval.**

- Implement within existing boundaries first
- No new methods/fields/params on public interfaces without asking
- No internal access to bypass an abstraction that doesn't fit
- No escape hatches: optional any-typed params, overly broad generics, loosened constraints
- No "thin wrappers" that just re-expose internals differently
- If the abstraction is wrong for the task, say so — don't patch around it

Models default to treating boundaries as obstacles. The compounding failure mode: boundary doesn't quite fit → agent adds a small hole → repeat across tasks → abstraction is now leaky, inconsistent, and load-bearing in conflicting directions → next refactor hits a wall of accumulated damage.

Instead: "This task needs X but the current interface only exposes Y. Options: (a) extend interface to add X, (b) rethink the boundary, (c) redesign the approach to only need Y. Which direction?"

---

## 24. Idempotent Processing

Every consumer must be replay-safe. Detect retried or duplicated inputs via a stable key (message hash, correlation id, content address) and skip re-processing.

Reader-side complement to #9 (Append-Only): producers write events; consumers must tolerate seeing an event twice. Mandatory wherever there are retries, webhook fan-out, scheduled re-runs, or at-least-once delivery.

Pick the dedup key at production time, not consumption time. Deduping on arrival timestamp or order breaks under retry.

---

## 25. Human-Facing Copy Discipline

Human-facing text is a contract too. Error messages, PR comments, notifications, onboarding — self-evident in one sentence, omit needless words, primary action first, no happy talk.

#1 optimizes the machine interface; this optimizes what's left. A clean CLI with vague copy is a failed product.

---

## 26. Progressive Disclosure

Surface the primary path; gate detail behind explicit calls to action. Docs, help output, and the machine contract (SKILL.md) should be legible at two scales: the top answers 80% of uses; references load on demand.

Operationalizes #3 at a finer grain. The skill is the index; references are the chapters.

---

## Path to Prototype 1

Ordered — do top first:

1. **SKILL.md** — design interface

2. **CLI** — implement commands matching contract

3. **Facades** — one per upstream, returning envelope

4. **Characterization tests** — write tests against each facade that document actual upstream behavior

5. **Store** — flat modules, append-only, paginated reads

6. **Edge validation** — enums/schemas at boundary, input hardening at boundary

7. **Agent test** — hand SKILL.md to Claude, see if it can use tool end-to-end

Done when: agent reads SKILL.md, calls CLI, parses output, chains commands into workflow. Ship. UI/auth/deploy/scale come after that proof.

---

## Quick Reference

| # | Principle | Rule |

|---|-----------|------|

| 1 | CLI First | terminal before browser, JSON before HTML; for services, provide a CLI client |

| 2 | One Envelope | same shape every response every source |

| 3 | SKILL.md First | machine contract before code |

| 4 | Facade Externals | wrap upstream + platform bindings, never leak shapes |

| 5 | Paginate Always | limit/offset/total day one |

| 6 | Flat Modules | copy-paste > abstraction until 3+ domains |

| 7 | Edge Validation | validate preconditions + postconditions at boundary, trust inside |

| 8 | Privacy in Schema | data model enforces, not app |

| 9 | Append-Only | events > mutations; for mutable stores: version, soft-delete, audit |

| 10 | No Agent Infra | good CLI + envelope > agent framework |

| 11 | Shared State Contracts | shared store = versioned schema contract before code |

| 12 | Secrets Never in Source | env vars or secret stores; rotation without redeploy |

| 13 | Characterize Before You Change | tests documenting actual behavior before modifying upstream |

| 14 | Document the Concurrency Model | state isolation level, enumerate accepted races |

| 15 | Normalize on Write, Accept on Read | write canonical only; read all legacy formats |

| 16 | Input Hardening | reject traversals, control chars, pre-encoded strings at boundary |

| 17 | Safety Rails | `--dry-run` for actuators; soft-delete in multi-writer systems |

| 18 | Single-Goal Edits | one behavioral change per commit, independently reversible |

| 19 | Edit-Test Cadence | run full test suite after each behavioral change, not just at end |

| 20 | Tenant Isolation | enforce data isolation at infrastructure level, not application logic |

| 21 | Staged Deployment | deploy to operator's tenant first, verify, then roll to others |

| 22 | Scaling Thresholds | document the metric and value where current architecture breaks |

| 23 | API Boundary Discipline | implement within existing boundaries; stop and ask before changing public interfaces |

| 24 | Idempotent Processing | consumers detect retried/duplicated inputs via stable key and skip re-processing |

| 25 | Human-Facing Copy Discipline | self-evident, action-first text; treat human copy with API-contract rigor |

| 26 | Progressive Disclosure | primary path up top; detail in references loaded on demand |
