# Ralph Agent Configuration

## Build Instructions

```bash
uv sync
```

## Test Instructions

```bash
uv run python -m pytest tests/ -q --no-header
```

## Run Instructions

```bash
# CLI chat mode
uv run python -m taskrunner chat

# Daemon mode
creel daemon start
```

## Project Structure
- `src/taskrunner/` — core agent loop, CLI, daemon, channels, orchestrator
- `src/executors/` — tool executors (Docker containers or bridge-proxied)
- `src/guardian/` — security pipeline (DeBERTa classifier, LLM judge, policy, coherence)
- `src/bridge/` — host bridge server (FastAPI, macOS-native tools)
- `src/llm/` — LLM container runner
- `tests/` — 1224+ tests
- `policies/` — Guardian YAML policies
- `agent.yaml` — tool definitions and system prompt config

## Branch Workflow
- **Branch protection on main** — all changes must go through PRs
- Always branch from `origin/main`: `git checkout -b <branch> origin/main`
- Push and open PR: `gh pr create --fill`

## Notes
- Entry point: `src/taskrunner/cli.py`
- Global CLI flags (`--containers`, `--simple`) go BEFORE the subcommand
- Content-hash Docker image tags prevent stale executors
