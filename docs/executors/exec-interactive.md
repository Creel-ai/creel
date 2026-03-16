# Interactive Shell Exec

The `exec_interactive` executor provides interactive terminal sessions (PTY) inside isolated Docker containers. It supports SSH, REPLs, editors, and other programs that require a pseudo-terminal with input/output streaming.

Unlike the non-interactive [exec](exec.md) executor which runs a single command and returns output, `exec_interactive` maintains a persistent session with a start/interact/close lifecycle.

## Containerization

Each `start` action spins up a dedicated Docker container running `pty_runner.py` (JSON-over-stdio protocol). Subsequent actions (`send_input`, `read_output`, etc.) route to the container by `session_id`. The `close` action tears down the container.

Containers run with:

- `--read-only` filesystem
- `--cap-drop=ALL`
- `--security-opt=no-new-privileges`
- Memory and CPU limits
- `--tmpfs /tmp:rw,noexec,nosuid`
- Network access enabled (required for SSH, package managers, etc.)

## Actions

| Action | Description |
|--------|-------------|
| `start` | Start a new interactive session with a command |
| `send_input` | Send keystrokes/text to the session (include `\n` for Enter) |
| `read_output` | Read available output from the session |
| `resize` | Resize the terminal dimensions |
| `close` | Close the session and clean up resources |
| `info` | Get metadata about a session |
| `list_sessions` | List all active sessions |
| `get_io_log` | Get the full I/O audit log for a session |

## Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `action` | yes | One of the actions listed above |
| `command` | for `start` | Shell command to run in the PTY |
| `session_id` | for most actions | Session ID returned by `start` |
| `input` | for `send_input` | Text to send (include `\n` for Enter) |
| `timeout` | no | Hard timeout in seconds (default 300) |
| `cols` | no | Terminal width in columns (default 120) |
| `rows` | no | Terminal height in rows (default 40) |
| `read_timeout` | no | How long to wait for output in seconds (default 10) |

## Security

- **Policy**: `exec_interactive` requires explicit review approval (listed under `review` in `policies/default.yaml`)
- **Deny patterns**: Dangerous commands (`rm -rf`, `bash -i`, reverse shells, fork bombs, etc.) are blocked by `deny_when` rules
- **Review patterns**: Risky but legitimate commands (`sudo`, credential exposure, force push) are flagged for review
- **Audit logging**: All I/O is logged by the Guardian audit system with session lifecycle events
- **Containerization**: Even if malicious input is sent via `send_input`, it executes inside a sandboxed container with dropped capabilities and a read-only filesystem
