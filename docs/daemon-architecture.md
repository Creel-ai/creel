# Creel Daemon Architecture (Phase 0/1)

## Problem

The legacy CLI currently bundles agent loop, iMessage listener, scheduler, and bridge entry points into mode-specific flows (`chat`, `listen`, `serve`, `bridge`), which prevents a durable background runtime with attachable clients.

## Scope (This Milestone)

Phase 0/1 introduces:

- a transport-agnostic daemon service layer (`taskrunner.daemon.service.DaemonService`)
- draft API/event contracts (`taskrunner.daemon.contracts`)
- initial CLI wiring for `daemon start|stop|status|run`, `attach`, and `send`

## Runtime Boundaries

- **Daemon service**: owns message routing, sessions, scheduler lifecycle, and channel plugin lifecycle.
- **Transport adapters**: HTTP/Unix socket handlers map requests to service methods.
- **Clients**: TUI/CLI call transport API only (no direct `ChatServer` coupling).

## Draft API Surface

Non-streaming:

- `POST /v1/messages`
- `GET /v1/sessions?sender_id=...`
- `POST /v1/sessions/new`
- `POST /v1/sessions/{id}/resume`
- `GET /v1/sessions/{id}/history`
- `GET /v1/status`

Streaming:

- `POST /v1/messages/stream` (SSE event stream)

## Streaming Event Schema

Event envelope:

```json
{
  "type": "start | token | tool_call | tool_result | final | error",
  "sender_id": "cli",
  "session_id": "abcd1234",
  "payload": {}
}
```

## Transport Decision (Current)

- Use Unix socket for daemon control-plane API.
- Keep loopback HTTP where existing integrations already depend on it (bridge endpoints).
- Support SSE for token/event streaming.

## Next Steps

1. Add daemon API server (Unix socket + HTTP adapter as needed).
2. Add `creel daemon start|stop|status`.
3. Convert TUI into `creel attach` daemon client.
4. Fold current `serve` behavior into daemon-managed channels.
