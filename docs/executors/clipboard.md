# Clipboard

The clipboard executor provides read/write access to the macOS system clipboard via the host bridge.

## Requirements

- macOS only
- Bridge service running (`bridge.enabled: true` in `agent.yaml`)

## Tools

### `read_clipboard`

Read the current clipboard contents.

```json
{"tool": "read_clipboard"}
```

### `write_clipboard`

Write text to the clipboard.

```json
{"tool": "write_clipboard", "args": {"text": "Hello, world!"}}
```

## How It Works

The executor communicates with the bridge server over HTTP rather than calling `pbcopy`/`pbpaste` directly. This keeps clipboard access mediated through the bridge's authentication layer.

- **Bridge endpoints**: `POST /clipboard/read`, `POST /clipboard/write`
- **Authentication**: Bearer token via `BRIDGE_TOKEN`
- **Timeout**: 30 seconds

## Security

| Property | Value |
|----------|-------|
| Credentials | Bridge HTTP token (scoped) |
| Platform | macOS only |
| Network | Bridge server only |
| Container isolation | Yes (production) |
