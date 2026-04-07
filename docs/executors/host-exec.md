# Host Exec

The host exec executor runs commands on the host machine via the bridge server. It supports foreground and background execution with process management, similar to [Dev Session](dev-session.md) but running on the host instead of in a container.

## Requirements

- Bridge service running (`bridge.enabled: true` in `agent.yaml`)

## Tools

### `host_exec`

Run a command on the host.

```json
{
  "tool": "host_exec",
  "args": {
    "command": "ls -la /tmp",
    "timeout": 60,
    "env": {"MY_VAR": "value"}
  }
}
```

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `command` | Yes | — | Command to execute |
| `background` | No | `false` | Run in background |
| `workdir` | No | — | Working directory |
| `timeout` | No | `300` | Timeout in seconds |
| `env` | No | — | Environment variables (JSON object) |

### `host_process`

Manage a running background process. Same interface as [`dev_process`](dev-session.md#dev_process).

### `host_sessions`

List all active host sessions. No parameters.

## Security

Environment variables passed via `env` are validated. The following patterns are blocked to prevent injection attacks:

- Library injection: `LD_PRELOAD`, `LD_LIBRARY_PATH`, `DYLD_*`
- Shell injection: `BASH_ENV`, `ENV`, `CDPATH`
- Interpreter injection: `PYTHONPATH`, `NODE_OPTIONS`, `PERL5OPT`, `RUBYOPT`
- Token leakage: `BRIDGE_TOKEN_*`, `BASH_FUNC_*`

**Bridge endpoints**: `/exec`, `/process`, `/sessions`
