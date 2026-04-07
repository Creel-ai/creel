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
| `network ...` | Network traffic monitoring and policy management |
| `monitor ...` | Manage proactive monitors and alerts |
| `pair ...` | Device pairing management (`generate`, `list`, `remove`, `test`) |

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
| `--provider TYPE` | LLM provider: `anthropic`, `openai`, `google`, `ollama` |
| `--api-key KEY` | LLM API key |
| `--model NAME` | LLM model name (defaults per provider) |
| `--channel TYPE` | Channel: `telegram`, `imessage`, `whatsapp`, `none` |
| `--bot-token TOKEN` | Telegram bot token |
| `--allowed-senders LIST` | Comma-separated list of allowed Telegram sender usernames |
| `--enable-media` | Enable media processing (images, voice) |
| `--tools LIST` | Comma-separated list of tools to enable |
| `--no-guardian` | Disable guardian security pipeline |

## Encrypt Command

```bash
creel encrypt <file> [options]
```

Encrypt a plaintext `.env` file with age.

| Option | Description |
|--------|-------------|
| `--recipient PATH` | Path to age public key file (default: `~/.age/key.pub`) |
| `--output PATH` | Custom output path (default: `<file>.enc`) |
| `--delete` | Delete plaintext file after encryption |

## Cron Commands

```bash
creel cron <subcommand> [options]
```

| Subcommand | Description |
|------------|-------------|
| `list` | List all cron jobs |
| `add` | Add a new cron job |
| `edit <id>` | Edit an existing cron job |
| `remove <id>` | Remove a cron job |
| `run <id>` | Trigger a cron job immediately |
| `runs <id>` | Show run history for a cron job |

## Daemon Commands

| Command | Description |
|---------|-------------|
| `creel daemon start` | Start daemon (`launchd` if installed, otherwise detached process) |
| `creel daemon stop` | Stop daemon (or unload `launchd` service) |
| `creel daemon restart` | Restart daemon |
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

## Monitor Commands

```bash
creel monitor <subcommand> [options]
```

| Subcommand | Description |
|------------|-------------|
| `list` | List all monitors |
| `add` | Add a new monitor |
| `add-template <name>` | Add a monitor from a built-in template |
| `templates` | List available templates |
| `enable <id>` | Enable a monitor |
| `disable <id>` | Disable a monitor |
| `remove <id>` | Remove a monitor |
| `run <id>` | Trigger a monitor check immediately |
| `history <id>` | Show run and alert history |

### Monitor Add Options

| Option | Description |
|--------|-------------|
| `--name NAME` | Monitor name (required) |
| `--executor NAME` | Executor to use (required) |
| `--prompt TEXT` | What to check for (required) |
| `--cron EXPR` | Cron expression (e.g., `*/15 * * * *`) |
| `--every N` | Check interval in seconds |
| `--delivery-channel NAME` | Channel for alert delivery |
| `--delivery-url URL` | Webhook URL for alert delivery |
| `--alert-level LEVEL` | `info`, `notice`, or `urgent` (default: `notice`) |
| `--quiet-hours RANGE` | Quiet hours range (e.g., `23:00-07:00`) |
| `--cooldown N` | Dedup cooldown in seconds (default: `3600`) |
| `--tz TIMEZONE` | Timezone (default: `UTC`) |
| `--disabled` | Create in disabled state |

### Monitor History Options

| Option | Description |
|--------|-------------|
| `--type TYPE` | `all`, `runs`, or `alerts` (default: `all`) |
| `--tail N` | Show last N entries (default: `20`) |

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

## Network Commands

```bash
creel network <subcommand> [options]
```

| Subcommand | Description |
|------------|-------------|
| `log` | Show network request audit log |
| `policy` | Show current network policy configuration |
| `allow <domain>` | Check if a domain is allowed by the network policy |
| `block <domain>` | Check if a domain is blocked by the network policy |

### Network Log Options

| Option | Description |
|--------|-------------|
| `--tail N` | Show last N entries (default: 20) |
| `--all` | Show all entries (no tail limit) |
| `--executor NAME` | Filter by executor name |

Example output:

```
[2026-03-15T10:23:01] OK POST api.openai.com executor=gmail_readonly [200]
[2026-03-15T10:23:05] BLOCKED GET evil.pastebin.com executor=fetch_url (domain 'evil.pastebin.com' matches blocked pattern '*.pastebin.com')

2 entries shown.
```

## Pair Commands

```bash
creel pair <subcommand> [options]
```

| Subcommand | Description |
|------------|-------------|
| `generate` | Generate a new pairing code |
| `list` | List all paired devices |
| `remove <device_id>` | Remove a paired device |
| `test <device_id>` | Test connectivity to a paired device |

### Pair Generate Options

| Option | Description |
|--------|-------------|
| `--timeout N` | Pairing timeout in seconds (default: 300) |

See [Device Pairing](../architecture/device-pairing.md) for the full pairing flow and API reference.
