# Host Bridge

For macOS-specific tools (Apple Notes, Apple Reminders, Things 3, iMessage), Docker containers can't directly execute AppleScript or access macOS applications. The host bridge solves this by running a FastAPI server on the host system that provides authenticated HTTP endpoints for containerized executors.

## Why

Docker containers are sandboxed and can't access the macOS scripting bridge or application APIs that tools like Notes, Reminders, and Things 3 require.

## How

A FastAPI server runs as a host process and exposes REST endpoints for macOS-native tools and host operations. Containerized executors make HTTP requests to these endpoints with scoped authentication tokens.

### Endpoint Groups

| Path | Scope | Purpose |
|------|-------|---------|
| `/notes/*` | `NOTES` | Apple Notes (read, search, create) |
| `/reminders/*` | `REMINDERS` | Apple Reminders (list, create, complete) |
| `/things/*` | `THINGS` | Things 3 task management |
| `/imessage/*` | `IMESSAGE` | iMessage send/receive |
| `/clipboard/*` | `CLIPBOARD` | macOS clipboard read/write |
| `/browser/*` | `BROWSER` | Playwright browser automation |
| `/git/*` | `GIT` | Git operations (status, diff, log, commit, push) |
| `/exec` | `EXEC` | Host command execution |
| `/process` | `EXEC` | Background process management |
| `/sessions` | `EXEC` | Session listing |
| `/health` | — | Health check (no auth required) |

## Security

Each executor receives a scoped token that only grants access to its specific endpoint group. For example, the `apple_notes` executor receives a `NOTES`-scoped token and can only call `/notes/*` endpoints, not `/reminders/*`, `/clipboard/*`, or any other group.

## CLI Integration

The bridge server delegates to command-line tools:

- **Apple Notes**: `memo` CLI for reading/writing notes
- **Apple Reminders**: `remindctl` CLI for managing reminders
- **Things 3**: `things` CLI for task management
- **iMessage**: `imsg` CLI for sending/receiving messages

## Starting the Bridge

```bash
creel daemon start
```

This starts the FastAPI server on `localhost:8099` with authentication middleware and scoped endpoint routing.
