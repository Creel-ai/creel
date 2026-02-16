# CLI Reference

```
./runner.py <command> [options]
```

## Commands

| Command | Description |
|---------|-------------|
| `run <task>` | Run a task immediately |
| `schedule` | Start cron scheduler for all tasks |
| `list` | List available tasks |
| `validate <task>` | Validate a task YAML file |
| `chat` | Interactive CLI chat with agent |
| `listen` | Listen for messages and respond |
| `serve` | Listen for messages + run scheduler |
| `bridge` | Start the host bridge server |
| `audit` | Query the guardian audit log |

## Global Options

| Option | Description |
|--------|-------------|
| `-v`, `--verbose` | Enable verbose/debug output |
| `--containers` | Run executors/LLM in Docker containers (all commands) |
| `--tasks-dir PATH` | Tasks directory (default: `tasks/`) |
| `--agent-config PATH` | Path to agent.yaml (default: `agent.yaml`) |
| `--simple` | Use simple stdin/stdout mode instead of TUI |
| `--json-logs` | Output structured JSON log lines (for production) |
| `--no-judge` | Disable the LLM judge to save API calls during development |

## Run Options

| Option | Description |
|--------|-------------|
| `--dry` | Render prompt only, skip LLM and output |

## Chat Options

| Option | Description |
|--------|-------------|
| `--new` | Start a new session (don't resume the active one) |
| `--resume ID` | Resume a specific session by ID |
| `--list-sessions` | List sessions and exit |

## Listen/Serve Options

| Option | Description |
|--------|-------------|
| `--channel TYPE` | Channel to listen on: `imessage` (default) or `bluebubbles` |

## Audit Options

| Option | Description |
|--------|-------------|
| `--tail N` | Show last N entries (default: 20) |
| `--all` | Show all entries |
| `--blocked` | Show only blocked input events |
| `--denied` | Show only denied action events |
| `--event TYPE` | Filter by event type (`screen_input`, `validate_action`, `tool_result`) |
| `--tool NAME` | Filter by tool name |
| `--since DATE` | Show entries since date (`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS`) |
