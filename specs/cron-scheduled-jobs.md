# SPEC: Cron / Scheduled Jobs (Issue #143)

Source: https://github.com/Creel-ai/creel/issues/143

## What It Does

Creel's current scheduler runs YAML-defined tasks on cron schedules. It works but is static — tasks are files on disk, there's no runtime management, no one-shot reminders, no delivery routing, and no way for the agent itself to create or manage jobs.

This spec upgrades the scheduler to a dynamic, agent-managed cron system with:

- **Runtime job management** — create, list, update, delete, and trigger jobs via CLI and agent tool calls (no file editing)
- **Three schedule types** — cron expressions, fixed intervals, and one-shot timestamps
- **Two execution modes** — main session (inject into conversation) or isolated (dedicated agent run)
- **Delivery routing** — send job output to a specific channel, webhook URL, or keep it internal
- **Persistence** — jobs survive daemon restarts
- **Run history** — track when jobs ran and whether they succeeded

## Architecture

```
┌──────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  CLI / Agent │────▶│  Cron Manager    │────▶│  Job Store      │
│  tool call   │     │  (add/list/edit/  │     │  (jobs.json)    │
│              │     │   delete/trigger) │     │                 │
└──────────────┘     └───────┬──────────┘     └─────────────────┘
                             │
                     ┌───────▼──────────┐
                     │  Scheduler Loop  │
                     │  (APScheduler)   │
                     └───────┬──────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼                             ▼
     ┌────────────────┐           ┌─────────────────┐
     │  Main Session  │           │  Isolated Run   │
     │  (system event │           │  (dedicated     │
     │   injected)    │           │   agent turn)   │
     └───────┬────────┘           └───────┬─────────┘
             │                            │
             ▼                            ▼
     Agent processes               ┌─────────────┐
     on next turn                  │  Delivery    │
                                   │  • channel   │
                                   │  • webhook   │
                                   │  • none      │
                                   └──────────────┘
```

## Existing Code Context

Key files to understand before implementing:

| Component | File |
|-----------|------|
| Current scheduler | `src/taskrunner/scheduler.py` (APScheduler BlockingScheduler, loads YAML tasks) |
| Task models | `src/taskrunner/models.py` (TaskDefinition, load_task, load_all_tasks — Pydantic v2) |
| Orchestrator | `src/taskrunner/orchestrator.py` (run_task pipeline) |
| Agent loop | `src/taskrunner/agent.py` (run_agent_loop, AgentResult) |
| Tool system | `src/taskrunner/tools.py` (build_tool_definitions, built-in tools like memory) |
| Chat server | `src/taskrunner/chat.py` (ChatServer, session routing, channel integration) |
| CLI entry | `src/taskrunner/cli.py` (argparse subcommands) |
| Daemon | `src/taskrunner/daemon/service.py` (DaemonService, start_scheduler in bg thread) |
| Agent config | `agent.yaml` (tools, channels, sessions) |
| Existing tasks | `tasks/*.yaml` |

## Schedule Types

| Type | Use case | Example |
|------|----------|---------|
| `cron` | Recurring on a schedule | `"0 8 * * *"` (daily 8am) |
| `every` | Fixed interval | `300` (every 5 minutes) |
| `at` | One-shot at a specific time | `"2026-03-01T09:00:00-07:00"` |

One-shot (`at`) jobs auto-delete after success by default.

## Execution Modes

**Main session** — injects a system event into the main conversation. The agent sees it on its next turn. Best for reminders and context-aware tasks.

**Isolated** — runs a fresh, dedicated agent turn with its own session. Output is delivered to a channel, webhook, or kept internal. Best for background work.

## Delivery (isolated jobs only)

| Mode | Behavior |
|------|----------|
| `announce` (default) | Send output to a chat channel |
| `webhook` | POST output to a URL |
| `none` | Run silently, no delivery |

## Job Definition (JSON)

```json
{
  "name": "Morning briefing",
  "schedule": {
    "kind": "cron",
    "expr": "0 8 * * *",
    "tz": "America/Denver"
  },
  "target": "isolated",
  "payload": {
    "kind": "agentTurn",
    "message": "Summarize overnight emails and today's calendar.",
    "model": "claude-sonnet-4-20250514",
    "timeout_seconds": 120
  },
  "delivery": {
    "mode": "announce",
    "channel": "whatsapp"
  },
  "enabled": true
}
```

## CLI Commands

```
creel cron list                         # list all jobs
creel cron add --name "..." --cron "..." --message "..."
creel cron add --name "..." --at "2026-03-01T09:00:00" --system-event "..."
creel cron edit <job-id> --disable
creel cron remove <job-id>
creel cron run <job-id>                 # trigger immediately
creel cron runs <job-id>               # show run history
```

## Agent Tool

The agent gets a `cron` tool with actions: `list`, `add`, `update`, `remove`, `run`, `runs`. This lets the agent schedule its own reminders and background tasks conversationally.

## Persistence

Jobs stored in `~/.creel/cron/jobs.json`. Run history in `~/.creel/cron/runs.json` (last N runs per job, default 50).

## Backward Compatibility

Existing YAML task files in `tasks/` continue to work. They're loaded as read-only cron jobs on startup. Users can migrate with `creel cron import tasks/`.

## New Code Location

```
src/taskrunner/cron/
├── __init__.py      # Package exports
├── models.py        # CronJob, Schedule, Payload, Delivery, RunRecord
├── store.py         # JobStore (JSON persistence)
├── manager.py       # CronManager (store + APScheduler)
├── executor.py      # Job execution (main session / isolated)
├── delivery.py      # Output routing (announce / webhook / none)
└── tool.py          # Agent tool definition and handler
```

