# Creel

A secure LLM task runner and personal AI assistant that separates credential-bearing data fetching from LLM processing. Supports both scheduled tasks (morning briefings, weather summaries) and interactive agent mode (chat via CLI or iMessage with tool calling).

A creel is a wicker basket usually used for carrying fish or blocks of peat. It is also the fish trap used to catch lobsters and other crustaceans.

## Why

Agentic LLM systems give the model access to tools, credentials, and untrusted input all at once. This project preserves the core security property — the LLM never sees credentials — whether running scheduled tasks or interactive agent conversations:

| Component | Has access to | Does NOT have |
|-----------|--------------|---------------|
| Fetcher (gcal) | Google OAuth token (read-only) | LLM, other credentials |
| Fetcher (gcal_write) | Google OAuth token (calendar.events) | LLM, other credentials |
| Fetcher (gmail) | Google OAuth token (read-only) | LLM, other credentials |
| Fetcher (gmail_send) | Google OAuth token (gmail.send) | LLM, other credentials |
| Fetcher (gmail_modify) | Google OAuth token (gmail.modify) | LLM, other credentials |
| Fetcher (drive) | Google OAuth token (read-only) | LLM, other credentials |
| Fetcher (drive_write) | Google OAuth token (drive.file) | LLM, other credentials |
| Fetcher (weather) | Nothing sensitive | LLM, other credentials |
| LLM Runner | Anthropic API key | Any other credentials |
| Orchestrator | All secrets, LLM output | Untrusted external input |

Even if prompt injection occurs (e.g., via a calendar event title), the LLM container has nothing to exfiltrate except its own API key.

## Architecture

```mermaid
flowchart TD
    subgraph orch["Orchestrator"]
        direction TB
        schedule["Cron Scheduler"]
        template["Prompt Template"]
        output["Output Router"]
    end

    subgraph fetchers["Isolated Fetcher Containers"]
        direction TB
        gcal["Fetcher: gcal\n🔑 Google OAuth token\n(calendar.readonly)"]
        gcal_w["Fetcher: gcal_write\n🔑 Google OAuth token\n(calendar.events)"]
        gmail["Fetcher: gmail\n🔑 Google OAuth token\n(gmail.readonly)"]
        gmail_s["Fetcher: gmail_send\n🔑 Google OAuth token\n(gmail.send)"]
        gmail_m["Fetcher: gmail_modify\n🔑 Google OAuth token\n(gmail.modify)"]
        drive["Fetcher: drive\n🔑 Google OAuth token\n(drive.readonly)"]
        drive_w["Fetcher: drive_write\n🔑 Google OAuth token\n(drive.file)"]
        weather["Fetcher: weather\n🔑 None"]
    end

    subgraph llm_container["Isolated LLM Container"]
        llm["LLM Runner\n🔑 Anthropic API key"]
    end

    subgraph outputs["Delivery"]
        imsg["iMessage"]
        stdout["stdout"]
        file["File"]
    end

    schedule -- "triggers" --> gcal
    schedule -- "triggers" --> gcal_w
    schedule -- "triggers" --> gmail
    schedule -- "triggers" --> gmail_s
    schedule -- "triggers" --> gmail_m
    schedule -- "triggers" --> drive
    schedule -- "triggers" --> drive_w
    schedule -- "triggers" --> weather
    gcal -- "JSON" --> template
    gcal_w -- "JSON" --> template
    gmail -- "JSON" --> template
    gmail_s -- "JSON" --> template
    gmail_m -- "JSON" --> template
    drive -- "JSON" --> template
    drive_w -- "JSON" --> template
    weather -- "JSON" --> template
    template -- "rendered prompt\n(no secrets)" --> llm
    llm -- "text response" --> output
    output --> imsg
    output --> stdout
    output --> file

    style fetchers fill:#2d333b,stroke:#f47067,stroke-width:2px,color:#f0f0f0
    style llm_container fill:#2d333b,stroke:#f47067,stroke-width:2px,color:#f0f0f0
    style orch fill:#2d333b,stroke:#58a6ff,stroke-width:2px,color:#f0f0f0
    style outputs fill:#2d333b,stroke:#3fb950,stroke-width:2px,color:#f0f0f0
```

