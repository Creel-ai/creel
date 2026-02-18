# Notion

Read-only Notion API access for searching workspace content, reading a page, and querying a database.

## Setup

Create a secret file with your Notion integration token:

```bash
cat > secrets/notion.env <<'EOF'
NOTION_API_KEY=secret_xxx
NOTION_VERSION=2022-06-28
EOF

./scripts/encrypt-secret.sh secrets/notion.env
```

`NOTION_VERSION` is optional and defaults to `2022-06-28`.

## Smoke Tests

`tests/test_notion_smoke.py` reuses the encrypted secrets file by default:

- Reads `NOTION_API_KEY` from `secrets/notion.env.enc` (or shell env if already set)
- Optionally reads `NOTION_TEST_PAGE_ID` and `NOTION_TEST_DATABASE_ID` for deeper live checks
- Optional override: `NOTION_SMOKE_SECRETS_FILE=/path/to/notion.env.enc`

## Configuration

```yaml
notion:
  image: executor-notion:latest
  secrets: secrets/notion.env.enc
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

## Parameters

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
