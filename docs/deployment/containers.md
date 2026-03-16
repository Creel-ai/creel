# Container Mode

For production use, executors and the LLM runner execute in isolated Docker containers with restricted capabilities. The `--containers` flag works with run and schedule commands.

## Building Container Images

```bash
# Build container images
docker build -t executor-weather:latest executors/weather/
docker build -t executor-gcal:latest executors/gcal/
docker build -t executor-gcal-write:latest executors/gcal_write/
docker build -t executor-gmail-readonly:latest executors/gmail_readonly/
docker build -t executor-gmail-send:latest executors/gmail_send/
docker build -t executor-gmail-modify:latest executors/gmail_modify/
docker build -t executor-drive:latest executors/drive/
docker build -t executor-drive-write:latest executors/drive_write/
docker build -t executor-bluebubbles:latest executors/bluebubbles/
docker build -t executor-brave-search:latest executors/brave_search/
docker build -t executor-fetch-url:latest executors/fetch_url/
docker build -t executor-exec-interactive:latest -f executors/exec_interactive/Dockerfile executors/
docker build -t llm-runner:latest llm/
```

## Running with Containers

```bash
# Run a task with containers
creel run morning_briefing --containers

# Scheduler with containers
creel schedule --containers
```

## Security Flags

Containers run with:

- `--read-only` filesystem
- `--cap-drop=ALL`
- `--security-opt=no-new-privileges`
- Memory and CPU limits (`256m`, `0.5` CPU)
- 60-second timeout
- Only the secrets each container needs

## Interactive Sessions

The `exec_interactive` executor uses a one-container-per-session model. Each `start` action creates a new container; subsequent actions route to it by session ID; `close` tears it down. These containers have network access enabled (for SSH, package managers, etc.) and a 5-minute timeout instead of the default 60 seconds.

## Agent Mode + Containers

In agent mode, the agent loop runs on the host while each tool call (executor) executes in its own isolated container. This preserves the trust boundary — the LLM never sees credentials, and executor code runs sandboxed.