> **Key insight:** Even if prompt injection occurs (e.g., via a calendar event title), the LLM container has no access to Google credentials, user contacts, or anything beyond its own API key. Each red box is a separate security boundary.

### Agent Mode

In agent mode, the same security boundary applies — the LLM requests tool calls, but the orchestrator handles secrets injection and fetcher execution:

```
                    ┌─────────────┐
                    │   Channels  │
                    │ stdin | iMsg│
                    └──────┬──────┘
                           │ incoming message
                           ▼
                    ┌──────────────┐
                    │   Session    │
                    │   Manager   │
                    │ (JSON files) │
                    └──────┬──────┘
                           │ message + history
                           ▼
              ┌────────────────────────┐
              │      Agent Loop        │
              │                        │
              │  messages ──→ LLM call │
              │               ↓        │
              │          tool_use? ─no─→ response
              │               ↓ yes    │
              │     execute via fetcher │
              │     (secrets injected)  │
              │               ↓        │
              │     tool_result → loop  │
              └────────────────────────┘
```

Scheduled tasks can also use agent mode by setting `mode: agent` in the task YAML.

### Guardian

The guardian layer screens inputs and validates tool calls before they execute. All stages are optional and independently configurable in `agent.yaml`:

```
Incoming message
    │
    ▼
screen_input(text)                     ← before session history
    ├── FastClassifier  (DeBERTa/ONNX, ~10ms)
    └── LLMJudge        (Haiku, ~300ms, off by default)
    │
    │  blocked → return rejection, skip agent loop
    │
    ▼
Agent loop → LLM returns tool_use
    │
    ▼
validate_action(tool, args)            ← before execute_tool_call
    └── PolicyEngine    (YAML rules, <1ms)
           allow  → execute
           review → log warning, execute
           deny   → return error to LLM
```

**Stages:**

| Stage | Component | What it does |
|-------|-----------|--------------|
| 1 | Fast classifier | Local DeBERTa model scores prompt-injection likelihood against a confidence threshold |
| 2 | LLM judge | Secondary Haiku-based check (disabled by default) |
| 3 | Policy engine | `fnmatch` rules in `policies/default.yaml` map tool names to allow/review/deny |

**Policy rules** (`policies/default.yaml`):

```yaml
allow:  [check_weather, check_calendar, check_email, read_email, check_drive]
review: [send_*, upload_*, create_*, mark_*]
deny:   [trash_*, delete_*]
```

Deny wins over review, review wins over allow. Unknown tools default to review.

**Audit logging** writes to `guardian_audit.jsonl` with hashed inputs (never raw text), tool names, arg keys (not values), verdicts, and confidence scores.

**Configuration** in `agent.yaml`:

```yaml
guardian:
  enabled: true
  fast_classifier:
    enabled: true
    threshold: 0.85
    model_name: protectai/deberta-v3-base-prompt-injection-v2
  llm_judge:
    enabled: false
  policy:
    enabled: true
    policy_file: policies/default.yaml
  audit:
    enabled: true
    log_file: guardian_audit.jsonl
```

## Quick Start

```bash
# Set up Python and virtualenv (requires pyenv and uv)
pyenv install 3.12.12   # if not already installed
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# Install age for secrets encryption (one-time)
brew install age
mkdir -p ~/.age
age-keygen -o ~/.age/key.txt 2> ~/.age/key.pub

# List available tasks
./runner.py list

# Validate a task definition
./runner.py validate weather_check

# Dry run (renders prompt, skips LLM and output)
./runner.py run weather_check --dry

# Full run (requires Anthropic credentials — see Authentication below)
./runner.py run weather_check

# Start the cron scheduler
./runner.py schedule

# Interactive CLI chat (agent mode)
./runner.py chat

# Listen for iMessages and respond
./runner.py listen

# Listen + scheduler (daemon mode)
./runner.py serve
```

## Authentication

The runner supports two ways to authenticate with the Anthropic API:

