# Quick Start

## Prerequisites

- [pyenv](https://github.com/pyenv/pyenv) — Python version management
- [uv](https://github.com/astral-sh/uv) — Fast Python package manager
- [age](https://github.com/FiloSottile/age) — Secrets encryption

## Installation

```bash
# Set up Python and virtualenv
pyenv install 3.12.12   # if not already installed
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# Optional: required for live ONNX export + classifier smoke tests
uv pip install -e ".[guardian]"
```

## Set Up Secrets Encryption

```bash
# Install age for secrets encryption (one-time)
brew install age
mkdir -p ~/.age
age-keygen -o ~/.age/key.txt 2> ~/.age/key.pub
```

## Your First Task

```bash
# List available tasks
./runner.py list

# Validate a task definition
./runner.py validate weather_check

# Dry run (renders prompt, skips LLM and output)
./runner.py run weather_check --dry

# Full run (requires Anthropic credentials — see Authentication)
./runner.py run weather_check
```

## More Commands

```bash
# Start the cron scheduler
./runner.py schedule

# Interactive CLI chat (agent mode — launches TUI by default)
./runner.py chat

# Simple stdin/stdout chat (no TUI)
./runner.py chat --simple

# Session management
./runner.py chat --list-sessions
./runner.py chat --new           # start fresh session
./runner.py chat --resume <ID>   # resume a specific session

# Listen for iMessages and respond
./runner.py listen

# Listen via BlueBubbles instead of local chat.db
./runner.py listen --channel bluebubbles

# Listen + scheduler (daemon mode)
./runner.py serve

# Query the guardian audit log
./runner.py audit
./runner.py audit --blocked --tail 50
```

## Next Steps

- [Authentication](authentication.md) — Set up Anthropic API credentials
- [Google Services Setup](google-services.md) — Configure Google Calendar, Gmail, and Drive
- [Task Definitions](../configuration/tasks.md) — Write your own tasks
