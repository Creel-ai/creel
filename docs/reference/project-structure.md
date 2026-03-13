# Project Structure

```
creel/
├── agent.yaml             # Global agent config (tools, channels, sessions, guardian)
├── pyproject.toml
├── .python-version        # pyenv Python version pin (3.12.11)
├── TESTING.md             # Testing guidelines and procedures
├── src/
│   ├── bridge/            # Host bridge server for macOS tool integration
│   │   ├── server.py      # FastAPI bridge server (localhost:8099)
│   │   └── browser.py     # Browser control
│   ├── creel/
│   │   ├── cli.py         # CLI entry point (`creel` command)
│   │   ├── agent.py       # Agent loop: LLM -> tool_use -> execute -> loop
│   │   ├── orchestrator.py# Core loop: fetch -> LLM -> output (simple + agent)
│   │   ├── llm.py         # Anthropic API calls (direct + container + tools)
│   │   ├── models.py      # Pydantic models (tasks, tools, agent config)
│   │   ├── tools.py       # Tool definitions + executor execution bridge
│   │   ├── session.py     # JSON file-backed conversation sessions
│   │   ├── chat.py        # Chat server (channels + sessions + agent)
│   │   ├── tui.py         # Textual TUI for interactive chat
│   │   ├── memory.py      # File-based workspace memory (daily logs + long-term)
│   │   ├── prompt_builder.py # System prompt assembly (memory, date, etc.)
│   │   ├── approvals.py   # Human-in-the-loop tool approval logic
│   │   ├── containers.py  # Docker container execution
│   │   ├── container_pool.py # Container pool management
│   │   ├── container_agent.py # Container-aware agent loop
│   │   ├── outputs.py     # Output routing (iMessage, stdout, file)
│   │   ├── secrets.py     # age encryption/decryption
│   │   ├── scheduler.py   # APScheduler cron integration
│   │   ├── validation.py  # Input validation
│   │   ├── oauth.py       # OAuth 2.0 flow handling
│   │   ├── startup.py     # Secrets validation on startup
│   │   ├── log.py         # Logging setup (console + JSON modes)
│   │   ├── quiet_hours.py # Quiet hours configuration and enforcement
│   │   ├── media.py       # Media handling
│   │   ├── init.py        # `creel init` wizard
│   │   ├── paths.py       # Path constants
│   │   ├── channels/
│   │   │   ├── base.py        # Channel ABC
│   │   │   ├── imessage.py    # iMessage channel (polls chat.db)
│   │   │   ├── bluebubbles.py # BlueBubbles iMessage channel (REST API)
│   │   │   ├── telegram.py    # Telegram channel
│   │   │   ├── telegram_bridge.py # Telegram bridge
│   │   │   ├── whatsapp.py    # WhatsApp channel
│   │   │   ├── whatsapp_bridge.py # WhatsApp bridge
│   │   │   └── webhook.py     # Webhook channel
│   │   ├── cron/          # Cron scheduling subsystem
│   │   │   ├── manager.py # Cron job management
│   │   │   ├── executor.py# Cron job execution
│   │   │   ├── delivery.py# Result delivery
│   │   │   ├── store.py   # Job persistence
│   │   │   └── tool.py    # Cron tool definitions
│   │   ├── daemon/        # Background daemon with REST API
│   │   │   ├── api.py     # FastAPI app (tasks, cron, dashboard, auth, logs)
│   │   │   ├── service.py # Daemon service lifecycle
│   │   │   ├── client.py  # Daemon API client
│   │   │   └── contracts.py # API request/response models
│   │   └── subagents/     # Sub-agent delegation
│   │       ├── manager.py # Sub-agent orchestration
│   │       ├── executor.py# Sub-agent execution
│   │       └── models.py  # Sub-agent data models
│   ├── executors/
│   │   ├── weather/           # wttr.in executor
│   │   ├── gcal/              # Google Calendar (read)
│   │   ├── gcal_write/        # Google Calendar (write)
│   │   ├── gmail_readonly/    # Gmail (read)
│   │   ├── gmail_send/        # Gmail (send)
│   │   ├── gmail_modify/      # Gmail (modify)
│   │   ├── drive/             # Google Drive (read)
│   │   ├── drive_write/       # Google Drive (write)
│   │   ├── google_docs/       # Google Docs
│   │   ├── google_sheets/     # Google Sheets
│   │   ├── google_slides/     # Google Slides
│   │   ├── github/            # GitHub CLI wrapper
│   │   ├── git_ops/           # Git operations
│   │   ├── notion/            # Notion (read)
│   │   ├── notion_write/      # Notion (write)
│   │   ├── browser/           # Playwright browser automation
│   │   ├── brave_search/      # Brave web search
│   │   ├── fetch_url/         # URL content extractor
│   │   ├── exec/              # Sandboxed shell command executor
│   │   ├── coding/            # Development environment
│   │   ├── file_ops/          # File operations
│   │   ├── apple_notes/       # Apple Notes (bridge)
│   │   ├── apple_reminders/   # Apple Reminders (bridge)
│   │   ├── things/            # Things 3 (bridge)
│   │   ├── imessage_bridge/   # iMessage (bridge)
│   │   └── bluebubbles/       # BlueBubbles iMessage relay
│   └── guardian/
│       ├── core.py            # Guardian class (screen_input, validate_action)
│       ├── types.py           # Data models and config
│       ├── fast_classifier.py # DeBERTa/ONNX prompt-injection detector
│       ├── llm_judge.py       # Haiku-based secondary judge
│       ├── coherence.py       # LLM-based action coherence checker
│       ├── policy.py          # YAML policy engine (allow/review/deny/auto_approve)
│       ├── credential_scanner.py # Credential detection
│       ├── drift.py           # Input drift detection
│       └── audit.py           # Privacy-preserving JSONL audit logger
├── policies/
│   └── default.yaml       # Default tool action policies
├── tasks/                 # Task definitions (YAML)
├── llm/                   # Containerized LLM runner + Dockerfile
├── dashboard/             # Web dashboard for monitoring
├── scripts/
│   ├── encrypt-secret.sh    # age encryption helper
│   └── setup-google-oauth.py# Google OAuth setup
├── secrets/               # Encrypted .env files (gitignored)
├── workspace/             # Agent workspace memory (gitignored)
└── tests/                 # Pytest suite (120+ test files)
```
