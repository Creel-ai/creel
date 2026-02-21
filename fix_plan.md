# Dynamic Cron / Scheduled Jobs — Implementation Plan

Issue: #143
Spec: `.ralph/specs/cron-scheduled-jobs.md`

## Phase 1: Data Models & Persistence

- [x] **1.1** Create cron data models (`CronJob`, `Schedule`, `Payload`, `Delivery`, `RunRecord`)
  - File: `src/taskrunner/cron/models.py`
  - Pydantic v2 models following patterns in `src/taskrunner/models.py`
  - Schedule: `kind` (cron | every | at), `expr`, `tz`
  - Payload: `kind` (agentTurn | systemEvent), `message`, `model`, `timeout_seconds`
  - Delivery: `mode` (announce | webhook | none), `channel`, `url`, `best_effort`
  - RunRecord: `job_id`, `started_at`, `ended_at`, `status` (success | failure), `error`
  - CronJob: `id`, `name`, `schedule`, `target` (main | isolated), `payload`, `delivery`, `enabled`, `created_at`, `updated_at`, `source` (user | yaml_import)
  - Tests: `tests/test_cron_models.py`

- [ ] **1.2** Create JobStore (JSON file persistence)
  - File: `src/taskrunner/cron/store.py`
  - `JobStore` class: `load()`, `save()`, `add()`, `get()`, `list()`, `update()`, `remove()`
  - Run history: `add_run()`, `get_runs()`, capped at N per job
  - Graceful handling of corrupt JSON (log + start empty)
  - Paths: `~/.creel/cron/jobs.json`, `~/.creel/cron/runs.json`
  - Tests: `tests/test_cron_store.py`

## Phase 2: CronManager (Store + Scheduler Bridge)

- [ ] **2.1** Create CronManager
  - File: `src/taskrunner/cron/manager.py`
  - Wraps `JobStore` + APScheduler `BackgroundScheduler`
  - CRUD operations: `add_job()`, `list_jobs()`, `update_job()`, `remove_job()`
  - `trigger_job()` — run immediately
  - `start()` / `stop()` — lifecycle
  - Syncs stored jobs to APScheduler on start
  - Auto-deletes one-shot (`at`) jobs after success
  - Tests: `tests/test_cron_manager.py`

## Phase 3: Job Execution & Delivery

- [ ] **3.1** Create job executor
  - File: `src/taskrunner/cron/executor.py`
  - Main session: inject system event into conversation
  - Isolated: dedicated agent turn via `run_agent_loop()`
  - Tests: `tests/test_cron_executor.py`

- [ ] **3.2** Create delivery routing
  - File: `src/taskrunner/cron/delivery.py`
  - `announce`: send to chat channel
  - `webhook`: POST to URL
  - `none`: silent
  - `best_effort` flag — don't fail job on delivery error
  - Tests: `tests/test_cron_delivery.py`

## Phase 4: Agent Tool

- [ ] **4.1** Create cron agent tool definition and handler
  - File: `src/taskrunner/cron/tool.py`
  - Actions: `list`, `add`, `update`, `remove`, `run`, `runs`
  - Returns JSON responses matching existing tool patterns
  - Tests: `tests/test_cron_tool.py`

- [ ] **4.2** Wire tool into agent system
  - Modify: `src/taskrunner/tools.py` — add BUILTIN_CRON_TOOLS, handle in execute_tool_call
  - Modify: `src/taskrunner/agent.py` — pass cron_manager to tool execution
  - Tests: verify cron tool appears in tool definitions

## Phase 5: CLI Commands

- [ ] **5.1** Add `creel cron` subcommand group
  - Modify: `src/taskrunner/cli.py`
  - Commands: `list`, `add`, `remove`, `edit`, `run`, `runs`, `import`
  - Tests: `tests/test_cron_cli.py`

## Phase 6: Daemon Integration

- [ ] **6.1** Wire CronManager into DaemonService
  - Modify: `src/taskrunner/daemon/service.py`
  - Replace or supplement existing `start_scheduler()` with CronManager
  - Expose cron status in `status()` endpoint
  - Tests: `tests/test_daemon_cron.py`

- [ ] **6.2** YAML task backward compatibility
  - Add `import_yaml_tasks()` to CronManager
  - Existing YAML tasks load as read-only cron jobs on startup
  - `creel cron import tasks/` converts them
  - Tests: verify YAML tasks still schedule correctly

## Learnings

(Updated as implementation progresses)
