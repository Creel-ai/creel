# Container Mode

For production use, executors and the LLM runner execute in isolated Docker containers with restricted capabilities. The `--containers` flag works with run and schedule commands.

## Building Container Images

```bash
# Build the shared base image first
docker build -t creel-executor-base:latest src/executors/base/

# Build executor images (context is src/executors/, -f points to each Dockerfile)
docker build -t executor-weather:latest -f src/executors/weather/Dockerfile src/executors/
docker build -t executor-gcal:latest -f src/executors/gcal/Dockerfile src/executors/
docker build -t executor-gcal-write:latest -f src/executors/gcal_write/Dockerfile src/executors/
docker build -t executor-gmail-readonly:latest -f src/executors/gmail_readonly/Dockerfile src/executors/
docker build -t executor-gmail-send:latest -f src/executors/gmail_send/Dockerfile src/executors/
docker build -t executor-gmail-modify:latest -f src/executors/gmail_modify/Dockerfile src/executors/
docker build -t executor-drive:latest -f src/executors/drive/Dockerfile src/executors/
docker build -t executor-drive-write:latest -f src/executors/drive_write/Dockerfile src/executors/
docker build -t executor-google-docs:latest -f src/executors/google_docs/Dockerfile src/executors/
docker build -t executor-google-sheets:latest -f src/executors/google_sheets/Dockerfile src/executors/
docker build -t executor-google-slides:latest -f src/executors/google_slides/Dockerfile src/executors/
docker build -t executor-brave-search:latest -f src/executors/brave_search/Dockerfile src/executors/
docker build -t executor-fetch-url:latest -f src/executors/fetch_url/Dockerfile src/executors/
docker build -t executor-notion:latest -f src/executors/notion/Dockerfile src/executors/
docker build -t executor-notion-write:latest -f src/executors/notion_write/Dockerfile src/executors/
docker build -t executor-github:latest -f src/executors/github/Dockerfile src/executors/
docker build -t executor-tts:latest -f src/executors/tts/Dockerfile src/executors/
docker build -t executor-exec:latest -f src/executors/exec/Dockerfile src/executors/
docker build -t executor-exec-interactive:latest -f src/executors/exec_interactive/Dockerfile src/executors/
docker build -t executor-coding:latest -f src/executors/coding/Dockerfile src/executors/
docker build -t executor-dev-session:latest -f src/executors/dev_session/Dockerfile src/executors/
docker build -t executor-file-ops:latest -f src/executors/file_ops/Dockerfile src/executors/
# ... plus bridge-based executors (clipboard, host_exec, apple_*, things, imessage)

# Build the LLM runner
docker build -t llm-runner:latest src/llm/
```

!!! note
    In practice, the container runtime auto-builds images on first use and tags them with a content hash for cache invalidation. Manual builds are only needed for pre-warming or CI.

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
