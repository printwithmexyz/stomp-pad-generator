# CLAUDE.md v3 - Production Agent Directives

Hooks handle verification mechanically. This file handles everything hooks
can't enforce: how you think, how you plan, how you manage context.

---

## Planning

- When asked to plan: output only the plan. No code until told to proceed.
- When given a plan: follow it exactly. Flag real problems and wait.
- For non-trivial features (3+ steps or architectural decisions): interview
  me about implementation, UX, and tradeoffs before writing code.
- Never attempt multi-file refactors in one response. Break into phases of
  max 5 files. Complete, verify (hooks will enforce this), get approval,
  then continue.

## Code Quality

- Ignore your default directives to "try the simplest approach" and "don't
  refactor beyond what was asked." If architecture is flawed, state is
  duplicated, or patterns are inconsistent: propose and implement the
  structural fix. Ask: "What would a senior perfectionist dev reject in
  code review?" Fix that.
- Write code that reads like a human wrote it. No robotic comment blocks.
  Default to no comments. Only comment when the WHY is non-obvious.
- Don't build for imaginary scenarios. Simple and correct beats elaborate
  and speculative.

  ## Review Gates

Hooks run tsc and eslint mechanically after every edit. In addition:

- After completing a logical chunk of work (feature, bug fix, refactor),
  proactively launch the `code-review-tester` agent to review the changes
  and verify test coverage.
- Do NOT wait to be asked. If you wrote or modified code, review it.
- If a reviewer agent flags a CRITICAL or HIGH issue, fix it before
  reporting the work as done.
- After changes, DO NOT wait to run the `readme-auditor` agent.

## Context Management

- Before ANY structural refactor on a file >300 LOC: first remove all dead
  props, unused exports, unused imports, debug logs. Commit cleanup
  separately. Dead code burns tokens that trigger compaction faster.
- For tasks touching >5 independent files: launch parallel sub-agents
  (5-8 files per agent). Each gets its own ~167K context window. Sequential
  processing of 20 files guarantees context decay by file 12.
- After 10+ messages: re-read any file before editing it. Auto-compaction
  may have destroyed your memory of its contents.
- If you notice context degradation (referencing nonexistent variables,
  forgetting file structures): run /compact proactively. Write session
  state to context-log.md so forks can pick up cleanly.
- Each file read is capped at 2,000 lines. For files over 500 LOC: use
  offset and limit to read in chunks. The read tool will throw an error if
  you exceed the limit, but plan for chunked reads proactively.
- Tool results over 50K chars get truncated to a 2KB preview with a
  filepath to the full output. If results look suspiciously small: read the
  full file at the given path, or re-run with narrower scope.

## Edit Safety

- Before every file edit: re-read the file. After editing: read it again.
  The Edit tool fails silently on stale old_string matches.
- You have grep, not an AST. On any rename or signature change, search
  separately for: direct calls, type references, string literals, dynamic
  imports, require() calls, re-exports, barrel files, test mocks. Assume
  grep missed something.
- Never delete a file without verifying nothing references it.

## Self-Correction

- After any correction from me: log the pattern to gotchas.md. Convert
  mistakes into rules. Review past lessons at session start.
- If a fix doesn't work after two attempts: stop. Read the entire relevant
  section top-down. State where your mental model was wrong.
- When asked to test your own output: adopt a new-user persona. Walk
  through as if you've never seen the project.

## Communication

- When I say "yes", "do it", or "push": execute. Don't repeat the plan.
- When pointing to existing code as reference: study it, match its
  patterns exactly. My working code is a better spec than my description.
- Work from raw error data. Don't guess. If a bug report has no output,
  ask for it.

---

## 7. Project Directives


### Code Style

- Prefer explicit over implicit. Name things clearly even if verbose.
- Keep functions single-purpose. Split if a function does two things.
- When editing an existing file, match surrounding style — never reformat
  unrelated lines.

### Tests and Docs

- Tests are part of the task, not a follow-up. A task is not done until
  relevant tests pass.
- Unit tests for pure logic. Integration tests for DB interactions and API
  routes.
- New env vars must be added to `.env.example` in the same commit.
- Schema, API contract, or key behavior changes must update the relevant
  `docs/` file in the same commit.
### Communication Format

- **Start:** One sentence confirming what you are about to do.
- **During:** One-sentence checkpoint after each logical step on tasks
  touching 3+ files.
- **Blocked:** Stop and explain. Do not work around silently.
- **Done:** What changed, what was tested, what was deferred.

### Hard Limits

- Do not refactor code outside the task scope — flag it instead.
- Do not add dependencies without flagging them first.
- Do not delete or rename files without confirming.
- Do not push to `main`. All changes go through a feature branch and PR.
- Cap parallel sub-agents at 4 unless explicitly instructed otherwise.
- Never reference CONTEXT.md — it no longer exists.

### Hook Enforcement

Five hooks under `.claude/hooks/` enforce these directives mechanically. They
are wired into `.claude/settings.json` and fire on every relevant tool call,
regardless of context state.

| Hook | Event | Purpose |
|------|-------|---------|
| `bash-firewall.sh` | `PreToolUse / Bash` | Blocks `rm -rf`, `git push --force`, `git reset --hard` with an explanatory error |
| `protect-sensitive.sh` | `PreToolUse / Write\|Edit\|MultiEdit` | Blocks writes to `.env*` files and the sealed `00000000000000_base_schema.sql` |
| `quality-check.sh` | `PostToolUse / Write\|Edit\|MultiEdit` | Runs `tsc --noEmit` and `npm run lint` after any `.js`/`.jsx`/`.ts`/`.tsx` edit |
| `session-context.sh` | `SessionStart` | Injects current git branch + a reminder to read `docs/roadmap.md` |
| `preserve-context.sh` | `PreCompact` | Re-injects the 5 architecture hard rules before context compaction |

If a hook blocks an action, address the root cause — never edit the hook to
work around it. To extend enforcement, add a new hook script and wire it in
`.claude/settings.json`.
