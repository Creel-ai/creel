# Host Bridge

For macOS-specific tools (Apple Notes, Apple Reminders, Things 3, iMessage), Docker containers can't directly execute AppleScript or access macOS applications. The host bridge solves this by running a FastAPI server on the host system that provides authenticated HTTP endpoints for containerized executors.

## Why

Docker containers are sandboxed and can't access the macOS scripting bridge or application APIs that tools like Notes, Reminders, and Things 3 require.

## How

A FastAPI server (`./runner.py bridge`) runs as a host process and exposes REST endpoints at `/notes/*`, `/reminders/*`, `/things/*`, and `/imessage/*`. Containerized executors make HTTP requests to these endpoints with scoped authentication tokens.

## Security

Each executor receives a scoped token that only grants access to its specific tool endpoints. For example, the `apple_notes` executor can only call `/notes/*` endpoints, not `/reminders/*` or `/things/*`.

## CLI Integration

The bridge server delegates to command-line tools:

- **Apple Notes**: `memo` CLI for reading/writing notes
- **Apple Reminders**: `remindctl` CLI for managing reminders
- **Things 3**: `things` CLI for task management
- **iMessage**: `imsg` CLI for sending/receiving messages

## Starting the Bridge

```bash
./runner.py bridge
```

This starts the FastAPI server on `localhost:8765` with authentication middleware and scoped endpoint routing.
