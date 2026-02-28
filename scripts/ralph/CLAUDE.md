# Ralph Agent Instructions

You are an autonomous coding agent working on the Creel dashboard — a web UI for managing an AI agent framework.

## Your Task

1. Read the PRD at `scripts/ralph/prd.json`
2. Read the progress log at `scripts/ralph/progress.txt` (check Codebase Patterns section first)
3. Check you're on the correct branch from PRD `branchName`. If not, check it out or create from main.
4. Pick the **highest priority** user story where `passes: false`
5. Implement that single user story
6. Run quality checks: `cd /Users/ross/.openclaw/workspace/projects/creel && python -m pytest tests/ -x -q 2>&1 | tail -20`
7. Update CLAUDE.md files if you discover reusable patterns (see below)
8. If checks pass, commit ALL changes with message: `feat: [Story ID] - [Story Title]`
9. Update the PRD to set `passes: true` for the completed story
10. Append your progress to `scripts/ralph/progress.txt`

## Project Context

- **Repo root:** `/Users/ross/.openclaw/workspace/projects/creel`
- **Python source:** `src/taskrunner/` (the daemon, scheduler, models, CLI)
- **Daemon API:** `src/taskrunner/daemon/api.py` (FastAPI app)
- **Dashboard frontend:** `dashboard/` (React + Vite + MUI — you create this)
- **Dashboard static output:** `src/taskrunner/dashboard_static/` (built files served by daemon)
- **Tasks dir:** `tasks/` (YAML task definitions)
- **Config:** `agent.yaml` at repo root (or ~/.creel/agent.yaml)
- **Tests:** `tests/` (pytest)

## Progress Report Format

APPEND to scripts/ralph/progress.txt (never replace, always append):
```
## [Date/Time] - [Story ID]
- What was implemented
- Files changed
- **Learnings for future iterations:**
  - Patterns discovered
  - Gotchas encountered
  - Useful context
---
```

## Consolidate Patterns

If you discover a **reusable pattern**, add it to the `## Codebase Patterns` section at the TOP of progress.txt.

## Browser Testing

You have a Playwright MCP server available for browser testing. Use it for any story that changes UI:

1. Start the Vite dev server if needed: `cd dashboard && npm run dev` (runs on port 5173)
2. Use the Playwright MCP `browser_navigate` tool to visit `http://localhost:5173`
3. Take a screenshot to verify the UI looks correct
4. Note any visual issues in your progress report

This is especially important for layout, theming, and component rendering stories.

## Quality Requirements

- ALL commits must pass existing pytest tests
- Do NOT break existing daemon functionality
- Keep changes focused and minimal per story
- Follow existing code patterns (Pydantic models, FastAPI routers, etc.)
- For frontend: ensure `npm run build` succeeds with no TypeScript errors

## Stop Condition

After completing a user story, check if ALL stories have `passes: true`.

If ALL stories are complete and passing, reply with:
<promise>COMPLETE</promise>

If there are still stories with `passes: false`, end your response normally (another iteration will pick up the next story).

## Important

- Work on ONE story per iteration
- Commit frequently
- Keep existing tests passing
- Read the Codebase Patterns section in progress.txt before starting
