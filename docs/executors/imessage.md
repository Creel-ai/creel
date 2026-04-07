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
| `limit` | no | Maximum messages to return (default: 20) |
| `after_date` | no | Only return messages after this date |
| `message_guid` | no | Message GUID for reactions |
| `reaction` | no | Reaction type for `send_reaction` |

### Safety Limits

Built-in safety measures: hard caps on message count (50), message length (2000 chars), send rate limiting (10/min), and allowlist enforcement for recipients.

## iMessage Bridge

Sends and reads iMessages via the host bridge. Uses the `imsg` CLI tool for macOS integration, providing an alternative to BlueBubbles.

### Tools

The executor exposes one tool via the skill registry:

**`imessage_send`** — Send an iMessage.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `recipient` | yes | Phone number or email of recipient |
| `text` | yes | Message text |

The underlying executor also supports `recent` (get recent messages) and `chats` (list chats) actions via direct invocation with `limit` (default: 20).

See [Host Bridge](../deployment/host-bridge.md) for information on the bridge server that powers this executor.
