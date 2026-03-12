# Creel

Secure LLM task runner and personal AI assistant. Separates credential-bearing data fetching from LLM processing so the LLM never sees credentials.

## Development Setup

- Python 3.12 managed by **pyenv** (see `.python-version` → 3.12.12)
- Package management via **uv**
- Virtual environment at `.venv`
- Minimum supported Python: 3.11

```bash
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

For guardian (prompt-injection detection) features:

```bash
uv pip install -e ".[dev,guardian]"
```

Other optional extras: `whatsapp`, `encryption`, `browser`, `vision`, `docs`.

## Running Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

- Coverage is configured automatically via `pyproject.toml` (`--cov=creel --cov=guardian --cov=bridge --cov=executors --cov-report=term-missing`)
- Coverage minimum: **72%** (enforced by `fail_under`)
- Smoke tests are excluded by default (`-m 'not smoke'`); run them explicitly with `-m smoke`
- Known CI skip: `test_fast_classifier.py` requires guardian ML deps (transformers/onnxruntime)

Quick validation one-liner:

```bash
.venv/bin/python -m pytest tests/ -x -q
```

## Linting and Type Checking

```bash
# Lint
.venv/bin/python -m ruff check src tests

# Format check
.venv/bin/python -m ruff format --check src tests

# Auto-fix formatting
.venv/bin/python -m ruff format src tests

# Type check
.venv/bin/python -m mypy
```

- **Ruff** rules: `E`, `F`, `I`, `B`, `S`, `UP` — line length 100
- **mypy** configured in `pyproject.toml` with `ignore_missing_imports = true`

## Project Structure

```
src/
├── creel/              Core orchestration package
│   ├── cli.py          CLI entry point (`creel` command)
│   ├── agent.py        Agent loop implementation
│   ├── orchestrator.py Main orchestration logic
│   ├── llm.py          LLM runner/integration
│   ├── models.py       Pydantic v2 data models
│   ├── tools.py        Tool registry and execution
│   ├── session.py      Session management
│   ├── memory.py       Memory management (daily notes + long-term)
│   ├── secrets.py      Secret decryption (age/pyrage)
│   ├── outputs.py      Output routing (iMessage, stdout, etc.)
│   ├── scheduler.py    Task scheduling (APScheduler)
│   ├── prompt_builder.py  Prompt template construction
│   ├── validation.py   Input validation
│   ├── oauth.py        OAuth 2.0 flow handling
│   ├── channels/       Communication channels (iMessage, Telegram, WhatsApp, BlueBubbles, webhook)
│   ├── cron/           Cron scheduler (manager, executor, delivery, store)
│   ├── daemon/         Daemon/service mode with FastAPI REST API
│   ├── services/       Media storage, transcription (Whisper), vision
│   ├── subagents/      Sub-agent executor and manager
│   └── migrations/     Database migrations
├── guardian/            Security pipeline
│   ├── core.py         Main guardian pipeline
│   ├── fast_classifier.py  DeBERTa/ONNX prompt-injection classifier
│   ├── llm_judge.py    LLM-based security judge (Haiku)
│   ├── policy.py       YAML policy engine
│   ├── coherence.py    Input coherence checking
│   ├── credential_scanner.py  Credential detection
│   ├── audit.py        Security audit logging
│   └── drift.py        Input drift detection
├── bridge/             macOS native tools (FastAPI server on :8099)
│   ├── server.py       Bridge server
│   └── browser.py      Browser control
├── executors/          Isolated data executors (27+)
│   ├── weather/        Weather API
│   ├── gcal/           Google Calendar (read-only)
│   ├── gcal_write/     Google Calendar (write)
│   ├── gmail_readonly/ Gmail (read-only)
│   ├── gmail_send/     Gmail (send)
│   ├── gmail_modify/   Gmail (labels, trash)
│   ├── drive/          Google Drive (read-only)
│   ├── drive_write/    Google Drive (write)
│   ├── google_docs/    Google Docs
│   ├── google_sheets/  Google Sheets
│   ├── google_slides/  Google Slides
│   ├── github/         GitHub CLI wrapper
│   ├── git_ops/        Git operations
│   ├── notion/         Notion (read-only)
│   ├── notion_write/   Notion (write)
│   ├── browser/        Playwright browser automation
│   ├── brave_search/   Brave Search API
│   ├── fetch_url/      URL content fetching
│   ├── exec/           Shell command execution (sandboxed)
│   ├── coding/         Development environment
│   ├── file_ops/       File operations
│   ├── apple_notes/    Apple Notes
│   ├── apple_reminders/ Apple Reminders
│   ├── things/         Things 3 task manager
│   ├── imessage_bridge/ iMessage bridge
│   └── bluebubbles/    BlueBubbles (iMessage relay)
└── llm/                Containerized LLM runner with Dockerfile
```

### Other Top-Level Directories

- `tasks/` — Task definitions in YAML with cron schedules (morning briefing, weather, email digest/triage)
- `policies/` — Tool access policy rules (`default.yaml`: allow/review/deny via fnmatch + conditional rules)
- `tests/` — Pytest suite (120+ test files covering all modules)
- `scripts/` — Build scripts, hooks, smoke runner, migration scripts, encryption utilities
- `dashboard/` — Web dashboard for monitoring
- `docs/` — MkDocs documentation site
- `specs/` — Technical design specs (cron, dashboard, streaming, sub-agents, etc.)
- `workspace/` — Runtime workspace with memory files (MEMORY.md, IDENTITY.md, etc.)
- `tools/` — Tool definitions (e.g., `exec.yaml`)
- `secrets/` — Encrypted secrets (age-encrypted `.env.enc` files)
- `docker/` — Docker configurations (WhatsApp bridge)
- `agent.yaml` — Global agent configuration (tools, channels, sessions, guardian)

## Entry Points

- **CLI**: `creel` command → `src/creel/cli.py:main()`
- **Module**: `python -m creel` via `src/creel/__main__.py`
- **Daemon API**: `src/creel/daemon/api.py` (FastAPI — tasks, cron, dashboard, auth, logs, files, config endpoints)
- **Channel plugins**: Registered via `pyproject.toml` entry points (`creel.channels`)

## Git Hooks

Enable shared git hooks (ruff lint + format on pre-commit):

```bash
git config core.hooksPath scripts/hooks
```

The pre-commit hook runs `ruff check` and `ruff format --check` on staged `.py` files only.

**Important:** CI checks lint/format across **all** files (`src tests`), not just staged ones. Before pushing, run a full check to catch pre-existing violations in files you didn't modify:

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
```

