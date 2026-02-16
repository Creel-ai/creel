# Web Search & Fetch

## Brave Search

Web search via the [Brave Search API](https://brave.com/search/api/).

### Configuration

```yaml
brave_search:
  image: executor-brave-search:latest
  secrets: secrets/brave_search.env.enc
  args:
    query: "latest news on AI safety"
    count: "5"   # max 20
```

### Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `query` | yes | Search query string |
| `count` | no | Number of results to return (max 20) |

## Fetch URL

Extracts text content from web pages using BeautifulSoup. Strips scripts, nav, and boilerplate. No API key required.

### Configuration

```yaml
fetch_url:
  image: executor-fetch-url:latest
  args:
    url: "https://example.com/article"
    max_chars: "10000"
```

### Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `url` | yes | URL to fetch |
| `max_chars` | no | Maximum characters to return (default: 10000) |
