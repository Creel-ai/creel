# Shell Exec

The exec executor provides sandboxed shell command execution. It's designed for running system commands, scripts, and CLI tools.

In development mode, commands run as subprocesses. In production mode with `--containers`, they execute in isolated Docker containers with `--read-only`, `--cap-drop=ALL`, memory/CPU limits, and network isolation.

## Configuration

```yaml
exec:
  args:
    command: "ls -la /workspace"
    workdir: "/workspace"
```

## Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `command` | yes | Shell command to execute |
| `workdir` | no | Working directory for the command |
