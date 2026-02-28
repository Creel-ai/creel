# Ralph Agent Instructions — Creel Test Harness

You are an autonomous coding agent building an end-to-end test harness for Creel, an AI agent framework with a daemon, dashboard, and API.

## Your Task

1. Read the PRD at `scripts/ralph-harness/prd.json`
2. Read the progress log at `scripts/ralph-harness/progress.txt` (check Codebase Patterns first)
3. Check you're on the correct branch from PRD `branchName`. If not, create it from `main`, then merge `ralph/dashboard` into it (dashboard source is needed for e2e tests).
4. Pick the **highest priority** user story where `passes: false`
5. Implement that single user story
6. Run quality checks (see below)
7. If checks pass, commit ALL changes with message: `feat: [Story ID] - [Story Title]`
8. Update the PRD to set `passes: true` for the completed story
9. Append your progress to `scripts/ralph-harness/progress.txt`

## Project Context

- **Repo root:** `/Users/ross/.openclaw/workspace/projects/creel`
- **Package:** `src/taskrunner/` (the Python package — will be renamed to `creel` later)
- **Daemon:** `src/taskrunner/daemon/` — FastAPI server (service.py, api.py, contracts.py)
- **Dashboard source:** `dashboard/` (React + MUI v6 + TypeScript + Vite) — on `ralph/dashboard` branch
- **Dashboard static:** `src/taskrunner/dashboard_static/` — built assets served by daemon
- **Tests:** `tests/` (97 test files, pytest)
- **Config model:** `src/taskrunner/models.py` — AgentDefinition, LLMConfig, etc.
- **Daemon port:** 8099
- **Auth:** Dashboard token in `~/.creel/dashboard-token`

## Important Architecture Notes

- Daemon is a FastAPI app serving both API and static dashboard files
- API routes: /health, /v1/status, /v1/messages, /v1/sessions, etc. (see daemon/api.py)
- Auth is Bearer token from dashboard-token file
- Config is agent.yml (YAML) loaded as AgentDefinition
- Real LLM calls via ANTHROPIC_API_KEY env var
- Dashboard uses MUI v6 (not Tailwind/shadcn)
- Tasks are YAML files in a tasks/ directory
- Cron jobs managed via daemon API

## Quality Checks

```bash
# Existing unit tests must pass
cd /Users/ross/.openclaw/workspace/projects/creel && uv run pytest tests/ -x -q -m "not smoke" 2>&1 | tail -20

# Dashboard must build (if you changed dashboard source)
cd /Users/ross/.openclaw/workspace/projects/creel/dashboard && npm run build 2>&1 | tail -10
```

## Branch Setup

This harness needs dashboard code. On first run:
```bash
git checkout -b ralph/test-harness main
git merge ralph/dashboard --no-edit
```

## Key Design Decisions

- **Real LLM calls** — tests use real Anthropic API (ANTHROPIC_API_KEY required), not mocks
- **Minimal config** — test agent uses claude-sonnet-4-20250514 with max_tokens 200 to minimize cost
- **Isolated test home** — ~/.creel-test/ so tests don't interfere with real config
- **Functional e2e** — assert elements exist and work, not visual regression
- **No channel testing** — no Telegram/iMessage in test config, pure API + dashboard

## Important

- Work on ONE story per iteration
- Commit after each story
- Keep existing tests passing (97 files, `pytest tests/ -m "not smoke"`)
- E2e tests need generous timeouts for real LLM calls (30s+)
- Dashboard Playwright tests need the dashboard source — make sure ralph/dashboard is merged
- When referencing daemon startup, check how `creel daemon start` works in the CLI or use direct Python invocation

## Progress Report Format

APPEND to scripts/ralph-harness/progress.txt:
```
## [Date/Time] - [Story ID]
- What was implemented
- Files changed
- **Learnings:**
  - Patterns discovered
  - Gotchas encountered
---
```

## Stop Condition

After completing a user story, check if ALL stories have `passes: true`.
If ALL complete: reply with `<promise>COMPLETE</promise>`
If stories remain: end normally.
