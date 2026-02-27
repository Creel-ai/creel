# SPEC: Cron / Scheduled Jobs

## What It Does

Creel's current scheduler runs YAML-defined tasks on cron schedules. It works but is static — tasks are files on disk, there's no runtime management, no one-shot reminders, no delivery routing, and no way for the agent itself to create or manage jobs.

This spec upgrades the scheduler to a dynamic, agent-managed cron system with:

- **Runtime job management** — create, list, update, delete, and trigger jobs via CLI and agent tool calls (no file editing)
- **Three schedule types** — cron expressions, fixed intervals, and one-shot timestamps
- **Two execution modes** — main session (inject into conversation) or isolated (dedicated agent run)
- **Delivery routing** — send job output to a specific channel, webhook URL, or keep it internal
- **Persistence** — jobs survive daemon restarts
- **Run history** — track when jobs ran and whether they succeeded

## How It Works

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

### Schedule Types

| Type | Use case | Example |
|------|----------|---------|
| `cron` | Recurring on a schedule | `"0 8 * * *"` (daily 8am) |
| `every` | Fixed interval | `300` (every 5 minutes) |
| `at` | One-shot at a specific time | `"2026-03-01T09:00:00-07:00"` |

One-shot (`at`) jobs auto-delete after success by default.

### Execution Modes

**Main session** — injects a system event into the main conversation. The agent sees it on its next turn (or immediately if wake mode is "now"). Best for reminders and context-aware tasks.

**Isolated** — runs a fresh, dedicated agent turn with its own session. Output is delivered to a channel, webhook, or kept internal. Best for background work that shouldn't clutter the main conversation.

### Delivery (isolated jobs only)

| Mode | Behavior |
|------|----------|
| `announce` (default) | Send output to a chat channel |
| `webhook` | POST output to a URL |
| `none` | Run silently, no delivery |

Announce delivery can target a specific channel (e.g. `whatsapp`, `telegram`) and recipient.

## Config Surface

### Job definition (JSON, used by CLI and agent tool)

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

### CLI commands

```
creel cron list                         # list all jobs
creel cron add --name "..." --cron "..." --message "..."
creel cron add --name "..." --at "2026-03-01T09:00:00" --system-event "Reminder: ..."
creel cron edit <job-id> --disable
creel cron remove <job-id>
creel cron run <job-id>                 # trigger immediately
creel cron runs <job-id>               # show run history
```

### Agent tool

The agent gets a `cron` tool with actions: `list`, `add`, `update`, `remove`, `run`, `runs`. This lets the agent schedule its own reminders and background tasks conversationally ("remind me in 20 minutes", "check my email every morning at 8").

### Persistence

Jobs stored in `~/.creel/cron/jobs.json`. Run history in `~/.creel/cron/runs.json` (last N runs per job, default 50).

### Backward compatibility

Existing YAML task files in `tasks/` continue to work. They're loaded as read-only cron jobs on startup (can be triggered but not edited via the cron API). Users can migrate with `creel cron import tasks/`.

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
- [ ] Agent can create a job via the cron tool ("remind me at 5pm")
- [ ] Agent can list, update, and delete jobs
- [ ] Agent can trigger a job immediately via `run`

### CLI
- [ ] `creel cron list` shows all jobs with status, schedule, last run
- [ ] `creel cron add` creates a job with all schedule types
- [ ] `creel cron run <id>` triggers immediately
- [ ] `creel cron runs <id>` shows run history with timestamps and status

### Run history
- [ ] Each run records: job ID, start time, end time, status (success/failure), error message if failed
- [ ] History is capped (configurable, default 50 per job)

### Backward compatibility
- [ ] Existing YAML tasks in `tasks/` still run on their schedules
- [ ] `creel cron import tasks/` converts YAML tasks to managed cron jobs

### Edge cases
- [ ] Job scheduled for a time in the past (one-shot) → fires immediately
- [ ] Two jobs scheduled at the same time → both run (no collision)
- [ ] Job payload fails → error logged, job stays enabled for next run
- [ ] Daemon starts with corrupt `jobs.json` → logs error, starts with empty job list
