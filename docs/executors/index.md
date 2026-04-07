# Executors

Executors are isolated, stateless data fetchers that run with minimal credentials. Each executor performs a single function (read email, check weather, search the web) and returns JSON to the orchestrator. The LLM never sees executor credentials.

## Security Model

| Executor | Credentials | What it can't access |
|----------|------------|---------------------|
| [weather](weather.md) | None | LLM, other credentials |
| [gcal / gcal_write](google-calendar.md) | Google OAuth token (scoped) | LLM, other credentials |
| [gmail_readonly / gmail_send / gmail_modify](gmail.md) | Google OAuth token (scoped) | LLM, other credentials |
| [drive / drive_write](google-drive.md) | Google OAuth token (scoped) | LLM, other credentials |
| google_docs / google_sheets / google_slides | Google OAuth token (scoped) | LLM, other credentials |
| [bluebubbles / imessage_bridge](imessage.md) | BlueBubbles API / Bridge token | LLM, other credentials |
| [apple_notes / apple_reminders / things](apple-apps.md) | Bridge HTTP token (scoped) | LLM, other credentials |
| [brave_search / fetch_url](web.md) | Brave API key / None | LLM, other credentials |
| browser | None (managed) or host Chrome (relay) | LLM, other credentials |
| [notion / notion_write](notion.md) | Notion integration token | LLM, other credentials |
| [github](github.md) | GitHub PAT (`GH_TOKEN`) | LLM, other credentials |
| git_ops | Host git config | LLM, other credentials |
| coding | Host filesystem (scoped) | LLM, other credentials |
| file_ops | Host filesystem (scoped) | LLM, other credentials |
| [exec](exec.md) | Host filesystem (scoped) | LLM, other credentials |
| [exec_interactive](exec-interactive.md) | Network access (SSH, REPLs) | LLM, other credentials |
| [clipboard](clipboard.md) | Bridge HTTP token (scoped) | LLM, other credentials |
| [tts](tts.md) | ElevenLabs / OpenAI API key | LLM, other credentials |
| [dev_session](dev-session.md) | None (containerized) | LLM, other credentials |
| [host_exec](host-exec.md) | Bridge HTTP token (scoped) | LLM, other credentials |

## How Executors Run

- **Development**: Executors run as subprocesses with secrets injected as environment variables
- **Production**: Each executor runs in its own Docker container with `--read-only`, `--cap-drop=ALL`, memory/CPU limits, and a 60-second timeout. The `exec_interactive` executor uses one container per session with a 5-minute timeout

See [Container Mode](../deployment/containers.md) for production deployment details.

## Executor Output

All executors output JSON to stdout. The orchestrator collects this output and injects it into the prompt template using `{executor_name}` placeholders.
