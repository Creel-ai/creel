# Dev Session

The dev session executor provides a containerized development environment with long-lived process management. Unlike the standard `exec` executor, dev sessions persist across tool calls, allowing background processes and interactive workflows.

!!! note
    This executor only works in container mode. It is not available in subprocess/development mode.

## Tools

### `dev_exec`

Run a command in the dev container.

```json
{
  "tool": "dev_exec",
  "args": {
    "command": "python app.py",
    "background": true,
    "workdir": "/workspace",
    "timeout": 300
  }
}
```

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `command` | Yes | — | Command to execute |
| `background` | No | `false` | Run in background, returns a session ID |
| `workdir` | No | Container default | Working directory |
| `timeout` | No | `300` | Timeout in seconds (foreground only) |

### `dev_process`

Manage a running background process.

```json
{"tool": "dev_process", "args": {"session_id": "abc123", "action": "log"}}
```

| Parameter | Required | Description |
|-----------|----------|-------------|
| `session_id` | Yes | Session ID from `dev_exec` |
| `action` | Yes | `log`, `poll`, `write`, or `kill` |
| `limit` | No | Max log lines (default 100) |
| `offset` | No | Line offset (default 0) |
| `data` | No | Stdin data (for `write` action) |

### `dev_sessions`

List all active sessions. No parameters.

## Configuration

In `agent.yaml`:

```yaml
tools:
  dev_session:
    writable: true
    memory: 512m
    cpus: 1.0
    tmpfs_size: 256M
    timeout: 300
    network: true
```
