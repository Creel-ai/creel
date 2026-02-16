# Shell Exec

The exec executor provides sandboxed shell command execution within isolated Docker containers. It's designed for running system commands, scripts, and CLI tools while maintaining security through filesystem isolation and network restrictions.

## Sandboxing

Commands execute in a minimal Alpine Linux container (`alpine:latest`) with:

- **Network isolation**: `--network=none` by default (no internet access)
- **Filesystem isolation**: Only configured mount points are accessible
- **Minimal base**: Just `bash`, `grep`, `sed`, `awk`, `curl`, `jq` installed
- **Read-only root**: Container filesystem is read-only except for mounted paths

## Configuration

```yaml
exec:
  args:
    command: "ls -la /workspace"
    workdir: "/workspace"
    mounts:
      - path: "/Users/user/docs"
        target: "/workspace"
        mode: "ro"
```

## Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `command` | yes | Shell command to execute |
| `workdir` | no | Working directory inside the container |
| `mounts` | no | List of mount point configurations |

### Mount Configuration

| Field | Description |
|-------|-------------|
| `path` | Host directory path |
| `target` | Container directory path |
| `mode` | Access mode: `ro` (read-only) or `rw` (read-write) |
