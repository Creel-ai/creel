# Agent Configuration

The global agent config (`agent.yaml`) defines tools, LLM settings, session behavior, channels, workspace memory, and guardian settings for interactive chat mode.

## Example

```yaml
system_prompt: |
  You are a personal assistant. Be concise and helpful.
  Today is {date}.

tools:
  check_weather:
    executor: weather
    description: "Get current weather and forecast"
    parameters:
      location:
        type: string
        description: "City name or coordinates"
        required: true

  check_email:
    executor: gmail_readonly
    secrets: secrets/gmail.env.enc
    description: "Search Gmail for emails"
    parameters:
      query:
        type: string
        description: "Gmail search query"
        required: true

  # ... see agent.yaml for all tools (calendar, drive, iMessage, etc.)

llm:
  model: claude-sonnet-4-20250514
  max_tokens: 1024
  secrets: secrets/anthropic.env.enc

agent:
  max_turns: 15

session:
  sessions_dir: sessions
  max_history: 50
  summarize_on_trim: true
  context_pruning:
    enabled: true
    threshold: 0.80
    min_recent_messages: 4
  tool_cache:
    enabled: true
    default_ttl: 300
    tool_ttls:
      check_weather: 1800

workspace:
  path: workspace
  timezone: "America/Denver"
  memory_days: 2
  memory_max_chars: 5000
  max_chars_per_file: 20000

channels:
  imessage:
    listen_to: "$PHONE"
    poll_interval: 3
  bluebubbles:
    server_url: "$BLUEBUBBLES_URL"
    password: "$BLUEBUBBLES_PASSWORD"
    listen_to:
      - "$PHONE"
    poll_interval: 3
  telegram:
    secrets: secrets/telegram.env.enc
    mode: polling           # "polling" or "webhook"
    poll_timeout: 30
    send_typing: true
    allowed_senders:          # required — at least one entry
      - "123456789"         # Telegram user ID
```

## Sessions

Sessions are stored as JSON files in `sessions/` (gitignored) and persist conversation history across interactions.

| Field | Description |
|-------|-------------|
| `sessions_dir` | Directory to store session JSON files |
| `max_history` | Maximum number of messages to keep in history |
| `summarize_on_trim` | Build a summarize callback for use by `/compact` and context pruning |

### Context Pruning

Context pruning automatically manages the token window during long conversations. When enabled, it runs before each LLM call and **transiently** prunes a copy of the message history — the full history is always preserved on disk.

| Field | Description |
|-------|-------------|
| `context_pruning.enabled` | Enable automatic context pruning (default: `false`) |
| `context_pruning.threshold` | Fraction of `max_context_tokens` at which pruning triggers (default: `0.80`) |
| `context_pruning.min_recent_messages` | Number of recent messages to always keep (default: `4`) |

When pruning triggers (estimated tokens > 80% of max), it prunes down to 60% to create headroom and avoid re-pruning every turn. Messages are scored by importance and the least important are dropped first. Tool-call pairs are never split. If `summarize_on_trim` is enabled, pruned messages are summarized and the summary is prepended to the context sent to the LLM.

**Importance scoring.** Each message is scored as `importance = base_weight × recency`:

| Message type | Base weight | Rationale |
|---|---|---|
| Tool results | 2.0 | Factual data the model needs to reason about |
| Tool use (assistant) | 1.8 | Records what was called and with what args |
| User text | 1.5 | User intent and questions |
| Assistant text | 1.0 | Lowest priority — can be regenerated |
| Conversation summaries | ∞ | Never pruned |

Recency uses exponential decay with a half-life of 8 messages: `recency = 0.5 ^ (distance_from_end / 8)`. A message 8 positions from the end gets a 0.5× multiplier; 16 positions back gets 0.25×. This means recent messages of any type are kept, while among older messages, tool results and user text survive longer than assistant prose.

To explicitly and persistently compact a session, use the `/compact` command in the TUI.

### Tool Result Caching

Tool result caching stores successful tool outputs in memory so repeated identical calls within the TTL window are served instantly. Errors are never cached.

| Field | Description |
|-------|-------------|
| `tool_cache.enabled` | Enable tool result caching (default: `false`) |
| `tool_cache.default_ttl` | Default cache TTL in seconds (default: `300`) |
| `tool_cache.max_entries` | Maximum cache entries before LRU eviction (default: `256`) |
| `tool_cache.tool_ttls` | Per-tool TTL overrides (e.g. `check_weather: 1800`) |

Individual tools can also set `cache_ttl` in their tool definition to override the default.

## Workspace Memory

Workspace memory provides file-based memory across sessions:

- `workspace/memory/YYYY-MM-DD.md` — Daily append-only logs written by the agent's `remember` tool
- `workspace/MEMORY.md` — Curated long-term memory

Recent daily logs and long-term memory are injected into the system prompt automatically.

| Field | Description |
|-------|-------------|
| `path` | Root directory for workspace files |
| `timezone` | Timezone for date-stamped memory files |
| `memory_days` | Number of recent daily logs to include in prompt |
| `memory_max_chars` | Max characters from memory to inject into prompt |
| `max_chars_per_file` | Max characters to read from any single workspace file |

## Quiet Hours

Creel supports quiet hours to suppress proactive notifications during configured time periods (e.g., nighttime). Quiet hours only suppress outbound notifications — direct replies to user messages are never suppressed.

```yaml
quiet_hours:
  enabled: true
  start: "22:00"  # 10 PM
  end: "08:00"    # 8 AM
  timezone: "America/Denver"
```

## Rate Limiting

Add a `rate_limits` block under `llm` to cap LLM API usage and prevent runaway costs:

```yaml
llm:
  model: claude-sonnet-4-20250514
  max_tokens: 1024
  rate_limits:
    enabled: true
    requests_per_minute: 30
    requests_per_hour: 500
    tokens_per_day: 1000000
    cost_per_day_usd: 10.00
    queue_timeout: 30.0
```

Rate limiting is **disabled by default** — set `enabled: true` to activate it. See [Rate Limiting](rate-limiting.md) for the full configuration reference.

## Monitors

Define proactive monitors that run on a schedule and alert you when conditions are met. See [Monitors](monitors.md) for the full reference.

```yaml
monitors:
  urgent_emails:
    executor: gmail_readonly
    prompt: "Check for urgent unread emails. If none, respond with empty string."
    schedule: "*/15 * * * *"
    delivery: telegram
    alert_level: urgent
    cooldown_seconds: 1800
```

## Guardian

See [Guardian Security](../architecture/guardian.md) for the full guardian configuration reference.
