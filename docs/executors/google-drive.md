# Google Drive

## Drive (Read)

Lists and reads files from Google Drive. Requires a one-time OAuth setup with the `drive.readonly` scope:

```bash
python scripts/setup-google-oauth.py drive --encrypt
```

### Configuration

```yaml
drive:
  image: executor-drive:latest
  secrets: secrets/drive.env.enc
  args:
    query: "mimeType='application/pdf'"   # Drive search query (optional)
    max_results: "20"
```

### Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `query` | no | Google Drive search query |
| `max_results` | no | Maximum files to return (default: 20) |

## Drive (Write)

Uploads a file to Google Drive. Requires a one-time OAuth setup with the `drive.file` scope:

```bash
python scripts/setup-google-oauth.py drive_write --encrypt
```

### Configuration

```yaml
drive_write:
  image: executor-drive-write:latest
  secrets: secrets/drive_write.env.enc
  args:
    name: "report.txt"
    content: "File contents here..."
    mime_type: "text/plain"      # optional, defaults to text/plain
    folder_id: ""                # optional Drive folder ID
```

### Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `name` | yes | File name |
| `content` | yes | File contents |
| `mime_type` | no | MIME type (default: `text/plain`) |
| `folder_id` | no | Target folder ID in Drive |
