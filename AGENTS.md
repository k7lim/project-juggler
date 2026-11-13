# Agent Instructions

This project uses **bd** (beads) for issue tracking. Run `bd prime` once at the
start of a session for workflow context. Your host already loads this file, so
do not also `cat AGENTS.md` or run `bd onboard`.

## Session completion

These rules replace the push steps in the managed block below.

1. File beads for remaining work, run tests if code changed, and close or update
   the beads you worked on.
2. If issue data changed, check `bd list`, then run
   `bd export -o .beads/issues.jsonl` and commit it. bd 1.0.0 has no `bd sync`,
   and this repo has no Dolt remote, so skip `bd dolt push`.
3. Commit locally. **Ask the user before `git push`.** Work is complete once it
   is committed and the user has been told what is unpushed.
4. Do not commit files that were already dirty when the session started unless
   the user asks.


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->