| Method | Env var | How to get it |
|--------|---------|---------------|
| API key | `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com/) |
| Claude Code setup token | `ANTHROPIC_AUTH_TOKEN` | `claude setup-token` |

If both are set, `ANTHROPIC_AUTH_TOKEN` takes precedence.

### Using an API key

```bash
export ANTHROPIC_API_KEY=sk-ant-...
./runner.py run weather_check
```

### Using a Claude Code setup token

Claude Code can generate OAuth tokens that work with the Anthropic API:

```bash
# Generate a setup token (requires Claude Code CLI)
claude setup-token
# Copy the sk-ant-oat01-... value

export ANTHROPIC_AUTH_TOKEN=sk-ant-oat01-...
./runner.py run weather_check
```

### Storing credentials in a secrets file

Either variable can go in an age-encrypted secrets file:

```bash
# Create the plaintext .env
echo 'ANTHROPIC_AUTH_TOKEN=sk-ant-oat01-...' > secrets/anthropic.env

# Encrypt and delete plaintext
./scripts/encrypt-secret.sh secrets/anthropic.env
rm secrets/anthropic.env
```

Then reference it in your task YAML under `llm.secrets`.

### Root `.env` file

The runner loads a root `.env` file (gitignored) at startup for non-secret configuration like phone numbers:

```bash
# .env (project root — gitignored, never committed)
PHONE=+1234567890
```

Values are available as environment variables and can be referenced in task YAMLs with `$VAR` syntax:

```yaml
output:
  type: imessage
  to: "$PHONE"
```

Real environment variables take precedence over `.env` values.

## Task Definitions

Tasks are YAML files in `tasks/`. Each defines what data to fetch, how to prompt the LLM, and where to send the result.

```yaml
# tasks/morning_briefing.yaml
name: morning_briefing
schedule: "0 7 * * *"  # 7am daily

fetch:
  calendar:
    image: fetcher-gcal:latest
    secrets: secrets/gcal.env.enc
    args:
      range: today

  weather:
    image: fetcher-weather:latest
    args:
      location: denver

prompt: |
  You're my personal assistant. Give me a quick rundown of my day.

  Today: {date}
  Weather: {weather}
  Calendar: {calendar}

  Keep it under 150 words. Flag any early meetings or conflicts.

output:
  type: imessage
  to: "$PHONE"

llm:
  model: claude-sonnet-4-20250514
  max_tokens: 300
  secrets: secrets/anthropic.env.enc
```

### Task fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Unique task identifier |
| `schedule` | yes | 5-part cron expression |
| `fetch` | yes | Map of fetcher name to config |
| `fetch.<name>.image` | yes | Docker image for containerized mode |
| `fetch.<name>.secrets` | no | Path to age-encrypted .env file |
| `fetch.<name>.args` | no | Key-value args passed to the fetcher |
| `prompt` | yes | Prompt template with `{name}` placeholders |
| `output.type` | yes | `imessage`, `stdout`, or `file` |
| `output.to` | yes | Phone number, empty string, or file path |
| `llm.model` | no | Anthropic model ID (default: `claude-sonnet-4-20250514`) |
| `llm.max_tokens` | no | Max response tokens (default: 300) |
| `llm.secrets` | no | Path to age-encrypted .env with `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_API_KEY` |

The `{date}` placeholder is always available and resolves to the current date.

### Agent mode tasks

Tasks can use the agent loop for multi-step tool calling by setting `mode: agent`:

```yaml
# tasks/email_triage.yaml
name: email_triage
schedule: "0 8 * * *"
mode: agent

fetch:
  gmail:
    image: fetcher-gmail:latest
    secrets: secrets/gmail.env.enc
    args:
      query: "is:unread newer_than:1d"

tools:
  trash_email:
    fetcher: gmail_modify
    secrets: secrets/gmail_modify.env.enc
    description: "Move an email to trash"
    parameters:
      message_id:
        type: string
        description: "Gmail message ID"
        required: true
    fixed_args:
      action: "trash"

agent:
  max_turns: 10

prompt: |
  Triage my unread emails. Trash spam. Summarize what you did.
  {gmail}

