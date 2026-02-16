# Gmail

## Gmail (Read)

Reads emails matching a Gmail search query. Supports both listing emails and reading individual messages by ID. Requires a one-time OAuth setup:

```bash
# Same GCP project — enable the Gmail API
python scripts/setup-google-oauth.py gmail --encrypt
```

The executor uses a read-only scope (`gmail.readonly`).

### Configuration

```yaml
gmail_readonly:
  image: executor-gmail-readonly:latest
  secrets: secrets/gmail.env.enc
  args:
    query: "is:unread newer_than:1d"   # Gmail search syntax
    max_results: "20"                   # max messages to fetch
    message_id: ""                      # set to read a specific email by ID
```

### Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `query` | yes | Gmail search query |
| `max_results` | no | Maximum messages to return (default: 20) |
| `message_id` | no | Specific message ID to read in full |

When reading a single message by ID, the full decoded body is returned (prefers `text/plain`, falls back to HTML stripping via BeautifulSoup).

## Gmail (Send)

Sends an email. Requires a one-time OAuth setup with the `gmail.send` scope:

```bash
python scripts/setup-google-oauth.py gmail_send --encrypt
```

### Configuration

```yaml
gmail_send:
  image: executor-gmail-send:latest
  secrets: secrets/gmail_send.env.enc
  args:
    to: "recipient@example.com"
    subject: "Daily report"
    body: "Here is today's summary..."
```

### Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `to` | yes | Recipient email address |
| `subject` | yes | Email subject line |
| `body` | yes | Email body text |

## Gmail (Modify)

Modifies, trashes, or permanently deletes Gmail messages. Requires a one-time OAuth setup with the `gmail.modify` scope:

```bash
python scripts/setup-google-oauth.py gmail_modify --encrypt
```

### Configuration

```yaml
gmail_modify:
  image: executor-gmail-modify:latest
  secrets: secrets/gmail_modify.env.enc
  args:
    action: "modify"              # modify, trash, or delete
    message_id: "18f1a2b3c4d5e6f" # Gmail message ID
    add_labels: "STARRED"         # comma-separated label IDs (modify only)
    remove_labels: "UNREAD,INBOX" # comma-separated label IDs (modify only)
```

### Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `action` | yes | `modify`, `trash`, or `delete` |
| `message_id` | yes | Gmail message ID |
| `add_labels` | no | Comma-separated label IDs to add (modify only) |
| `remove_labels` | no | Comma-separated label IDs to remove (modify only) |
