# Notion

## Read-only (`notion_api`)

Read-only Notion API access for searching workspace content, reading a page, and querying a database.

### Setup

Create a secret file with your Notion integration token:

```bash
cat > secrets/notion_read.env <<'EOF'
NOTION_API_KEY=secret_xxx
NOTION_VERSION=2022-06-28
EOF

./scripts/encrypt-secret.sh secrets/notion_read.env
```

`NOTION_VERSION` is optional and defaults to `2022-06-28`.

### Smoke Tests

`tests/test_notion_smoke.py` reuses the encrypted secrets file by default:

- Reads `NOTION_API_KEY` from `secrets/notion_read.env.enc` (or shell env if already set)
- Optionally reads `NOTION_TEST_PAGE_ID` and `NOTION_TEST_DATABASE_ID` for deeper live checks
- Optional override: `NOTION_SMOKE_SECRETS_FILE=/path/to/notion_read.env.enc`

### Configuration

```yaml
notion_api:
  executor: notion
  secrets: secrets/notion_read.env.enc
  args:
    action: "search"                 # search | retrieve_page | query_database
    query: "roadmap"                 # search only
    page_id: ""                      # retrieve_page only
    database_id: ""                  # query_database only
    filter_json: ""                  # query_database only (JSON object)
    sorts_json: ""                   # query_database only (JSON array)
    page_size: "20"                  # optional, clamped to 1-100
    start_cursor: ""                 # optional pagination cursor
```

### Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `action` | yes | `search`, `retrieve_page`, or `query_database` |
| `query` | no | Search text for `search` |
| `page_id` | no | Notion page ID for `retrieve_page` |
| `database_id` | no | Notion database ID for `query_database` |
| `filter_json` | no | JSON object filter for `query_database` |
| `sorts_json` | no | JSON array of sort descriptors for `query_database` |
| `page_size` | no | Result page size (default 20, max 100) |
| `start_cursor` | no | Cursor for pagination |

## Write (`notion_write`)

Create, update, append blocks to, and archive (soft-delete) Notion pages. Uses a separate secret file and policy entry so write access is independently controlled.

### Setup

```bash
cat > secrets/notion_write.env <<'EOF'
NOTION_API_KEY=secret_xxx
NOTION_VERSION=2022-06-28
EOF

./scripts/encrypt-secret.sh secrets/notion_write.env
```

The token can be the same integration token as the read executor, or a separate one with write permissions.

### Configuration

```yaml
notion_write:
  executor: notion_write
  secrets: secrets/notion_write.env.enc
  args:
    action: "create_page"            # create_page | update_page | append_blocks | delete_page
    database_id: ""                  # create_page only
    page_id: ""                      # update_page, append_blocks, delete_page
    properties_json: ""              # create_page, update_page (JSON object)
    children_json: ""                # create_page, append_blocks (JSON array of blocks)
```

### Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `action` | yes | `create_page`, `update_page`, `append_blocks`, or `delete_page` |
| `database_id` | for `create_page` | Target database ID |
| `page_id` | for `update_page`, `append_blocks`, `delete_page` | Target page ID |
| `properties_json` | for `create_page`, `update_page` | JSON object of Notion page properties |
| `children_json` | for `create_page` (optional), `append_blocks` | JSON array of block objects |

### Policy

All `notion_write` actions require human review by default (see `policies/default.yaml`).
