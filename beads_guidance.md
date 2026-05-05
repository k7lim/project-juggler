# Agent Instructions

bd is source of truth for tasks. Run `bd onboard` to get started.

## Workflow

```bash
# Start
bd prime                        # Load context (if available)
bd ready --json                 # Find available work

# During
bd update <id> --status in_progress --json    # Claim
bd create "Title" -p 1 -t task --deps discovered-from:<id> --json  # New discovery
bd dep add <child> <parent> --json            # Add blocker

# Finish
bd close <id> --reason "Summary" --json       # Complete
bd sync                                        # Sync beads with git
git add -A && git commit -m "Summary of changes"  # Always commit locally
```

## Git Sync

After committing locally, handle the remote:

1. **Remote exists and is tracking**: `git pull --rebase && git push`
2. **Remote exists but branch doesn't track**: `git push -u origin <branch>`
3. **No remote configured**: Offer to set one up (`git remote add origin <url>`) — don't block on it

Work is complete once changes are **committed locally** and **bd sync** has run. Pushing is best-effort: always push if a remote is available, but a missing remote should never prevent you from finishing work.

## Planning Docs + Beads

- Detailed plans → `docs/plans/` (beads are lightweight for tracking, not specs)
- Reference plans from beads: `bd create "Task" --desc "See docs/plans/foo.md#section"`
- Group with epics: `bd create "Epic" --type epic` then `bd create "Task" --parent <epic-id>`
- Philosophy: beads track **what** + **dependencies**, docs capture **how** + **why**

## Rules

- Always `--json` for machine output
- Always double-quote titles/descriptions
- No `bd edit` (human-only); use `bd update` flags
- If daemon unsafe (sandbox/CI/worktrees): `bd --sandbox` or `bd --no-daemon`
- Always commit locally — never leave work uncommitted
- Push to remote when one exists; offer to set up remote if missing
