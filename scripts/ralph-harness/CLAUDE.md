# Ralph Agent Instructions — Creel Test Harness

You are building an integration test harness for Creel, an AI agent framework.

## Your Task

1. Read the PRD at `scripts/ralph-harness/prd.json`
2. Read the progress log at `scripts/ralph-harness/progress.txt` (Codebase Patterns first)
3. **Read the full spec at `specs/test-harness.md`** for detailed design
4. Check you're on branch `ralph/test-harness`. If not, check it out.
5. Pick the highest priority story where `passes: false`
6. Implement that single user story
7. Run quality checks
8. Commit: `feat: [Story ID] - [Story Title]`
9. Update PRD `passes: true`, append to progress.txt

## Project Context

- **Repo root:** `/Users/ross/.openclaw/workspace/projects/creel`
- **Full spec:** `specs/test-harness.md`
- **Source:** `src/taskrunner/` (the agent framework)
- **Existing tests:** `tests/` (108 files, 1700+ tests)
- **New harness code goes in:** `scripts/test-harness/`

## Quality Checks

```bash
# Existing tests must still pass
cd /Users/ross/.openclaw/workspace/projects/creel && uv run pytest tests/ -x -q 2>&1 | tail -20

# After HARNESS-002+: the harness script should run without crashing
# (scenarios may fail until implemented, but infrastructure should work)
```

## Key Design Points

- **Mock LLM server** is the foundation — FastAPI mimicking OpenAI's /v1/chat/completions
- **CREEL_HOME env var** points the daemon at the test config directory
- **No Docker required** — test tools use executor: host
- **No real API keys** — everything uses mock endpoints
- **Scripted responses** from fixtures/llm_triggers.json enable deterministic testing
- **httpx** for test HTTP calls to the daemon
- **pytest** for test scenarios
- Keep the mock server minimal — just enough to test the pipeline

## Important

- Read specs/test-harness.md before each story
- ONE story per iteration
- Commit after each
- Don't break existing tests
- The harness tests are SEPARATE from existing tests — they live in scripts/test-harness/scenarios/

## Progress Report

APPEND to scripts/ralph-harness/progress.txt:
```
## [Date/Time] - [Story ID]
- What was implemented
- Files changed
- **Learnings:**
---
```

## Stop Condition
All `passes: true` → `<promise>COMPLETE</promise>`
Otherwise end normally.