## Git Workflow

- **Never commit or push directly to `main`.** Always create a feature branch and open a PR.
- Branch naming: `feat/`, `fix/`, etc. (e.g., `feat/google-workspace-executors`)
- CI runs on push to `main` and on all pull requests

## CI (GitHub Actions)

The `.github/workflows/tests.yml` workflow runs two parallel jobs:

1. **quality**: ruff format check → ruff lint → mypy type check
2. **test**: full pytest suite with `[dev,guardian,vision]` extras

Both use Python 3.12 and uv for dependency management.

## Key Conventions

- **Configuration**: YAML-based (`agent.yaml`, `tasks/*.yaml`, `policies/default.yaml`)
- **Data models**: Pydantic v2 with strict validation
- **Secrets**: Encrypted with **age** (decrypted via `pyrage`); decryption key at `~/.age/key.txt`
- **Async**: All async code uses `pytest-asyncio` for testing
- **HTTP client**: `httpx` (not `requests`)
- **Executors**: Each executor is a self-contained package with its own `Dockerfile`; runs in isolated Docker containers in production (`--read-only`, `--cap-drop=ALL`, `--no-new-privileges`, memory/CPU limits) or as subprocesses in development
- **Channels**: Plugin-based architecture — register via entry points in `pyproject.toml`
- **Security**: Multi-layer Guardian pipeline (fast classifier → policy engine → coherence check → audit log)

## Security Policies

The `policies/default.yaml` defines tool access control:

- **Allow**: Read-only tools (weather, calendar read, email read, search, memory)
- **Review**: Mutating tools (send, upload, create, modify, git push)
- **Auto-approve**: Subset of review tools that skip human approval (reminders, notes, browser, file writes)
- **Deny**: Destructive operations (`delete_*`)
- **Conditional deny** (Tier 1-4): Dangerous shell patterns (rm -rf, reverse shells, fork bombs, pipe injection)
- **Conditional review** (Tier 5-6): Credential exposure, force push, sudo, network access, data exfiltration

## Adding a New Executor

1. Create `src/executors/<name>/` with `__init__.py` and `executor.py`
2. Add a `Dockerfile` for container isolation
3. Define the tool in `agent.yaml` under the `tools:` section
4. Add policy rules in `policies/default.yaml`
5. Wire into `orchestrator.py` dispatch
6. Add tests in `tests/test_<name>_executor.py`
7. No `shell=True` in subprocess calls; no string interpolation in shell commands

## Testing Checklist (from TESTING.md)

Before merging any PR:

1. `pytest tests/ -x -q` — zero new failures
2. New modules import cleanly (no circular imports)
3. New Dockerfiles build without errors
4. Smoke test new features manually
5. Error paths return clear messages (not stack traces)
6. No `shell=True`, no secrets in logs, auth tokens validated
7. Container security flags preserved