---

## Phases

### Phase 1: Data Models & Job Store
- Define Pydantic v2 models in `src/taskrunner/cron/models.py`: CronJob, Schedule (cron/every/at), Payload (agentTurn/systemEvent), Delivery (announce/webhook/none), RunRecord
- Implement JobStore in `src/taskrunner/cron/store.py`: JSON persistence at ~/.creel/cron/, CRUD operations, run history with configurable cap (default 50), corrupt file recovery
- Atomic writes via temp file + rename
- Tests: `tests/test_cron_models.py`, `tests/test_cron_store.py`

### Phase 2: Cron Manager & Scheduler Integration
- Implement CronManager in `src/taskrunner/cron/manager.py`: wraps JobStore + APScheduler, add/remove/update/trigger/enable/disable jobs, auto-delete one-shot `at` jobs after success
- Refactor `src/taskrunner/scheduler.py` to use AsyncIOScheduler (needed for async agent turns), load legacy YAML tasks as read-only cron jobs alongside managed jobs
- Backward compat: existing YAML tasks still fire on schedule
- Tests: `tests/test_cron_manager.py`, update `tests/test_scheduler.py`

### Phase 3: Job Execution & Delivery
- Implement job executor in `src/taskrunner/cron/executor.py`: main session mode (inject system event into ChatServer session), isolated mode (temporary session + run_agent_loop), model override support, run recording
- Implement delivery routing in `src/taskrunner/cron/delivery.py`: announce (channel routing), webhook (httpx POST), none (silent), best_effort flag
- Tests: `tests/test_cron_executor.py`, `tests/test_cron_delivery.py`

### Phase 4: CLI Commands
- Add `creel cron` subcommand group to `src/taskrunner/cli.py`: list, add, edit, remove, run, runs
- Add `creel cron import tasks/` for YAML migration
- Follow existing argparse patterns in cli.py
- Tests: `tests/test_cron_cli.py`

### Phase 5: Agent Tool
- Implement `cron` agent tool in `src/taskrunner/cron/tool.py`: actions list/add/update/remove/run/runs, Anthropic-compatible tool schema
- Register as built-in tool in `build_tool_definitions()` (like memory tools), handle in agent loop tool dispatch
- Tests: `tests/test_cron_tool.py`

### Phase 6: Daemon Integration
- Wire CronManager into DaemonService: init on startup (load jobs.json + legacy YAML), pass to ChatServer for session event injection, graceful shutdown
- Tests: `tests/test_daemon_cron.py`

### Phase 7: Acceptance Testing
- End-to-end: CLI add → scheduler fires → delivery routes → history recorded
- Agent tool: agent creates reminder → fires → injects into session
- Legacy compat: existing YAML tasks still work alongside managed jobs
- Edge cases: past one-shot fires immediately, concurrent jobs both run, failed payload doesn't disable job, corrupt jobs.json recovery
- Run full test suite, verify all acceptance criteria from issue #143

---

## Acceptance Criteria

### Core scheduling
- [ ] Create a cron job via CLI → it runs at the specified time
- [ ] Create an `at` job 1 minute in the future → it fires once and auto-deletes
- [ ] Create an `every` job with 60s interval → it fires repeatedly
- [ ] Restart the daemon → all jobs survive and resume on schedule
- [ ] Disable a job → it stops firing; re-enable → it resumes

### Execution modes
- [ ] Main session job injects a system event into the agent's conversation
- [ ] Isolated job runs a fresh agent turn without polluting main session history
- [ ] Isolated job with `model` override uses the specified model

### Delivery
- [ ] Isolated job with `announce` delivery sends output to the configured channel
- [ ] Isolated job with `webhook` delivery POSTs output to the URL
- [ ] Isolated job with `none` delivery runs silently
- [ ] If delivery fails and `best_effort` is true, job still succeeds

### Agent tool
- [ ] Agent can create a job via the cron tool
- [ ] Agent can list, update, and delete jobs
- [ ] Agent can trigger a job immediately via `run`

### CLI
- [ ] `creel cron list` shows all jobs with status, schedule, last run
- [ ] `creel cron add` creates a job with all schedule types
- [ ] `creel cron run <id>` triggers immediately
- [ ] `creel cron runs <id>` shows run history with timestamps and status

### Run history
- [ ] Each run records: job ID, start time, end time, status, error message if failed
- [ ] History is capped (configurable, default 50 per job)

### Backward compatibility
- [ ] Existing YAML tasks in `tasks/` still run on their schedules
- [ ] `creel cron import tasks/` converts YAML tasks to managed cron jobs

### Edge cases
- [ ] Job scheduled for a time in the past (one-shot) → fires immediately
- [ ] Two jobs scheduled at the same time → both run
- [ ] Job payload fails → error logged, job stays enabled for next run
- [ ] Daemon starts with corrupt `jobs.json` → logs error, starts empty

## Progress

- [x] Phase 1: Data Models & Job Store — models.py, store.py, tests passing (52 tests)
- [x] Phase 2: Cron Manager & Scheduler Integration — manager.py wraps JobStore + APScheduler BackgroundScheduler, CRUD + enable/disable/trigger, one-shot auto-delete, legacy YAML task loading (read-only), store.py updated with keep_history option, 45 new tests (tests/test_cron_manager.py), all 1470 tests passing
- [x] Phase 3: Job Execution & Delivery — executor.py with JobExecutor class (main-session event injection via callback, isolated mode via run_agent_loop, model override support), delivery.py with announce/webhook/none routing and best_effort flag, 31 new tests (tests/test_cron_executor.py, tests/test_cron_delivery.py), all 1501 tests passing
