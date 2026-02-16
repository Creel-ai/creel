# Container Mode

For production use, executors and the LLM runner execute in isolated Docker containers with restricted capabilities. The `--containers` flag works with all commands — scheduled tasks, one-off runs, and agent mode (chat, listen, serve).

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
docker build -t llm-runner:latest llm/
```

## Running with Containers

```bash
# Run a task with containers
./runner.py --containers run morning_briefing

# Agent mode with containerized executors
./runner.py --containers chat
./runner.py --containers listen
./runner.py --containers serve

# Scheduler with containers
./runner.py --containers schedule
```

## Security Flags

Containers run with:

- `--read-only` filesystem
- `--cap-drop=ALL`
- `--security-opt=no-new-privileges`
- Memory and CPU limits (`256m`, `0.5` CPU)
- 60-second timeout
- Only the secrets each container needs

## Agent Mode + Containers

In agent mode, the agent loop runs on the host while each tool call (executor) executes in its own isolated container. This preserves the trust boundary — the LLM never sees credentials, and executor code runs sandboxed.