output:
  type: imessage
  to: "$PHONE"

llm:
  model: claude-sonnet-4-20250514
  max_tokens: 1024
  secrets: secrets/anthropic.env.enc
```

Agent mode task fields (in addition to standard fields):

| Field | Required | Description |
|-------|----------|-------------|
| `mode` | no | `simple` (default) or `agent` |
| `tools` | no | Map of tool name to tool config |
| `tools.<name>.fetcher` | yes | Fetcher to execute (e.g., `gmail_modify`) |
| `tools.<name>.secrets` | no | Path to age-encrypted .env file |
| `tools.<name>.description` | yes | Description shown to the LLM |
| `tools.<name>.parameters` | no | Parameters the LLM can provide |
| `tools.<name>.fixed_args` | no | Args always passed to fetcher (override LLM input) |
| `agent.max_turns` | no | Max agent loop iterations (default: 10) |

## Agent Configuration

The global agent config (`agent.yaml`) defines tools, LLM settings, session behavior, and channels for interactive chat mode:

```yaml
system_prompt: |
  You are a personal assistant. Be concise and helpful.
  Today is {date}.

tools:
  check_weather:
    fetcher: weather
    description: "Get current weather and forecast"
    parameters:
      location:
        type: string
        description: "City name or coordinates"
        required: true

  check_email:
    fetcher: gmail
    secrets: secrets/gmail.env.enc
    description: "Search Gmail for emails"
    parameters:
      query:
        type: string
        description: "Gmail search query"
        required: true

llm:
  model: claude-sonnet-4-20250514
  max_tokens: 1024
  secrets: secrets/anthropic.env.enc

agent:
  max_turns: 15

session:
  sessions_dir: sessions
  max_history: 50

channels:
  imessage:
    listen_to: "$PHONE"
    poll_interval: 3
```

Sessions are stored as JSON files in `sessions/` (gitignored) and persist conversation history across interactions.

## Fetchers

### Weather

Uses [wttr.in](https://wttr.in) - no API key required.

```yaml
weather:
  image: fetcher-weather:latest
  args:
    location: denver   # city name or coordinates
```

### Google Calendar

Requires a one-time OAuth setup:

```bash
# 1. Create GCP project, enable Calendar API, download OAuth credentials
# 2. Run the setup script (--encrypt auto-encrypts and deletes plaintext)
python scripts/setup-google-oauth.py gcal --encrypt
```

The fetcher uses a read-only scope (`calendar.readonly`) and authenticates with a refresh token.

### Gmail

Reads emails matching a Gmail search query. Requires a one-time OAuth setup:

```bash
# Same GCP project — enable the Gmail API
python scripts/setup-google-oauth.py gmail --encrypt
```

The fetcher uses a read-only scope (`gmail.readonly`). Configuration:

```yaml
gmail:
  image: fetcher-gmail:latest
  secrets: secrets/gmail.env.enc
  args:
    query: "is:unread newer_than:1d"   # Gmail search syntax
    max_results: "25"                   # max messages to fetch
    full_body: "false"                  # set "true" to include decoded body text
```

When `full_body` is enabled, the fetcher decodes MIME parts (prefers `text/plain`, falls back to HTML stripping via BeautifulSoup).

### Google Calendar (Write)

Creates calendar events. Requires a one-time OAuth setup with the `calendar.events` scope:

```bash
python scripts/setup-google-oauth.py gcal_write --encrypt
```

Configuration:

```yaml
gcal_write:
  image: fetcher-gcal-write:latest
  secrets: secrets/gcal_write.env.enc
  args:
    summary: "Team standup"
    start: "2025-01-15T09:00:00-07:00"   # ISO 8601
    end: "2025-01-15T09:30:00-07:00"
    description: "Daily sync"              # optional
    location: "Room 42"                    # optional
```

### Gmail (Send)

Sends an email. Requires a one-time OAuth setup with the `gmail.send` scope:

```bash
python scripts/setup-google-oauth.py gmail_send --encrypt
```

Configuration:

```yaml
gmail_send:
  image: fetcher-gmail-send:latest
  secrets: secrets/gmail_send.env.enc
  args:
    to: "recipient@example.com"
    subject: "Daily report"
    body: "Here is today's summary..."
