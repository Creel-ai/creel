# Getting Started

Get Creel running in 5 minutes. By the end you'll have a personal AI agent responding to messages in a rich terminal UI.

## Prerequisites

| Tool | What it's for | Install |
|------|--------------|---------|
| Python 3.12+ | Runtime | `pyenv install 3.12` |
| [uv](https://github.com/astral-sh/uv) | Fast package manager | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| [age](https://github.com/FiloSottile/age) | Secrets encryption | `brew install age` |
| Docker | Container isolation (optional) | [docker.com](https://docs.docker.com/get-docker/) |

## 1. Clone and install

```bash
git clone https://github.com/creel-ai/creel.git
cd creel

# Create virtualenv and install
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

## 2. Set up Anthropic credentials

You need an Anthropic API key or Claude Code auth token:

=== "API Key (simplest)"

    ```bash
    export ANTHROPIC_API_KEY=sk-ant-...
    ```

    Get one at [console.anthropic.com](https://console.anthropic.com/).

=== "Claude Code Token"

    ```bash
    claude setup-token
    export ANTHROPIC_AUTH_TOKEN=sk-ant-oat01-...
    ```

For persistent storage, encrypt it:

```bash
# Set up age encryption (one-time)
mkdir -p ~/.age
age-keygen -o ~/.age/key.txt 2> ~/.age/key.pub

# Encrypt your credentials
echo 'ANTHROPIC_API_KEY=sk-ant-...' > secrets/anthropic.env
./scripts/encrypt-secret.sh secrets/anthropic.env
rm secrets/anthropic.env   # delete plaintext
```

## 3. Run your first task

```bash
# See what tasks are available
creel list

# Dry run — renders the prompt without calling the LLM
creel run weather_check --dry

# Full run
creel run weather_check
```

## 4. Start the daemon

The daemon runs the agent loop, scheduler, and channel plugins in the background:

```bash
# Start the daemon
creel daemon start

# Check it's running
creel daemon status
```

## 5. Chat with your agent

Attach a rich terminal UI to the running daemon:

```bash
creel attach
```

You'll get a full TUI with markdown rendering, status bar, multi-line input, and streaming responses. Type `/help` to see available commands.

Or send a one-shot message:

```bash
creel send "What's the weather today?"

# Stream the response as it generates
creel send "Summarize my calendar" --stream
```

## 6. Run on startup (optional)

Install as a macOS launchd service so Creel starts automatically:

```bash
creel daemon install
```

To remove: `creel daemon uninstall`

## What's next?

<div class="grid cards" markdown>

-   **Configure tools**

    ---

    Define tools in `agent.yaml` — calendar, email, web search, and more. Each runs in its own isolated Docker container.

    [:octicons-arrow-right-24: Agent Configuration](../configuration/agent-config.md)

-   **Set up Google services**

    ---

    Connect Google Calendar, Gmail, and Drive with OAuth credentials.

    [:octicons-arrow-right-24: Google Services](google-services.md)

-   **Enable container isolation**

    ---

    Run executors in Docker for per-tool security boundaries.

    [:octicons-arrow-right-24: Container Mode](../deployment/containers.md)

-   **Understand the security model**

    ---

    Learn how the Guardian pipeline protects against prompt injection.

    [:octicons-arrow-right-24: Architecture](../architecture/overview.md)

</div>
