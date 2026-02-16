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

### Configuration

```yaml
things:
  args:
    action: "list_tasks"     # list_tasks, create_task, complete_task, search_tasks
    area: "Personal"
    limit: "25"
```

### Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `action` | yes | `list_tasks`, `create_task`, `complete_task`, or `search_tasks` |
| `area` | no | Things area to filter by |
| `limit` | no | Maximum tasks to return (default: 25) |
