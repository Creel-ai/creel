# Creel Dashboard Spec

## Overview

A local web dashboard for managing Creel — focused on the things that are annoying to hand-edit in YAML: **tasks/jobs**, **workspace files**, and **cron schedules**. Not trying to replicate OpenClaw's full dashboard; this is a config management UI.

## Inspiration: OpenClaw Dashboard

OpenClaw's dashboard (served by the gateway on the same port) has:
- **Sidebar nav:** Chat, Control (Overview, Channels, Instances, Sessions, Usage, Cron Jobs), Agent (Agents, Skills, Nodes), Settings (Config, Debug, Logs)
- **Cron Jobs page:** Job list with search/filter, run history with status/delivery filters, and a "New Job" form with basics/schedule/execution/delivery sections
- **Config page:** Schema-driven form editor with search, tag filters, Form/Raw toggle, Save/Apply/Update workflow
- **Overview:** Gateway access config (WebSocket URL, token), health snapshot, instance/session/cron counts

**What we like:**
- Cron job management UI — creating/editing jobs visually instead of YAML
- Config editor with form + raw modes
- Clean sidebar organization
- Status overview at a glance

**What we'd simplify:**
- No Chat page (Creel doesn't have a web chat interface — channels handle that)
- No Instances/Nodes pages (Creel is single-instance for now)
- No Usage page (keep it simple initially)
- Fewer config knobs — Creel's config is simpler than OpenClaw's

## Architecture

### Serving
- Dashboard served by the Creel daemon on the same HTTP port as the API (e.g., `localhost:8099`)
- Static SPA served at `/` — FastAPI serves the built frontend
- API endpoints at `/api/...` (already exists for daemon control)
- WebSocket at `/ws` for live updates (daemon logs, job run status)

### Tech Stack
- **Frontend:** React + TypeScript (Vite build)
- **UI library:** shadcn/ui + Tailwind CSS (clean, minimal, customizable)
- **Backend:** FastAPI (already the daemon's framework)
- **State:** Reads/writes `~/.creel/` files directly via API

### Why React + shadcn?
- Creel is a dev tool — the users are developers
- shadcn gives us clean components without a heavy framework
- Tailwind keeps styling maintainable
- Vite builds are fast, output is small
- OpenClaw uses a similar approach — proven pattern

## Pages

### 1. Overview (`/`)

At-a-glance status for the Creel daemon.

**Sections:**
- **Daemon Status** — running/stopped, PID, uptime, socket path
- **Agent Info** — name, model, provider (from `agent.yaml`)
- **Channels** — connected channels with status (Telegram ✅, WhatsApp ❌, etc.)
- **Quick Stats** — active tasks, cron jobs (enabled/total), next scheduled run
- **Recent Activity** — last 5 cron runs with status badges

### 2. Tasks (`/tasks`)

Manage task definitions (the YAML files in `~/.creel/tasks/`).

**List view:**
- Table of all tasks with: name, description, schedule (if any), last run, status
- Search/filter by name
- Enable/disable toggle per task
- Click to edit

**Detail/Edit view:**
- Form fields mapped to the task YAML schema:
  - Name, description
  - Prompt/instruction
  - Schedule (cron expression, interval, or manual-only)
  - Model override
  - Timeout
  - Delivery (channel, recipient)
  - Tool allowlist
- **Raw YAML tab** — edit the YAML directly with syntax highlighting
- Save validates YAML before writing
- "Run now" button for manual trigger

**New Task:**
- Same form as edit, with sensible defaults
- Templates for common patterns (daily digest, monitor, reminder)

### 3. Cron Jobs (`/cron`)

Dedicated view for scheduled execution — overlaps with Tasks but focused on the schedule/run-history angle.

**Job List:**
- All tasks that have a schedule defined
- Columns: name, schedule (human-readable), next run, last run, status, enabled
- Sort by next run (default), name, last updated
- Quick toggle enable/disable

**Run History:**
- Filterable log of all cron executions
- Columns: job name, started, duration, status (success/failed/timeout), delivery status
- Expandable rows showing: prompt sent, response summary, token usage, error details
- Filter by status, job name, date range

**New Job form** (similar to OpenClaw's):
- Basics: name, description, enabled
- Schedule: "Every X minutes/hours/days" or cron expression, with a human-readable preview
- Execution: prompt, model, timeout
- Delivery: channel, recipient, announce/silent/webhook

### 4. Files (`/files`)

Browse and edit files in `~/.creel/workspace/` and `~/.creel/` config files.

**File Browser:**
- Tree view of `~/.creel/` with expandable directories
- Highlight key files: `agent.yaml`, `workspace/SOUL.md`, `workspace/MEMORY.md`
- File icons by type (YAML, Markdown, JSON)
- Click to open in editor

**Editor:**
- Syntax-highlighted editor (Monaco or CodeMirror)
- YAML validation for config files
- Markdown preview for `.md` files
- Save with backup (keep `.bak` of previous version)
- Diff view for unsaved changes

**Quick Access:**
- Pinned files section at top: `agent.yaml`, `SOUL.md`, `USER.md`, `MEMORY.md`
- These are the files you edit most — one click away

### 5. Config (`/config`)

Structured editor for `agent.yaml` — the main Creel config.

**Form Mode:**
- Grouped sections matching `agent.yaml` structure:
  - **Agent** — name, model, provider, API key reference
  - **Channels** — per-channel config (Telegram token, WhatsApp settings, etc.)
  - **Tools** — tool definitions, enable/disable, Docker settings
  - **Guardian** — policy settings, content filtering
  - **Daemon** — socket path, log level, port
- Validation on save
- Descriptions/help text for each field

**Raw Mode:**
- Full YAML editor with syntax highlighting
- Toggle between Form ↔ Raw

**Workflow:**
- Save writes to disk
- "Apply" restarts the daemon to pick up changes (with confirmation)
- Change indicator ("unsaved changes" badge)

### 6. Logs (`/logs`)

Live daemon log viewer.

- Streaming log output via WebSocket
- Filter by level (DEBUG, INFO, WARN, ERROR)
- Search within logs
- Auto-scroll with pause button
- Download log file

## API Endpoints (new)

These extend the existing FastAPI daemon API:

```
# Status
GET  /api/status              → daemon status, agent info, channel health

# Tasks
GET  /api/tasks               → list all task definitions
GET  /api/tasks/:name         → single task detail
POST /api/tasks               → create task
PUT  /api/tasks/:name         → update task
DELETE /api/tasks/:name       → delete task
POST /api/tasks/:name/run     → trigger manual run

# Cron / Schedules
GET  /api/cron/jobs            → list scheduled jobs
GET  /api/cron/history         → run history (paginated, filterable)
PUT  /api/cron/jobs/:name      → update schedule
POST /api/cron/jobs/:name/toggle → enable/disable

# Files
GET  /api/files/tree           → directory tree of ~/.creel/
GET  /api/files/*path          → file contents
PUT  /api/files/*path          → write file (with backup)

# Config
GET  /api/config               → parsed agent.yaml
PUT  /api/config               → write agent.yaml
POST /api/config/apply         → restart daemon with new config

# Logs
WS   /ws/logs                  → streaming log output
```

## Implementation Plan

### Phase 1: Foundation
1. Add static file serving to FastAPI daemon (`/` serves built frontend)
2. Scaffold React + Vite + shadcn/ui project in `dashboard/` directory
3. Implement sidebar layout and routing
4. Build Overview page (daemon status, basic stats)

### Phase 2: Task & Cron Management
5. Build `/api/tasks` and `/api/cron` endpoints
6. Task list page with search/filter
7. Task edit form with YAML validation
8. Cron job list with run history
9. New job form (schedule builder, delivery config)

### Phase 3: File & Config Editing
10. Build `/api/files` endpoints
11. File browser with tree view
12. Code editor integration (CodeMirror — lighter than Monaco)
13. Config form mode with agent.yaml schema
14. Form ↔ Raw toggle

### Phase 4: Live Features
15. WebSocket log streaming
16. Live cron run status updates
17. Toast notifications for job completions/failures
18. "Run now" manual trigger with live output

## Design Principles

- **Config-first, not ops-first** — this is for editing config, not monitoring a fleet
- **YAML is the source of truth** — the UI reads/writes the same files the CLI uses. No separate database.
- **Non-destructive** — always backup before overwriting, show diffs, confirm destructive actions
- **Works offline** — static SPA, no CDN dependencies, all assets bundled
- **Mobile-friendly** — responsive layout (you'll check this from your phone sometimes)
- **Dark mode** — developers live in dark mode

## Open Questions

- **Auth for the dashboard?** OpenClaw uses a gateway token. Creel could use a simple token or rely on the fact that it's localhost-only. Leaning toward optional token auth.
- **Bundle in the wheel?** The built dashboard should ship with `pip install creel` — no separate `npm install` for end users. Build step in CI, bundle as package data.
- **Real-time vs polling?** WebSocket for logs makes sense. For task/cron status, polling every 5s is probably fine and simpler.
- **Editor choice?** CodeMirror 6 is lighter (~150KB) vs Monaco (~2MB). Leaning CodeMirror since we're bundling in a pip package.