```

### Gmail (Modify)

Modifies, trashes, or permanently deletes Gmail messages. Requires a one-time OAuth setup with the `gmail.modify` scope:

```bash
python scripts/setup-google-oauth.py gmail_modify --encrypt
```

Configuration:

```yaml
gmail_modify:
  image: fetcher-gmail-modify:latest
  secrets: secrets/gmail_modify.env.enc
  args:
    action: "modify"              # modify, trash, or delete
    message_id: "18f1a2b3c4d5e6f" # Gmail message ID
    add_labels: "STARRED"         # comma-separated label IDs (modify only)
    remove_labels: "UNREAD,INBOX" # comma-separated label IDs (modify only)
```

### Google Drive (Read)

Lists and reads files from Google Drive. Requires a one-time OAuth setup with the `drive.readonly` scope:

```bash
python scripts/setup-google-oauth.py drive --encrypt
```

Configuration:

```yaml
drive:
  image: fetcher-drive:latest
  secrets: secrets/drive.env.enc
  args:
    query: "mimeType='application/pdf'"   # Drive search query (optional)
    max_results: "20"
```

### Google Drive (Write)

Uploads a file to Google Drive. Requires a one-time OAuth setup with the `drive.file` scope:

```bash
python scripts/setup-google-oauth.py drive_write --encrypt
```

Configuration:

```yaml
drive_write:
  image: fetcher-drive-write:latest
  secrets: secrets/drive_write.env.enc
  args:
    name: "report.txt"
    content: "File contents here..."
    mime_type: "text/plain"      # optional, defaults to text/plain
    folder_id: ""                # optional Drive folder ID
```

## Google Services Setup

All Google services use the same OAuth2 client credentials (client ID + client secret from a single GCP project) but obtain **separate refresh tokens** with different scopes. This means:

- One `client_secret.json` file works for all services
- Each service gets its own `.env` file (e.g. `secrets/gcal.env`, `secrets/gmail_send.env`)
- Each refresh token is scoped to a single API permission
- Revoking one token doesn't affect the others

```bash
# Set up all services at once (encrypts automatically)
python scripts/setup-google-oauth.py --all

# Or set up specific services
python scripts/setup-google-oauth.py gmail gcal

# Encrypt with age after each OAuth flow
python scripts/setup-google-oauth.py gmail --encrypt

# Encrypt all existing .env files in secrets/ (no OAuth flow)
python scripts/setup-google-oauth.py --encrypt-all
```

Available services: `gcal`, `gcal_write`, `gmail`, `gmail_send`, `gmail_modify`, `drive`, `drive_write`.

## Secrets Management

Secrets are encrypted at rest using [age](https://github.com/FiloSottile/age). The Python side uses [pyrage](https://pypi.org/project/pyrage/) for decryption.

```bash
# Generate an age key pair (one-time)
mkdir -p ~/.age
age-keygen -o ~/.age/key.txt 2> ~/.age/key.pub

# Encrypt a .env file
./scripts/encrypt-secret.sh secrets/anthropic.env

# The decryption key path defaults to ~/.age/key.txt
# Override with AGE_IDENTITY environment variable
export AGE_IDENTITY=/path/to/key.txt
```

The `.env` format supports `KEY=value`, quoted values, and comments:

```
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_CREDENTIALS_JSON='{"refresh_token": "...", "client_id": "...", "client_secret": "..."}'
```

## Container Mode

For production use, fetchers and the LLM runner execute in isolated Docker containers with restricted capabilities:

```bash
# Build container images
docker build -t fetcher-weather:latest fetchers/weather/
docker build -t fetcher-gcal:latest fetchers/gcal/
docker build -t fetcher-gcal-write:latest fetchers/gcal_write/
docker build -t fetcher-gmail:latest fetchers/gmail/
docker build -t fetcher-gmail-send:latest fetchers/gmail_send/
docker build -t fetcher-gmail-modify:latest fetchers/gmail_modify/
docker build -t fetcher-drive:latest fetchers/drive/
docker build -t fetcher-drive-write:latest fetchers/drive_write/
docker build -t llm-runner:latest llm/

