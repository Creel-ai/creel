# Executors

Executors are isolated, stateless data fetchers that run with minimal credentials. Each executor performs a single function (read email, check weather, search the web) and returns JSON to the orchestrator. The LLM never sees executor credentials.

## Security Model

| Executor | Credentials | What it can't access |
|----------|------------|---------------------|
| [weather](weather.md) | None | LLM, other credentials |
| [gcal / gcal_write](google-calendar.md) | Google OAuth token (scoped) | LLM, other credentials |
| [gmail_readonly / gmail_send / gmail_modify](gmail.md) | Google OAuth token (scoped) | LLM, other credentials |
| [drive / drive_write](google-drive.md) | Google OAuth token (scoped) | LLM, other credentials |
| [bluebubbles / imessage_bridge](imessage.md) | BlueBubbles API / Bridge token | LLM, other credentials |
| [apple_notes / apple_reminders / things](apple-apps.md) | Bridge HTTP token (scoped) | LLM, other credentials |
| [brave_search / fetch_url](web.md) | Brave API key / None | LLM, other credentials |
| [exec](exec.md) | Host filesystem (mounted paths only) | LLM, other credentials |

## How Executors Run

- **Development**: Executors run as subprocesses with secrets injected as environment variables
- **Production**: Each executor runs in its own Docker container with `--read-only`, `--cap-drop=ALL`, memory/CPU limits, and a 60-second timeout

See [Container Mode](../deployment/containers.md) for production deployment details.

## Executor Output

All executors output JSON to stdout. The orchestrator collects this output and injects it into the prompt template using `{executor_name}` placeholders.
