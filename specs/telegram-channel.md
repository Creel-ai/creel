# RALPH: Telegram Channel for Creel

## Requirements

### Goal
Add Telegram as a messaging channel so Creel can send/receive messages via a Telegram bot, matching the existing WhatsApp channel pattern.

### User Stories
- As a user, I can configure a Telegram bot token and start receiving/sending messages through Creel
- As a user, I can restrict which Telegram users or chats can interact with my agent via an allowlist
- As a user, I can receive messages in both DMs and group chats (with mention-based activation in groups)
- As a user, I can send/receive images, documents, voice notes, and other media
- As a user, I can see typing indicators while the agent is processing

### Non-Goals (for this spec)
- Inline buttons / keyboard markup (future)
- Telegram payments API
- Telegram Mini Apps
- Multi-bot support (single bot per Creel instance)

---

## Architecture

### Overview

```
Telegram Bot API
       │
       ▼
┌─────────────────┐
│ TelegramChannel  │  ← Channel (base class)
│  (webhook mode)  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ TelegramBridge   │  ← Abstraction over Bot API HTTP calls
│  (python-telegram-bot or httpx)
└─────────────────┘
```

Follows the same pattern as `WhatsAppChannel` + `WhatsAppBridge`:
- `TelegramBridge` handles raw Bot API calls (send, get updates, set webhook)
- `TelegramChannel` implements the `Channel` interface and uses the bridge

### Operating Modes
1. **Webhook** (recommended for production) — Telegram pushes updates to a FastAPI endpoint
2. **Polling** (for dev/local) — long-polling via `getUpdates`

---

## Lean Implementation

### Files to Create

| File | Purpose |
|------|---------|
| `src/taskrunner/channels/telegram.py` | `TelegramChannel` + `TelegramBridge` + `register_plugin()` |
| `tests/test_telegram_channel.py` | Unit tests |

### Files to Modify

| File | Change |
|------|--------|
| `src/taskrunner/models.py` | Add `TelegramChannelConfig`, add `telegram` field to `ChannelsConfig` |
| `pyproject.toml` | Add entry point: `telegram = "taskrunner.channels.telegram:register_plugin"` |

### Dependencies
- `httpx` (already in deps) for Bot API HTTP calls — no heavy Telegram SDK needed
- OR `python-telegram-bot` (lighter than alternatives, async-native) — add as optional dep under `[telegram]` extra

**Recommendation:** Use raw `httpx` to keep it lean and consistent with the WhatsApp bridge approach. The Bot API is simple enough that a wrapper SDK isn't needed.

---

## Approach

### 1. TelegramChannelConfig (in `models.py`)

```python
class TelegramChannelConfig(BaseModel):
    """Telegram channel settings."""

    bot_token: str
    mode: str = "polling"  # "polling" or "webhook"
    webhook_path: str = "/webhooks/telegram"
    webhook_secret: str = ""  # X-Telegram-Bot-Api-Secret-Token header
    poll_interval: int = 1  # seconds between getUpdates (only in polling mode)
    allowed_senders: list[str] = Field(default_factory=list)  # user IDs or usernames
    allowed_chats: list[str] = Field(default_factory=list)  # chat IDs for groups
    send_typing: bool = True  # send "typing..." action while processing

    @model_validator(mode="after")
    def check_webhook_secret(self) -> TelegramChannelConfig:
        if self.mode == "webhook" and not self.webhook_secret:
            raise ValueError(
                "webhook_secret is recommended when mode is 'webhook'"
            )
        return self
```

Add to `ChannelsConfig`:
```python
class ChannelsConfig(BaseModel):
    # ... existing fields ...
    telegram: TelegramChannelConfig | None = None
```

### 2. TelegramBridge

