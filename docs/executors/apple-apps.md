# Apple Apps

These executors integrate with macOS applications via the [Host Bridge](../deployment/host-bridge.md). They require the bridge server to be running and use scoped authentication tokens.

## Apple Notes

Reads and creates notes in Notes.app. Uses the `memo` CLI tool for macOS integration.

### Configuration

```yaml
apple_notes:
  args:
    action: "list_notes"   # list_notes, search_notes, read_note, create_note
    folder: "Notes"
    limit: "25"
```

### Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `action` | yes | `list_notes`, `search_notes`, `read_note`, or `create_note` |
| `folder` | no | Notes folder name |
| `limit` | no | Maximum notes to return (default: 25) |

## Apple Reminders

Reads and creates reminders in Reminders.app. Uses the `remindctl` CLI tool for macOS integration.

### Configuration

```yaml
apple_reminders:
  args:
    action: "list_reminders"  # list_reminders, create_reminder, complete_reminder, get_lists
    list_name: "Reminders"
```

### Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `action` | yes | `list_reminders`, `create_reminder`, `complete_reminder`, or `get_lists` |
| `list_name` | no | Reminders list name |

## Things 3

Manages tasks in Things 3. Uses the `things` CLI tool for macOS integration.

### Tools

**`list_things`** — List tasks from Things 3.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `action` | no | `inbox` (default), `today`, `upcoming`, `projects`, or `search` |
| `query` | no | Search query (required when action is `search`) |

**`create_things_task`** — Create a new task in Things 3.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `title` | yes | Title of the task |
| `notes` | no | Notes for the task |
| `when` | no | When to schedule the task |
| `deadline` | no | Deadline for the task |
| `tags` | no | Tags for the task |
| `list` | no | Project or area to add the task to |

**`complete_things_task`** — Mark a task as complete.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `id` | yes | ID of the task to complete |
