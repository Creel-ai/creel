# LLM Task Runner

A secure, scheduled task runner that separates credential-bearing data fetching from LLM processing. Designed for predictable, recurring tasks (morning briefings, weather summaries) where full agentic autonomy is unnecessary and the security risk isn't worth it.

## Why

Agentic LLM systems give the model access to tools, credentials, and untrusted input all at once. For scheduled tasks with known data sources and fixed output destinations, that's a bad trade-off. This project splits the problem:

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
  to: "+1XXXXXXXXXX"

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
# 2. Run the setup script
python scripts/setup-google-oauth.py gcal

# 3. Encrypt the credentials
./scripts/encrypt-secret.sh secrets/gcal.env

# 4. Delete plaintext
rm secrets/gcal.env
```

The fetcher uses a read-only scope (`calendar.readonly`) and authenticates with a refresh token.

### Gmail

Reads emails matching a Gmail search query. Requires a one-time OAuth setup:

```bash
# 1. Same GCP project — enable the Gmail API
# 2. Run the setup script (uses the same client_secret.json)
python scripts/setup-google-oauth.py gmail

# 3. Encrypt the credentials
./scripts/encrypt-secret.sh secrets/gmail.env

# 4. Delete plaintext
rm secrets/gmail.env
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
python scripts/setup-google-oauth.py gcal_write
./scripts/encrypt-secret.sh secrets/gcal_write.env
rm secrets/gcal_write.env
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
python scripts/setup-google-oauth.py gmail_send
./scripts/encrypt-secret.sh secrets/gmail_send.env
rm secrets/gmail_send.env
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
python scripts/setup-google-oauth.py gmail_modify
./scripts/encrypt-secret.sh secrets/gmail_modify.env
rm secrets/gmail_modify.env
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
python scripts/setup-google-oauth.py drive
./scripts/encrypt-secret.sh secrets/drive.env
rm secrets/drive.env
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
python scripts/setup-google-oauth.py drive_write
./scripts/encrypt-secret.sh secrets/drive_write.env
rm secrets/drive_write.env
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

Use `scripts/setup-google-oauth.py <service>` to run the OAuth flow for each service. Available services: `gcal`, `gcal_write`, `gmail`, `gmail_send`, `gmail_modify`, `drive`, `drive_write`.

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

Options:
  -v, --verbose     Enable verbose/debug output
  --containers      Run fetchers and LLM in Docker containers
  --tasks-dir PATH  Tasks directory (default: tasks/)

Run options:
  --dry             Render prompt only, skip LLM and output
```

## Project Structure

```
llm-taskrunner/
├── runner.py              # CLI entry point
├── pyproject.toml
├── .python-version        # pyenv Python version pin (3.12)
├── taskrunner/
│   ├── models.py          # Task YAML parsing + Pydantic validation
│   ├── orchestrator.py    # Core loop: fetch -> LLM -> output
│   ├── scheduler.py       # APScheduler cron integration
│   ├── llm.py             # Anthropic API calls (direct + container)
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
├── llm/                   # Containerized LLM runner + Dockerfile
├── tasks/                 # Task definitions (YAML)
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
