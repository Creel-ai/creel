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
│   │   ├── browser.py     # Browser control (Playwright)
│   │   └── process_manager.py # Background process management
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
│   │   ├── context.py     # Context pruning / token management
│   │   ├── config_reload.py # Hot-reload agent config
│   │   ├── safety.py      # Destructive command blocklist
│   │   ├── knowledge_base.py # Knowledge base indexing
│   │   ├── rate_limiter.py # LLM API rate limiting
│   │   ├── pairing.py     # Device pairing (TOTP)
│   │   ├── tool_cache.py  # Tool result caching
│   │   ├── providers/     # LLM provider backends (Anthropic, OpenAI, Google, Ollama)
│   │   ├── skills/        # Skill registry and definitions
│   │   ├── monitors/      # Proactive monitor system
│   │   ├── services/      # Media storage, transcription, vision
│   │   ├── channels/
│   │   │   ├── base.py        # Channel ABC
│   │   │   ├── plugin.py      # Plugin metadata and capabilities
│   │   │   ├── registry.py    # Channel discovery and registration
│   │   │   ├── sender_gate.py # Sender approval/deny access control
│   │   │   ├── sender_store.py# Persistent sender state
│   │   │   ├── message.py     # Message models
│   │   │   ├── mixins/        # Reusable channel mixins (polling, media, retry, etc.)
│   │   │   ├── imessage.py    # iMessage channel (polls chat.db)
│   │   │   ├── bluebubbles.py # BlueBubbles iMessage channel (REST API)
│   │   │   ├── telegram.py    # Telegram channel
│   │   │   ├── telegram_bridge.py # Telegram bridge
│   │   │   ├── whatsapp.py    # WhatsApp channel
│   │   │   ├── whatsapp_bridge.py # WhatsApp bridge
│   │   │   └── webhook.py     # Webhook channel
│   │   ├── cron/          # Cron scheduling subsystem
│   │   │   ├── models.py  # Cron data models
│   │   │   ├── manager.py # Cron job management
│   │   │   ├── executor.py# Cron job execution
│   │   │   ├── delivery.py# Result delivery
│   │   │   ├── store.py   # Job persistence
│   │   │   └── tool.py    # Cron tool definitions
│   │   ├── daemon/        # Background daemon with REST API
│   │   │   ├── api.py     # FastAPI app (main routes)
│   │   │   ├── api_auth.py    # Authentication endpoints
│   │   │   ├── api_chat.py    # Chat/messaging endpoints
│   │   │   ├── api_config.py  # Config endpoints
│   │   │   ├── api_cron.py    # Cron management endpoints
│   │   │   ├── api_dashboard.py # Dashboard endpoints
│   │   │   ├── api_files.py   # File management endpoints
│   │   │   ├── api_logs.py    # Log viewing endpoints
│   │   │   ├── api_pairing.py # Device pairing endpoints
│   │   │   ├── api_tasks.py   # Task management endpoints
│   │   │   ├── service.py # Daemon service lifecycle
│   │   │   ├── client.py  # Daemon API client
│   │   │   ├── contracts.py # API request/response models
│   │   │   └── watcher.py # File/config watcher
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
│   │   ├── bluebubbles/       # BlueBubbles iMessage relay
│   │   ├── clipboard/         # macOS clipboard (bridge)
│   │   ├── tts/               # Text-to-speech (ElevenLabs, OpenAI, local)
│   │   ├── dev_session/       # Containerized dev sessions
│   │   ├── host_exec/         # Host command execution (bridge)
│   │   ├── exec_interactive/  # Interactive PTY sessions
│   │   └── base/              # Shared base Docker image
│   └── guardian/
│       ├── core.py            # Guardian class (screen_input, validate_action)
│       ├── types.py           # Data models and config
│       ├── fast_classifier.py # DeBERTa/ONNX prompt-injection detector
│       ├── llm_judge.py       # Haiku-based secondary judge
│       ├── coherence.py       # LLM-based action coherence checker
│       ├── policy.py          # YAML policy engine (allow/review/deny/auto_approve)
│       ├── credential_scanner.py # Credential detection
│       ├── drift.py           # Input drift detection
│       ├── audit.py           # Privacy-preserving JSONL audit logger
│       ├── network.py         # Network policy enforcement
│       ├── overrides.py       # Temporary allow/deny overrides
│       └── pipeline.py        # Pipeline orchestration (parallel/sequential checks)
├── policies/
│   └── default.yaml       # Default tool action policies
├── tasks/                 # Task definitions (YAML)
├── src/llm/               # Containerized LLM runner + Dockerfile
├── dashboard/             # Web dashboard for monitoring
├── scripts/
│   ├── encrypt-secret.sh    # age encryption helper
│   └── setup-google-oauth.py# Google OAuth setup
├── secrets/               # Encrypted .env files (gitignored)
├── workspace/             # Agent workspace memory (gitignored)
└── tests/                 # Pytest suite (160+ test files)
```
