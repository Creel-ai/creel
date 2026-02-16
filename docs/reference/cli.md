# CLI Reference

```bash
creel <command> [options]
```

## Commands

| Command | Description |
|---------|-------------|
| `run <task>` | Run a task immediately |
| `schedule` | Start cron scheduler for all tasks |
| `list` | List available tasks |
| `validate <task>` | Validate a task YAML file |
| `daemon ...` | Manage daemon lifecycle (`start`, `stop`, `status`, `install`, `uninstall`) |
| `attach` | Attach TUI client to running daemon |
| `send <message>` | Send one message via daemon API |
| `audit` | Query the guardian audit log |

## Global Options

| Option | Description |
|--------|-------------|
| `-v`, `--verbose` | Enable verbose/debug output |
| `--containers` | Run executors/LLM in Docker containers (all commands) |
| `--tasks-dir PATH` | Tasks directory (default: `tasks/`) |
| `--agent-config PATH` | Path to agent.yaml (default: `agent.yaml`) |
| `--json-logs` | Output structured JSON log lines (for production) |
| `--no-judge` | Disable the LLM judge to save API calls during development |

## Daemon Commands

| Command | Description |
|---------|-------------|
| `creel daemon start` | Start daemon (`launchd` if installed, otherwise detached process) |
| `creel daemon stop` | Stop daemon (or unload `launchd` service) |
| `creel daemon status` | Show daemon process, API, and `launchd` status |
| `creel daemon install` | Install daemon as a persistent `launchd` service (macOS) |
| `creel daemon uninstall` | Uninstall daemon `launchd` service (macOS) |

### Shared Daemon Options

| Option | Description |
|--------|-------------|
| `--socket-path PATH` | Unix socket path (default: `/tmp/creel-daemon.sock`) |
| `--pid-file PATH` | PID file path (default: `/tmp/creel-daemon.pid`) |
| `--log-file PATH` | Daemon log file (default: `/tmp/creel-daemon.log`) |
| `--channel TYPE` | Channel plugin: `none`, `imessage`, `bluebubbles` |
| `--no-scheduler` | Disable scheduler in daemon runtime |
| `--wait-seconds N` | Seconds to wait for daemon health check |
| `--label NAME` | `launchd` service label (default: `com.creel.daemon`) |
| `--plist-path PATH` | `launchd` plist path |

## Attach Options

| Option | Description |
|--------|-------------|
| `--sender-id ID` | Sender ID/session namespace (default: `cli`) |
| `--new` | Start and attach to a new session |
| `--resume ID` | Attach and resume a specific session |
| `--socket-path PATH` | Unix socket path (default: `/tmp/creel-daemon.sock`) |
| `--timeout N` | Request timeout in seconds |

## Send Options

| Option | Description |
|--------|-------------|
| `--sender-id ID` | Sender ID/session namespace (default: `cli`) |
| `--session-id ID` | Resume and send into a specific session |
| `--socket-path PATH` | Unix socket path (default: `/tmp/creel-daemon.sock`) |
| `--timeout N` | Request timeout in seconds |
| `--stream` | Stream response events from daemon SSE endpoint |

## Run Options

| Option | Description |
|--------|-------------|
| `--dry` | Render prompt only, skip LLM and output |

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
