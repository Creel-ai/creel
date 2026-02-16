# iMessage

Creel supports iMessage through two different executors depending on your setup.

## BlueBubbles

Sends and reads iMessages via a [BlueBubbles](https://bluebubbles.app/) server. Requires a running BlueBubbles instance.

### Configuration

```yaml
bluebubbles:
  image: executor-bluebubbles:latest
  secrets: secrets/bluebubbles.env.enc
  args:
    action: "get_recent_messages"  # get_recent_messages, send_message, send_reaction, get_chats
    chat_id: "chat123"
    limit: "25"
```

### Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `action` | yes | `get_recent_messages`, `send_message`, `send_reaction`, or `get_chats` |
| `chat_id` | varies | Chat ID for message operations |
| `limit` | no | Maximum messages to return (default: 25) |

### Safety Limits

Built-in safety measures: hard caps on message count (50), message length (2000 chars), send rate limiting (10/min), and allowlist enforcement for recipients.

## iMessage Bridge

Sends and reads iMessages via the host bridge. Uses the `imsg` CLI tool for macOS integration, providing an alternative to BlueBubbles.

### Configuration

```yaml
imessage_bridge:
  args:
    action: "get_recent"     # get_recent, send_message, get_chats
    limit: "25"
    chat_id: "chat123"
```

### Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `action` | yes | `get_recent`, `send_message`, or `get_chats` |
| `limit` | no | Maximum messages to return (default: 25) |
| `chat_id` | varies | Chat ID for message operations |

See [Host Bridge](../deployment/host-bridge.md) for information on the bridge server that powers this executor.
