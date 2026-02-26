# Ralph Agent Instructions — Creel Media Messages

You are an autonomous coding agent adding image and voice message support to Creel (an AI agent framework).

## Your Task

1. Read the PRD at `scripts/ralph-media/prd.json`
2. Read the progress log at `scripts/ralph-media/progress.txt` (check Codebase Patterns section first)
3. Check you're on the correct branch from PRD `branchName`. If not, check it out or create from main.
4. Pick the **highest priority** user story where `passes: false`
5. Implement that single user story
6. Run quality checks (see below)
7. If checks pass, commit ALL changes with message: `feat: [Story ID] - [Story Title]`
8. Update the PRD to set `passes: true` for the completed story
9. Append your progress to `scripts/ralph-media/progress.txt`

## Project Context

- **Repo root:** `/Users/ross/.openclaw/workspace/projects/creel`
- **Python source:** `src/taskrunner/` (the agent framework)
- **Channels:** `src/taskrunner/channels/` (channel implementations)
- **Core agent:** `src/taskrunner/chat.py` (ChatServer — message handling + LLM calls)
- **Daemon:** `src/taskrunner/daemon/` (service layer bridging channels to ChatServer)
- **Models:** `src/taskrunner/models.py` (all Pydantic config models)
- **Tests:** `tests/` (pytest)

## Quality Checks

```bash
cd /Users/ross/.openclaw/workspace/projects/creel && uv run pytest tests/ -x -q 2>&1 | tail -20
```

All tests must pass before committing.

## Key Design Decisions

- **Backward compatible**: existing channels that only handle text MUST continue to work without changes
- **No heavy dependencies**: use httpx (already installed) for Telegram API, Pillow as optional for vision
- **OpenAI Whisper API** as primary transcription backend — most users will have an OpenAI key already
- **Local filesystem** for media storage — no cloud services
- **Content blocks**: when images are present, LLM message content changes from string to list of content blocks (OpenAI and Anthropic formats)
- **Telegram first**: this is the primary channel to get right. iMessage media is secondary.
- **Only Telegram and iMessage**: do NOT add media support to WhatsApp or other channels.

## Coding Style

- Follow existing patterns in the codebase
- Pydantic v2 for all models
- Type hints everywhere
- Logging via `logging.getLogger(__name__)`
- Docstrings for public methods
- Keep modules focused and small

## Progress Report Format

APPEND to scripts/ralph-media/progress.txt:
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
If stories remain: end normally (another iteration picks up next).

## Important

- Work on ONE story per iteration
- Commit after each story
- Keep existing tests passing
- Read Codebase Patterns in progress.txt before starting
- Don't break existing channel functionality
