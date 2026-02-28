# SPEC: Multi-Provider Model Support + Failover

## What It Does

Creel is currently hardcoded to Anthropic. This spec adds support for multiple LLM providers (OpenAI, Google Gemini, Groq, local models) and automatic failover when a provider is down or rate-limited.

Users should be able to:
- Choose any supported model for their tasks and agent sessions
- Define a failover chain so the agent keeps working during outages
- Override the model per-job or per-session
- Use local models (Ollama, llama.cpp) for privacy-sensitive or offline use

## How It Works

```
            ┌──────────────────┐
            │  Model Router    │
            │                  │
            │  Resolves model  │
            │  string to       │
            │  provider client │
            └───────┬──────────┘
                    │
       ┌────────────┼────────────┬──────────────┐
       ▼            ▼            ▼              ▼
  ┌─────────┐ ┌─────────┐ ┌─────────┐   ┌──────────┐
  │Anthropic│ │ OpenAI  │ │ Google  │   │  Local   │
  │         │ │         │ │ Gemini  │   │ (Ollama) │
  └─────────┘ └─────────┘ └─────────┘   └──────────┘

  On failure (429, 500, 502, 503):
  ┌─────────┐  fail   ┌─────────┐  fail   ┌─────────┐
  │ Primary ├────────▶│Fallback1├────────▶│Fallback2│
  └─────────┘         └─────────┘         └─────────┘
```

### Model string format

`provider/model-name` — e.g.:
- `anthropic/claude-sonnet-4-20250514`
- `openai/gpt-4o`
- `google/gemini-2.0-flash`
- `groq/llama-3.3-70b`
- `ollama/llama3.2`

Bare model names (e.g. `claude-sonnet-4-20250514`) resolve via the provider that recognizes them (Anthropic first for backward compat).

### Provider interface

Each provider implements a common interface:
- `create_message(messages, model, max_tokens, tools, system, stream_callback)` → response
- `health()` → bool

All providers normalize to Anthropic-style message format internally. The router handles translation to/from each provider's native format.

### Failover

Failover is configured as an ordered list. On transient errors (429, 500, 502, 503, timeout), the router tries the next provider in the chain. Non-transient errors (400, 401, 403) are not retried.

## Config Surface

```yaml
llm:
  model: anthropic/claude-sonnet-4-20250514
  max_tokens: 8192
  failover:
    - openai/gpt-4o
    - google/gemini-2.0-flash

providers:
  anthropic:
    api_key_env: ANTHROPIC_API_KEY    # or auth_token_env for OAuth
  openai:
    api_key_env: OPENAI_API_KEY
  google:
    api_key_env: GOOGLE_API_KEY
  groq:
    api_key_env: GROQ_API_KEY
  ollama:
    base_url: http://localhost:11434   # default
```

Provider credentials use env var names pointing to secrets (consistent with Creel's existing secrets model — the actual keys live in encrypted env files, never in config).

### Per-job override

```yaml
# In a cron job or task definition
payload:
  model: openai/gpt-4o
  # Falls back to global failover chain if this model fails
```

## Acceptance Criteria

### Multi-provider
- [ ] Configure Anthropic + OpenAI → agent works with either model
- [ ] Configure a Google Gemini model → agent can use it for tasks
- [ ] Configure Ollama → agent works with local models (no internet required)
- [ ] Bare model name `claude-sonnet-4-20250514` resolves to Anthropic automatically
- [ ] Prefixed model `openai/gpt-4o` routes to OpenAI

### Failover
- [ ] Primary model returns 429 → automatically retries with fallback model
- [ ] Primary model returns 500/502/503 → same failover behavior
- [ ] Primary model returns 401 → does NOT failover (config error, not transient)
- [ ] All models in chain fail → raises error with details of each attempt
- [ ] Failover is logged so the user can see what happened

### Tool compatibility
- [ ] Tool calling works with OpenAI models (format translation)
- [ ] Tool calling works with Gemini models (format translation)
- [ ] Models that don't support tools gracefully degrade (tools omitted, text-only response)

### Backward compatibility
- [ ] Existing configs with just `model: claude-sonnet-4-20250514` continue to work unchanged
- [ ] `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` env vars still work as before
- [ ] Existing task YAML files don't need changes

### Edge cases
- [ ] Provider configured but API key missing → clear error message at startup
- [ ] Unknown provider prefix (e.g. `foo/bar`) → clear error
- [ ] Failover model is same provider as primary → still works (different model, same provider)
- [ ] Streaming works through failover (if primary fails mid-stream, falls back to non-streaming on next provider)
