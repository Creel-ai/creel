# Telegram Bot Setup

This guide walks you through setting up a Telegram bot as a channel for Creel.

## 1. Create a Telegram Bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts to name your bot
3. BotFather will give you a **bot token** — a string like `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`
4. Save the token securely (see step 3)

## 2. Configure Creel

Add a `telegram` section under `channels` in your `agent.yaml`:

**Polling mode** (recommended for local development):

```yaml
channels:
  telegram:
    secrets: secrets/telegram.env.enc
    mode: polling
    poll_timeout: 30
    send_typing: true
    allowed_senders:
      - "123456789"        # your Telegram user ID
```

**Webhook mode** (recommended for production):

```yaml
channels:
  telegram:
    secrets: secrets/telegram.env.enc
    mode: webhook
    webhook_path: /webhooks/telegram
    webhook_secret: "$TELEGRAM_WEBHOOK_SECRET"
    send_typing: true
    allowed_senders:
      - "@yourusername"
```

## 3. Store the Token

Encrypt your bot token with age (same pattern as all other secrets):

```bash
echo "TELEGRAM_BOT_TOKEN=123456:ABC-DEF..." > secrets/telegram.env
creel encrypt secrets/telegram.env --delete
```

This produces `secrets/telegram.env.enc` and removes the plaintext file. The token is decrypted at startup and never stored in plaintext.

## 4. Polling vs Webhook

| | Polling | Webhook |
|---|---|---|
| **How it works** | Bot calls `getUpdates` with long-polling | Telegram sends updates to your HTTPS endpoint |
| **Best for** | Local development, NAT/firewall environments | Production servers with a public URL |
| **Latency** | Slightly higher | Near-instant |
| **Requirements** | None | Public HTTPS URL, `webhook_secret` |

## 5. Webhook Setup

If using webhook mode:

1. Expose your Creel instance on a public HTTPS URL (e.g. via nginx, Caddy, or a cloud provider)
2. Set `webhook_secret` to a random string — Telegram will send it in the `X-Telegram-Bot-Api-Secret-Token` header
3. The webhook endpoint will be at `https://yourdomain.com/webhooks/telegram` (matching `webhook_path`)

## 6. Access Control

**Finding your user ID:** Send a message to `@userinfobot` on Telegram — it will reply with your numeric user ID.

**Finding a chat ID:** Add the bot to a group, then check the bot's `getUpdates` output for the `chat.id` field.

Configure access:

```yaml
allowed_senders:
  - "123456789"          # numeric user ID
  - "@yourusername"      # @username (case-sensitive)
allowed_chats:
  - "-1001234567890"     # group chat ID (negative number)
```

If both lists are empty, all senders and chats are allowed.

## 7. Group Chat

1. Add your bot to a Telegram group
2. In groups, the bot only responds to messages containing `@yourbotusername`
3. The `@mention` is stripped before the message is passed to the agent

**Privacy mode:** By default, Telegram bots in groups only receive messages that mention them or start with `/`. This is the desired behavior for Creel. If you need the bot to see all messages, disable privacy mode via BotFather (`/setprivacy`).

## 8. Test It

> **Important:** You must send a message to your bot first before it can reply.
> Telegram bots cannot initiate conversations — open a chat with your bot
> and send any message (even "hi") to establish the conversation.

```bash
creel daemon start
```

Send a message to your bot in Telegram. You should see the bot typing, followed by a response.
