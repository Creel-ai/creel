# CLI Reference

```bash
creel <command> [options]
```

## Commands

| Command | Description |
|---------|-------------|
| `init` | Initialize `~/.creel/` with interactive wizard or CLI flags |
| `run <task>` | Run a task immediately |
| `schedule` | Start cron scheduler for all tasks |
| `list` | List available tasks |
| `validate <task>` | Validate a task YAML file |
| `daemon ...` | Manage daemon lifecycle (`start`, `stop`, `status`, `install`, `uninstall`) |
| `attach` | Attach TUI client to running daemon |
| `send <message>` | Send one message via daemon API |
| `usage` | Show current LLM usage against rate limits |
| `limits override` | Temporarily bypass rate limits |
| `doctor` | Check system health and dependencies |
| `reload` | Reload agent configuration without restarting |
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

## Init Options

```bash
creel init [options]
```

By default, `creel init` launches an interactive wizard that validates credentials inline and encrypts secrets via age. Use `--non-interactive` with explicit flags for scripted setups.

| Option | Description |
|--------|-------------|
| `--force` | Overwrite existing files |
| `--migrate` | Copy existing repo-based config into `~/.creel/` |
| `--repo-root PATH` | Repo root to migrate from (default: current directory) |
| `--non-interactive` | Skip interactive wizard, use CLI flags below |
| `--provider TYPE` | LLM provider: `anthropic`, `openai`, `ollama` |
| `--api-key KEY` | LLM API key |
| `--model NAME` | LLM model name (defaults per provider) |
| `--channel TYPE` | Channel: `telegram`, `imessage`, `none` |
| `--bot-token TOKEN` | Telegram bot token |
| `--allowed-senders LIST` | Comma-separated list of allowed Telegram sender usernames |
| `--enable-media` | Enable media processing (images, voice) |
| `--no-guardian` | Disable guardian security pipeline |

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
| `--socket-path PATH` | Unix socket path (default: `~/.creel/daemon.sock`) |
| `--pid-file PATH` | PID file path (default: `~/.creel/daemon.pid`) |
| `--log-file PATH` | Daemon log file (default: `~/.creel/daemon.log`) |
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
| `--socket-path PATH` | Unix socket path (default: `~/.creel/daemon.sock`) |
| `--timeout N` | Request timeout in seconds |

## Send Options

| Option | Description |
|--------|-------------|
| `--sender-id ID` | Sender ID/session namespace (default: `cli`) |
| `--session-id ID` | Resume and send into a specific session |
| `--socket-path PATH` | Unix socket path (default: `~/.creel/daemon.sock`) |
| `--timeout N` | Request timeout in seconds |
| `--stream` | Stream response events from daemon SSE endpoint |

## Run Options

| Option | Description |
|--------|-------------|
| `--dry` | Render prompt only, skip LLM and output |

## Usage Options

```bash
creel usage [options]
```

Shows current LLM usage against configured rate limits.

| Option | Description |
|--------|-------------|
| `--history` | Show usage history by day |
| `--days N` | Number of days to show in history (default: 7) |

Example output:

```
Current LLM Usage:
  Requests (last minute): 3 / 30
  Requests (last hour):   47 / 500
  Tokens (today):         125,430 / 1,000,000
  Cost (today):           $0.4821 / $10.00
```

## Limits Override

```bash
creel limits override --duration <DURATION>
```

Temporarily bypass all rate limits. Duration accepts `h`, `m`, or `s` suffixes (e.g., `1h`, `30m`, `60s`).

!!! warning
    Overrides disable **all** rate limits for the specified duration. Cost caps will not be enforced.

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
