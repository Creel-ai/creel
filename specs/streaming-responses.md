# SPEC: Streaming Responses

## What It Does

Currently, Creel waits for the entire LLM response before sending it to the user. For long responses, this means the user stares at a blank screen (or a typing indicator) for 10-30+ seconds.

Streaming sends tokens to the chat channel as they arrive, so the user sees the response building in real time. This also enables:
- Typing indicators that are actually meaningful (you see text appearing)
- Early cancellation (user can interrupt a bad response)
- Perceived speed improvement even when total time is the same

## How It Works

```
LLM Provider
    │
    │ token stream
    ▼
┌──────────────┐
│  Chunker     │  Buffers tokens, flushes on sentence boundaries
│              │  or after N chars / M ms (whichever comes first)
└──────┬───────┘
       │
       │ chunks
       ▼
┌──────────────┐
│  Channel     │  Sends each chunk as a message edit (if supported)
│  Adapter     │  or as sequential messages (fallback)
└──────────────┘
```

### Chunking strategy

Not every token should be a separate message — that would spam the channel. Instead:

1. Buffer incoming tokens
2. Flush when any of these triggers hit:
   - Sentence boundary detected (`. `, `! `, `? `, newline)
   - Buffer exceeds N characters (default 200)
   - Time since last flush exceeds M ms (default 1500ms)
   - Stream ends

### Channel behavior

| Channel | Approach |
|---------|----------|
| WhatsApp | Sequential messages (no edit support). Chunk size should be larger (~500 chars) to avoid message spam |
| Telegram | Edit the same message as tokens arrive (`editMessageText`). Final message is the complete response |
| Discord | Edit the same message (`message.edit()`). Same as Telegram |
| Slack | Edit via `chat.update`. Same as Telegram |
| TUI/CLI | Print tokens directly to terminal as they arrive (no chunking needed) |
| Webhook | Buffer full response, send once at end |

Channels declare a `StreamingMode` capability:
- `EDIT_IN_PLACE` — send initial message, then edit it as chunks arrive (Telegram, Discord, Slack)
- `SEQUENTIAL` — send chunks as separate messages (WhatsApp)
- `DIRECT` — print tokens directly (TUI)
- `NONE` — no streaming, buffer everything (webhook, channels that don't support it)

### Agent loop integration

Streaming only applies to the final text response in a turn. Tool calls are not streamed — they execute normally. The flow is:

1. Agent decides to call tools → execute tools (no streaming)
2. Agent produces final text response → stream to channel
3. If agent calls more tools after text → new turn, streaming resets

## Config Surface

```yaml
streaming:
  enabled: true               # global toggle
  chunk_size: 200             # max chars before flush
  flush_interval_ms: 1500     # max time before flush
  # Per-channel overrides (optional)
  whatsapp:
    chunk_size: 500           # larger chunks for WhatsApp
  telegram:
    mode: edit_in_place       # override if needed
```

Streaming is **on by default** for channels that support it. Users can disable globally or per-channel.

## Acceptance Criteria

### Core streaming
- [ ] Agent response streams to TUI character-by-character
- [ ] Agent response streams to Telegram as an edited message that grows in real time
- [ ] Agent response streams to WhatsApp as chunked sequential messages
- [ ] Tool calls execute normally (not streamed) — only final text is streamed
- [ ] Full response is identical whether streaming is on or off

### Chunking
- [ ] Short responses (< chunk_size) send as a single message
- [ ] Long responses break at sentence boundaries when possible
- [ ] Flush timer fires even if no sentence boundary is found (prevents stalling)
- [ ] Code blocks are not split mid-block (buffer until block closes)

### Edit-in-place (Telegram, Discord, Slack)
- [ ] Initial message sent on first chunk
- [ ] Subsequent chunks edit the same message
- [ ] Final edit contains the complete response
- [ ] If edit fails (rate limit), falls back to appending a new message

### Channel compatibility
- [ ] Channels that don't declare streaming support get the full buffered response (no change in behavior)
- [ ] Streaming can be disabled per-channel via config
- [ ] Streaming disabled globally → all channels get buffered responses

### Cancellation
- [ ] User sends a new message while streaming → current stream aborts, new message processed
- [ ] Partial response is preserved in session history (not lost)

### Edge cases
- [ ] LLM returns empty stream → no message sent
- [ ] Stream errors mid-response → send whatever was buffered + error notice
- [ ] Very long response (>4096 chars on Telegram) → handle message length limits (split into multiple messages)
- [ ] Streaming + failover → if primary fails, fallback uses non-streaming (acceptable degradation)