# Run with containers
./runner.py --containers run morning_briefing
```

Containers run with:
- `--read-only` filesystem
- `--cap-drop=ALL`
- `--security-opt=no-new-privileges`
- Memory and CPU limits
- Only the secrets each container needs

## CLI Reference

```
./runner.py <command> [options]

Commands:
  run <task>        Run a task immediately
  schedule          Start cron scheduler for all tasks
  list              List available tasks
  validate <task>   Validate a task YAML file
  chat              Interactive CLI chat with agent
  listen            Listen for iMessages and respond
  serve             Listen for iMessages + run scheduler

Options:
  -v, --verbose       Enable verbose/debug output
  --containers        Run fetchers and LLM in Docker containers
  --tasks-dir PATH    Tasks directory (default: tasks/)
  --agent-config PATH Path to agent.yaml (default: agent.yaml)

Run options:
  --dry             Render prompt only, skip LLM and output
```

## Project Structure

```
creel/
├── runner.py              # CLI entry point
├── agent.yaml             # Global agent config (tools, channels, sessions)
├── pyproject.toml
├── .python-version        # pyenv Python version pin (3.12)
├── taskrunner/
│   ├── models.py          # Pydantic models (tasks, tools, agent config)
│   ├── orchestrator.py    # Core loop: fetch -> LLM -> output (simple + agent)
│   ├── agent.py           # Agent loop: LLM -> tool_use -> execute -> loop
│   ├── tools.py           # Tool definitions + fetcher execution bridge
│   ├── session.py         # JSON file-backed conversation sessions
│   ├── chat.py            # Chat server (channels + sessions + agent)
│   ├── channels/
│   │   ├── __init__.py    # Channel ABC
│   │   ├── stdin.py       # Interactive CLI channel
│   │   └── imessage.py    # iMessage channel (polls chat.db)
│   ├── scheduler.py       # APScheduler cron integration
│   ├── llm.py             # Anthropic API calls (direct + container + tools)
│   ├── outputs.py         # Output routing (iMessage, stdout, file)
│   └── secrets.py         # age encryption/decryption
├── fetchers/
│   ├── weather/           # wttr.in fetcher + Dockerfile
│   ├── gcal/              # Google Calendar (read) fetcher + Dockerfile
│   ├── gcal_write/        # Google Calendar (write) fetcher + Dockerfile
│   ├── gmail/             # Gmail (read) fetcher + Dockerfile
│   ├── gmail_send/        # Gmail (send) fetcher + Dockerfile
│   ├── gmail_modify/      # Gmail (modify) fetcher + Dockerfile
│   ├── drive/             # Google Drive (read) fetcher + Dockerfile
│   └── drive_write/       # Google Drive (write) fetcher + Dockerfile
├── guardian/
│   ├── __init__.py        # Guardian class (screen_input, validate_action)
│   ├── types.py           # Data models and config
│   ├── fast_classifier.py # DeBERTa/ONNX prompt-injection detector
│   ├── llm_judge.py       # Haiku-based secondary judge
│   ├── policy.py          # YAML policy engine (allow/review/deny)
│   └── audit.py           # Privacy-preserving JSONL audit logger
├── policies/
│   └── default.yaml       # Default tool action policies
├── llm/                   # Containerized LLM runner + Dockerfile
├── tasks/                 # Task definitions (YAML)
├── sessions/              # Conversation sessions (gitignored)
├── secrets/               # Encrypted .env files (gitignored)
├── scripts/
│   ├── encrypt-secret.sh    # age encryption helper
│   └── setup-google-oauth.py# Google OAuth setup (gcal, gmail)
└── tests/
```

## Development

Requires [pyenv](https://github.com/pyenv/pyenv) and [uv](https://github.com/astral-sh/uv).

```bash
# First-time setup
pyenv install 3.12.12   # .python-version pins this
uv venv                 # creates .venv using pyenv's Python
source .venv/bin/activate
uv pip install -e ".[dev]"

# Run tests
pytest
```
