# Creel

<p align="center">
  <img src="assets/creel-logo.jpg" alt="Creel" width="400">
</p>

A secure LLM task runner and personal AI assistant that separates credential-bearing data fetching from LLM processing. Supports both scheduled tasks (morning briefings, weather summaries) and interactive agent mode (chat via CLI or iMessage with tool calling).

*A creel is a wicker basket usually used for carrying fish or blocks of peat. It is also the fish trap used to catch lobsters and other crustaceans.*

## Key Features

- **Secure by design** — The LLM never sees credentials. Each executor runs in isolation with only the secrets it needs.
- **Scheduled tasks** — Cron-based scheduling for recurring tasks like morning briefings and email digests.
- **Interactive agent mode** — Chat via CLI (with TUI) or iMessage with full tool calling support.
- **Guardian security pipeline** — Multi-stage input screening and action validation with prompt-injection detection, policy enforcement, and audit logging.
- **27+ executors** — Google Calendar, Gmail, Drive, Docs, Sheets, Slides, Apple Notes, Reminders, Things 3, iMessage, Telegram, WhatsApp, Notion, GitHub, web search, browser automation, and more.
- **Container isolation** — Production mode runs executors in Docker containers with read-only filesystems, dropped capabilities, and memory/CPU limits.
- **Secrets management** — age-encrypted secrets decrypted at runtime; the LLM never touches credential files.

## Quick Links

- [Quick Start](getting-started/quickstart.md) — Install and run your first task
- [Architecture](architecture/overview.md) — Security model and system design
- [Executors](executors/index.md) — Available data fetchers and integrations
- [CLI Reference](reference/cli.md) — All commands and options
