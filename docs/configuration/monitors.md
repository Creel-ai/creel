# Monitors

Monitors are proactive agents that run on a schedule, check for conditions, and send alerts. Unlike tasks (which always produce output), monitors only notify you when something needs attention.

## How it works

1. A monitor runs on a cron or interval schedule
2. An executor fetches data (email, calendar, system stats, etc.)
3. The LLM evaluates the data against the monitor's prompt
4. If the LLM returns a non-empty response, an alert is generated
5. The alert is deduplicated, checked against quiet hours, and delivered

## Quick start

```bash
# Add a monitor from a built-in template
creel monitor add-template urgent_email --delivery-channel telegram

# Add a custom monitor
creel monitor add \
  --name "disk-check" \
  --executor exec \
  --prompt "Check disk usage. Alert if any partition > 85%." \
  --cron "0 */6 * * *" \
  --delivery-channel telegram \
  --alert-level notice

# List monitors
creel monitor list

# Run a check immediately
creel monitor run <monitor_id>

# View history
creel monitor history <monitor_id>
```

## Configuration in agent.yaml

Monitors can be defined declaratively in `agent.yaml`:

```yaml
monitors:
  urgent_emails:
    executor: gmail_readonly
    prompt: |
      Check for urgent unread emails from VIP senders or with
      urgent keywords. Summarize each one briefly. If nothing
      urgent, respond with an empty string.
    schedule: "*/15 * * * *"
    delivery: telegram
    alert_level: urgent
    quiet_hours: "23:00-07:00"
    cooldown_seconds: 1800

  calendar_conflicts:
    executor: gcal
    prompt: |
      Check today's and tomorrow's calendar for double-bookings
      or back-to-back meetings. If no conflicts, respond with
      an empty string.
    schedule: "0 8 * * *"
    delivery: telegram
    alert_level: notice
    cooldown_seconds: 3600
```

### Monitor fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `executor` | yes | — | Executor to fetch data (`gmail_readonly`, `gcal`, `exec`, etc.) |
| `prompt` | yes | — | What to check for — the LLM evaluates this against executor output |
| `schedule` | yes | — | 5-part cron expression |
| `delivery` | no | `announce` | Channel name for alert delivery |
| `delivery_mode` | no | `announce` | `announce`, `webhook`, or `none` |
| `delivery_url` | no | — | Webhook URL (required when `delivery_mode: webhook`) |
| `alert_level` | no | `notice` | `info`, `notice`, or `urgent` |
| `quiet_hours` | no | — | `HH:MM-HH:MM` range to suppress non-urgent alerts |
| `cooldown_seconds` | no | `3600` | Deduplication window in seconds |
| `description` | no | `""` | Human-readable description |
| `enabled` | no | `true` | Set `false` to disable without removing |

## Alert levels

| Level | Behavior |
|-------|----------|
| `info` | Logged only — never delivered. Useful for audit trails. |
| `notice` | Delivered during active hours. Suppressed during quiet hours. |
| `urgent` | Always delivered immediately, even during quiet hours. |

## Alert deduplication

Monitors use SHA-256 fingerprinting on the alert message to avoid sending duplicate alerts. If the same alert (same monitor + similar message content) fires again within the `cooldown_seconds` window, it is suppressed.

- Fingerprints are based on the monitor ID and the first 200 characters of the alert message
- Set `cooldown_seconds: 0` to disable deduplication
- Different alert messages produce different fingerprints and are delivered independently

## Quiet hours

Quiet hours suppress `notice`-level alerts during a time window. `urgent` alerts always bypass quiet hours.

```yaml
quiet_hours: "23:00-07:00"   # in agent.yaml (string format)
```

Via CLI:

```bash
creel monitor add --quiet-hours "23:00-07:00" --tz "America/Denver" ...
```

Overnight ranges (e.g., `23:00-07:00`) and same-day ranges (e.g., `09:00-17:00`) are both supported. The timezone defaults to UTC.

## Delivery

### Channel delivery (announce)

Alerts are sent to a configured channel (Telegram, iMessage, etc.):

```bash
creel monitor add --delivery-channel telegram ...
```

### Webhook delivery

Alerts are POSTed as JSON to an HTTPS URL:

```bash
creel monitor add --delivery-url "https://hooks.example.com/alerts" ...
```

Payload format:

```json
{
  "monitor_id": "a1b2c3d4e5f6",
  "monitor_name": "disk-check",
  "alert_level": "notice",
  "message": "Disk /dev/sda1 at 92% capacity"
}
```

Webhook URLs must use HTTPS and cannot target private/loopback addresses (SSRF protection).

### No delivery

Set `delivery_mode: none` to log alerts without sending them. Useful with `alert_level: info` for audit-only monitors.

## Built-in templates

Three templates are included for common use cases:

| Template | Executor | Schedule | Level | Description |
|----------|----------|----------|-------|-------------|
| `urgent_email` | `gmail_readonly` | Every 15 min | urgent | Checks for urgent unread emails |
| `calendar_conflicts` | `gcal` | Daily at 8am | notice | Detects double-bookings and conflicts |
| `system_health` | `exec` | Every 6 hours | notice | Monitors disk, memory, and load |

```bash
# List available templates
creel monitor templates

# Create from template with delivery
creel monitor add-template urgent_email --delivery-channel telegram

# Override timezone for quiet hours
creel monitor add-template calendar_conflicts --tz "America/Denver"
```

## Data storage

Monitor definitions, run history, and alert records are stored as JSON files in `~/.creel/monitors/`. Files are written with owner-only permissions (`0700` directory, `0600` files) since alert content may contain sensitive data.

- `monitors.json` — monitor definitions
- `runs.json` — execution history (capped at 50 per monitor)
- `alerts.json` — alert records (capped at 100 per monitor)
