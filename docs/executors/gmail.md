# Gmail

## Gmail (Read)

Reads emails matching a Gmail search query. Supports both listing emails and reading individual messages by ID. Requires a one-time OAuth setup:

```bash
# Same GCP project — enable the Gmail API
python scripts/setup-google-oauth.py gmail --encrypt
```

The executor uses a read-only scope (`gmail.readonly`).

### Tools

The executor exposes two tools:

**`check_email`** — Search for emails matching a query.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `query` | yes | Gmail search query |
| `max_results` | no | Maximum messages to return (default: 20) |
| `full_body` | no | Include full body in search results (default: false) |

**`read_email`** — Read a specific email by ID.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `message_id` | yes | Gmail message ID |

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

### Tools

The executor exposes two tools:

**`trash_email`** — Move an email to trash.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `message_id` | yes | Gmail message ID |
| `subject` | yes | Email subject (for confirmation) |

**`mark_read`** — Mark an email as read.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `message_id` | yes | Gmail message ID |

The underlying executor also supports `modify` (add/remove labels), `delete` (permanent), and batch operations via direct invocation.
