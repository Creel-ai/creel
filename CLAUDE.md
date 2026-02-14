# Creel

Secure LLM task runner and personal AI assistant. Separates credential-bearing data fetching from LLM processing so the LLM never sees credentials.

## Development Setup

- Python 3.12 managed by **pyenv** (see `.python-version`)
- Package management via **uv**
- Virtual environment at `.venv`

```bash
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

For guardian (prompt-injection detection) features:

```bash
uv pip install -e ".[dev,guardian]"
```

## Running Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

Coverage is configured automatically via `pyproject.toml` (`--cov=taskrunner --cov=guardian --cov-report=term-missing`).

## Project Structure

- `taskrunner/` — Core orchestration, agent loop, LLM calls, session management, tool execution, output routing, channels (stdin, iMessage)
- `guardian/` — Security pipeline: fast classifier (DeBERTa/ONNX), LLM judge (Haiku), YAML policy engine, audit logging
- `fetchers/` — Isolated data fetchers (weather, gcal, gmail, drive), each with minimal OAuth scopes
- `tasks/` — Task definitions in YAML (morning briefing, weather, email digest/triage)
- `policies/` — Tool access policy rules (allow/review/deny via fnmatch)
- `tests/` — Pytest suite covering all modules
- `runner.py` — CLI entry point (subcommands: run, schedule, chat, listen, serve, validate, list)
- `agent.yaml` — Global agent configuration (tools, channels, sessions, guardian)

## Key Conventions

- Configuration is YAML-based (`agent.yaml`, `tasks/*.yaml`, `policies/default.yaml`)
- Data models use **Pydantic v2**
- Secrets encrypted with **age** (decrypted via `pyrage`); decryption key at `~/.age/key.txt`
- Fetchers run in isolated Docker containers in production (`--read-only`, `--cap-drop=ALL`, memory/CPU limits) or as subprocesses in development
- All async code uses `pytest-asyncio` for testing
