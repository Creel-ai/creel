# Google Calendar

## Calendar (Read)

Reads calendar events. Requires a one-time OAuth setup:

```bash
# 1. Create GCP project, enable Calendar API, download OAuth credentials
# 2. Run the setup script (--encrypt auto-encrypts and deletes plaintext)
python scripts/setup-google-oauth.py gcal --encrypt
```

The executor uses a read-only scope (`calendar.readonly`) and authenticates with a refresh token.

### Configuration

```yaml
gcal:
  image: executor-gcal:latest
  secrets: secrets/gcal.env.enc
  args:
    range: today
```

### Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `range` | no | Time range to fetch (`today`, `week`) |

## Calendar (Write)

Creates calendar events. Requires a one-time OAuth setup with the `calendar.events` scope:

```bash
python scripts/setup-google-oauth.py gcal_write --encrypt
```

### Configuration

```yaml
gcal_write:
  image: executor-gcal-write:latest
  secrets: secrets/gcal_write.env.enc
  args:
    summary: "Team standup"
    start: "2025-01-15T09:00:00-07:00"   # ISO 8601
    end: "2025-01-15T09:30:00-07:00"
    description: "Daily sync"              # optional
    location: "Room 42"                    # optional
```

### Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `summary` | yes | Event title |
| `start` | yes | Start time in ISO 8601 format |
| `end` | yes | End time in ISO 8601 format |
| `description` | no | Event description |
| `location` | no | Event location |
