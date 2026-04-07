# Apple Apps

These executors integrate with macOS applications via the [Host Bridge](../deployment/host-bridge.md). They require the bridge server to be running and use scoped authentication tokens.

## Apple Notes

Reads and creates notes in Notes.app. Uses the `memo` CLI tool for macOS integration.

### Tools

**`list_notes`** — List notes, optionally filtered by folder.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `folder` | no | Notes folder name |

**`search_notes`** — Search notes by text query.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `query` | yes | Search query |

**`read_note`** — Read a specific note.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `note_id` | yes | Note identifier |

**`create_note`** — Create a new note.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `title` | yes | Note title |
| `body` | no | Note body text |
| `folder` | no | Folder to create in |

## Apple Reminders

Reads and creates reminders in Reminders.app. Uses the `remindctl` CLI tool for macOS integration.

### Tools

**`list_reminders`** — List reminders from a list.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `list_name` | no | Reminders list name |
| `filter` | no | Filter: `incomplete` (default) or `all` |

**`create_reminder`** — Create a new reminder.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `title` | yes | Reminder title |
| `due_date` | no | Due date |
| `notes` | no | Additional notes |
| `list_name` | no | List to add to |

**`complete_reminder`** — Mark a reminder as complete.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `name` | yes | Name of the reminder to complete |

**`get_reminder_lists`** — List available reminder lists. No parameters.

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