Thin wrapper over the [Bot API](https://core.telegram.org/bots/api) using `httpx`:

```python
@dataclass
class TelegramMessage:
    chat_id: str
    sender_id: str
    sender_username: str | None
    text: str
    message_id: int
    timestamp: datetime
    is_group: bool
    reply_to_message_id: int | None = None
    media: list[TelegramMedia] | None = None  # photos, documents, voice, etc.

@dataclass
class TelegramMedia:
    type: str  # "photo", "document", "voice", "video", "sticker"
    file_id: str
    file_size: int | None = None
    mime_type: str | None = None


class TelegramBridge:
    """Low-level Telegram Bot API client."""

    BASE_URL = "https://api.telegram.org/bot{token}"

    def __init__(self, bot_token: str) -> None:
        self._token = bot_token
        self._base = self.BASE_URL.format(token=bot_token)
        self._client = httpx.Client(timeout=30)
        self._offset: int = 0  # for getUpdates

    def get_me(self) -> dict:
        """Verify bot token and get bot info."""
        return self._call("getMe")

    def get_updates(self, timeout: int = 25) -> list[TelegramMessage]:
        """Long-poll for new messages."""
        result = self._call("getUpdates", offset=self._offset, timeout=timeout)
        messages = []
        for update in result:
            self._offset = update["update_id"] + 1
            msg = update.get("message")
            if msg:
                messages.append(self._parse_message(msg))
        return messages

    def send_message(self, chat_id: str, text: str, reply_to: int | None = None) -> dict:
        """Send a text message."""
        params = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        if reply_to:
            params["reply_to_message_id"] = reply_to
        return self._call("sendMessage", **params)

    def send_typing(self, chat_id: str) -> None:
        """Send 'typing...' chat action."""
        self._call("sendChatAction", chat_id=chat_id, action="typing")

    def set_webhook(self, url: str, secret_token: str = "") -> dict:
        """Register a webhook URL with Telegram."""
        params = {"url": url}
        if secret_token:
            params["secret_token"] = secret_token
        return self._call("setWebhook", **params)

    def delete_webhook(self) -> dict:
        """Remove the webhook (switch to polling mode)."""
        return self._call("deleteWebhook")

    def get_file_url(self, file_id: str) -> str:
        """Get download URL for a file."""
        result = self._call("getFile", file_id=file_id)
        file_path = result["file_path"]
        return f"https://api.telegram.org/file/bot{self._token}/{file_path}"

    def health(self) -> dict[str, Any]:
        """Check bot connectivity."""
        try:
            me = self.get_me()
            return {"healthy": True, "bot_username": me.get("username")}
        except Exception as e:
            return {"healthy": False, "error": str(e)}

    def _call(self, method: str, **params) -> Any:
        """Make a Bot API call."""
        resp = self._client.post(f"{self._base}/{method}", json=params)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data.get('description')}")
        return data.get("result")

    def _parse_message(self, msg: dict) -> TelegramMessage:
        """Parse a raw Telegram message dict into TelegramMessage."""
        chat = msg["chat"]
        sender = msg.get("from", {})
        is_group = chat["type"] in ("group", "supergroup")

        # Extract text (could be caption for media messages)
        text = msg.get("text", "") or msg.get("caption", "")

        # Extract media
        media = []
        if msg.get("photo"):
            # photos come as array of sizes, take largest
            largest = max(msg["photo"], key=lambda p: p.get("file_size", 0))
            media.append(TelegramMedia(type="photo", file_id=largest["file_id"],
                                       file_size=largest.get("file_size")))
        for media_type in ("document", "voice", "video", "video_note", "sticker"):
            if msg.get(media_type):
                m = msg[media_type]
                media.append(TelegramMedia(type=media_type, file_id=m["file_id"],
                                           file_size=m.get("file_size"),
                                           mime_type=m.get("mime_type")))

        return TelegramMessage(
            chat_id=str(chat["id"]),
            sender_id=str(sender.get("id", "")),
            sender_username=sender.get("username"),
            text=text,
            message_id=msg["message_id"],
            timestamp=datetime.fromtimestamp(msg["date"], tz=timezone.utc),
            is_group=is_group,
            reply_to_message_id=msg.get("reply_to_message", {}).get("message_id"),
            media=media or None,
        )
```

### 3. TelegramChannel

```python
class TelegramChannel(WebhookChannelMixin, Channel):
    """Telegram messaging channel."""

    def __init__(
        self,
        bridge: TelegramBridge,
        *,
        mode: str = "polling",
        poll_interval: int = 1,
        allowed_senders: list[str] | None = None,
        allowed_chats: list[str] | None = None,
        webhook_path: str = "/webhooks/telegram",
        webhook_secret: str = "",
        send_typing: bool = True,
    ) -> None:
        self._bridge = bridge
        self._mode = mode
        self._poll_interval = poll_interval
        self._allowed_senders = set(allowed_senders or [])
        self._allowed_chats = set(allowed_chats or [])
        self._webhook_path = webhook_path
        self._webhook_secret = webhook_secret
        self._send_typing = send_typing
        self._callback: Callable[[str, str], str] | None = None
        self._bot_username: str | None = None

    def listen(self, callback: Callable[[str, str], str]) -> None:
        self._callback = callback

        # Fetch bot username for mention detection in groups
        try:
            me = self._bridge.get_me()
            self._bot_username = me.get("username", "").lower()
        except Exception:
            logger.warning("Could not fetch bot info")

        if self._mode == "webhook":
            self.set_webhook_callback(callback)
            logger.info("Telegram channel listening in webhook mode")
            self._webhook_listen_block()
        else:
            if self._mode == "polling":
                # Ensure no stale webhook
                self._bridge.delete_webhook()
            logger.info("Telegram channel listening in polling mode")
            self._poll_loop(callback)

        logger.info("Telegram channel stopped")

    def send(self, recipient: str, text: str) -> None:
        # Telegram has a 4096 char limit per message — chunk if needed
        max_len = 4096
        for i in range(0, len(text), max_len):
            self._bridge.send_message(recipient, text[i:i + max_len])
        logger.info("Sent Telegram message to %s (%d chars)", recipient, len(text))

    def stop(self) -> None:
        self._stop_requested = True

    # --- Access control ---

    def _is_allowed(self, msg: TelegramMessage) -> bool:
        """Check sender/chat allowlist."""
        if self._allowed_senders:
            sender_match = (
                msg.sender_id in self._allowed_senders
                or (msg.sender_username and msg.sender_username.lower()
                    in {s.lower().lstrip("@") for s in self._allowed_senders})
            )
            if not sender_match:
                return False

        if msg.is_group and self._allowed_chats:
            if msg.chat_id not in self._allowed_chats:
                return False

        return True

    def _is_mentioned(self, msg: TelegramMessage) -> bool:
        """Check if bot is mentioned in a group message."""
        if not msg.is_group:
            return True  # DMs always count

        if not self._bot_username:
            return False

        text_lower = msg.text.lower()
        return f"@{self._bot_username}" in text_lower

    # --- Polling mode ---

    def _poll_loop(self, callback: Callable[[str, str], str]) -> None:
        consecutive_errors = 0
        max_backoff = 60

        while not self._stop_requested:
            try:
                messages = self._bridge.get_updates(timeout=25)
                consecutive_errors = 0

                for msg in messages:
                    self._handle_message(msg, callback)

            except Exception:
                consecutive_errors += 1
                backoff = min(self._poll_interval * (2 ** consecutive_errors), max_backoff)
                logger.exception(
                    "Error polling Telegram (consecutive=%d, backoff=%.1fs)",
                    consecutive_errors, backoff,
                )
                time.sleep(backoff)
                continue

            time.sleep(self._poll_interval)

    def _handle_message(self, msg: TelegramMessage, callback: Callable[[str, str], str]) -> None:
        """Process a single incoming message."""
        if not msg.text and not msg.media:
            return

        if not self._is_allowed(msg):
            return

        if not self._is_mentioned(msg):
            return

        # Strip bot mention from text
        text = msg.text
        if self._bot_username:
            text = text.replace(f"@{self._bot_username}", "").strip()

        if not text:
            return

        logger.info("Telegram from %s in %s: %s", msg.sender_id, msg.chat_id, text[:80])

        if self._send_typing:
            try:
                self._bridge.send_typing(msg.chat_id)
            except Exception:
                pass

        response = callback(msg.chat_id, text)
        self.send(msg.chat_id, response)

    # --- Webhook mode ---

    def get_webhook_routes(self) -> list[dict[str, Any]] | None:
        if self._mode != "webhook":
            return None

        return [
            {
                "path": self._webhook_path,
                "method": "POST",
                "handler": self._handle_webhook,
            },
        ]

    async def _handle_webhook(self, request) -> dict:
        """Handle incoming Telegram webhook update."""
        from fastapi import HTTPException

        # Verify secret token header
        if self._webhook_secret:
            header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if header != self._webhook_secret:
                raise HTTPException(status_code=403, detail="Invalid secret token")

        import json
        raw_body = await request.body()
        update = json.loads(raw_body)

        msg_data = update.get("message")
        if not msg_data:
            return {"ok": True}

        msg = self._bridge._parse_message(msg_data)

        callback = self._webhook_callback or self._callback
        if callback:
            await asyncio.to_thread(self._handle_message, msg, callback)

        return {"ok": True}

    # --- Health ---

    def health_check(self) -> dict[str, Any]:
        bridge_health = self._bridge.health()
        return {
            "healthy": not self._stop_requested and bridge_health.get("healthy", False),
            "mode": self._mode,
            "bridge": bridge_health,
        }
```

### 4. register_plugin()

```python
def register_plugin() -> tuple[ChannelPluginMeta, Callable[[dict[str, Any]], Channel]]:
    from taskrunner.channels.plugin import ChannelCapability, ChannelPluginMeta
    from taskrunner.models import TelegramChannelConfig

    meta = ChannelPluginMeta(
        id="telegram",
        label="Telegram",
        capabilities=(
            ChannelCapability.POLLING
            | ChannelCapability.WEBHOOK
            | ChannelCapability.SEND
            | ChannelCapability.TYPING_INDICATOR
            | ChannelCapability.GROUP_CHAT
            | ChannelCapability.MEDIA
        ),
    )

    def factory(config: dict[str, Any]) -> TelegramChannel:
        cfg = TelegramChannelConfig(**config)
        bridge = TelegramBridge(cfg.bot_token)

        return TelegramChannel(
            bridge=bridge,
            mode=cfg.mode,
            poll_interval=cfg.poll_interval,
            allowed_senders=cfg.allowed_senders,
            allowed_chats=cfg.allowed_chats,
            webhook_path=cfg.webhook_path,
            webhook_secret=cfg.webhook_secret,
            send_typing=cfg.send_typing,
        )

    return meta, factory
```

---

## Prose

### How It Fits Together

The Telegram channel follows the exact same plugin pattern as WhatsApp:

1. `TelegramBridge` talks to the Bot API via `httpx` — no heavy SDK dependency. It handles `getUpdates` (polling), `sendMessage`, `sendChatAction` (typing), webhook setup, and file downloads.

2. `TelegramChannel` implements the `Channel` interface. In polling mode, it long-polls via `getUpdates` (Telegram supports long polling natively with a `timeout` parameter, so we get near-realtime with minimal overhead). In webhook mode, it registers a FastAPI POST route and blocks.

3. Access control works on two levels: `allowed_senders` (user IDs or @usernames) for DMs, and `allowed_chats` (chat IDs) for groups. In groups, messages are only processed if the bot is @mentioned — the bot username is fetched on startup via `getMe`.

4. The bot mention is stripped from the text before passing to the agent callback, so the agent sees clean input.

5. Messages over 4096 chars (Telegram's limit) are automatically chunked.

### Config Example

```yaml
channels:
  telegram:
    bot_token: "123456:ABC-DEF..."
    mode: polling          # or "webhook"
    allowed_senders:
      - "12345678"         # Telegram user ID
      - "@rossUsername"    # or username
    allowed_chats:
      - "-100123456789"   # group chat ID
    send_typing: true
    # webhook-only:
    # webhook_path: /webhooks/telegram
    # webhook_secret: "random-secret-string"
```

### Testing Strategy

Mirror `test_whatsapp_channel.py` structure:

1. **MockBridge** — in-memory `TelegramBridge` that returns canned messages and records sends
2. **Polling tests:**
   - Receives message, calls callback, sends response
   - Respects `allowed_senders` filter
   - Respects `allowed_chats` filter for groups
   - Mention detection in groups (only responds when @mentioned)
   - Strips bot mention from text
   - Handles consecutive errors with backoff
   - Stops cleanly when `stop()` called
3. **Webhook tests:**
   - Verifies secret token header
   - Rejects invalid/missing secret
   - Parses update payload correctly
   - Calls callback and sends response
4. **Bridge tests:**
   - `_parse_message` handles text, photos, documents, voice
   - `send_message` chunks long messages
   - `health()` returns status
   - `get_updates` advances offset correctly

### Edge Cases
- Bot receives edited messages (ignore for now — `edited_message` key)
- Bot receives channel posts vs group messages (handle `channel_post` later)
- User sends media without caption (skip if no text)
- Rate limiting from Telegram (retry with backoff, they allow ~30 msg/sec)
- Group chat without bot username set (can't detect mentions — log warning)

---

## Linting & Packaging

- Add `import asyncio` at top of `telegram.py`
- Entry point in `pyproject.toml`: `telegram = "taskrunner.channels.telegram:register_plugin"`
- No new pip extras needed (uses `httpx` which is already a dependency)
- Run `pytest tests/test_telegram_channel.py` to verify

---

## Estimated Scope

| Component | LOC (approx) |
|-----------|-------------|
| `TelegramBridge` | ~120 |
| `TelegramChannel` | ~180 |
| `TelegramChannelConfig` | ~20 |
| `register_plugin` + wiring | ~30 |
| Tests | ~300 |
| **Total** | **~650** |

Roughly a half-day of focused work. The existing WhatsApp channel is a near-perfect template.
