# Task Definitions

Tasks are YAML files in `tasks/`. Each defines what data to fetch, how to prompt the LLM, and where to send the result.

## Example

```yaml
# tasks/morning_briefing.yaml
name: morning_briefing
schedule: "0 7 * * *"  # 7am daily

fetch:
  calendar:
    image: executor-gcal:latest
    secrets: secrets/gcal.env.enc
    args:
      range: today

  weather:
    image: executor-weather:latest
    args:
      location: denver

prompt: |
  You're my personal assistant. Give me a quick rundown of my day.

  Today: {date}
  Weather: {weather}
  Calendar: {calendar}

  Keep it under 150 words. Flag any early meetings or conflicts.

output:
  type: imessage
  to: "$PHONE"

llm:
  model: claude-sonnet-4-20250514
  max_tokens: 300
  secrets: secrets/anthropic.env.enc
```

## Task Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Unique task identifier |
| `schedule` | yes | 5-part cron expression |
| `fetch` | yes | Map of executor name to config |
| `fetch.<name>.image` | yes | Docker image for containerized mode |
| `fetch.<name>.secrets` | no | Path to age-encrypted .env file |
| `fetch.<name>.args` | no | Key-value args passed to the executor |
| `prompt` | yes | Prompt template with `{name}` placeholders |
| `output.type` | yes | `imessage`, `stdout`, or `file` |
| `output.to` | yes | Phone number, empty string, or file path |
| `llm.model` | no | Anthropic model ID (default: `claude-sonnet-4-20250514`) |
| `llm.max_tokens` | no | Max response tokens (default: 300) |
| `llm.secrets` | no | Path to age-encrypted .env with `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_API_KEY` |

The `{date}` placeholder is always available and resolves to the current date.

## Agent Mode Tasks

Tasks can use the agent loop for multi-step tool calling by setting `mode: agent`:

```yaml
# tasks/email_triage.yaml
name: email_triage
schedule: "0 8 * * *"
mode: agent

fetch:
  gmail:
    image: executor-gmail:latest
    secrets: secrets/gmail.env.enc
    args:
      query: "is:unread newer_than:1d"

tools:
  trash_email:
    executor: gmail_modify
    secrets: secrets/gmail_modify.env.enc
    description: "Move an email to trash"
    parameters:
      message_id:
        type: string
        description: "Gmail message ID"
        required: true
    fixed_args:
      action: "trash"

agent:
  max_turns: 10

prompt: |
  Triage my unread emails. Trash spam. Summarize what you did.
  {gmail}

output:
  type: imessage
  to: "$PHONE"

llm:
  model: claude-sonnet-4-20250514
  max_tokens: 1024
  secrets: secrets/anthropic.env.enc
```

### Agent Mode Fields

In addition to the standard fields above:

| Field | Required | Description |
|-------|----------|-------------|
| `mode` | no | `simple` (default) or `agent` |
| `tools` | no | Map of tool name to tool config |
| `tools.<name>.executor` | yes | Executor to execute (e.g., `gmail_modify`) |
| `tools.<name>.secrets` | no | Path to age-encrypted .env file |
| `tools.<name>.description` | yes | Description shown to the LLM |
| `tools.<name>.parameters` | no | Parameters the LLM can provide |
| `tools.<name>.fixed_args` | no | Args always passed to executor (override LLM input) |
| `agent.max_turns` | no | Max agent loop iterations (default: 10) |
