# Project Structure

```
creel/
├── runner.py              # CLI entry point
├── agent.yaml             # Global agent config (tools, channels, sessions, guardian)
├── pyproject.toml
├── .python-version        # pyenv Python version pin (3.12)
├── TESTING.md             # Testing guidelines and procedures
├── bridge/                # Host bridge server for macOS tool integration
├── creel/
│   ├── models.py          # Pydantic models (tasks, tools, agent config)
│   ├── orchestrator.py    # Core loop: fetch -> LLM -> output (simple + agent)
│   ├── agent.py           # Agent loop: LLM -> tool_use -> execute -> loop
│   ├── tools.py           # Tool definitions + executor execution bridge
│   ├── session.py         # JSON file-backed conversation sessions
│   ├── chat.py            # Chat server (channels + sessions + agent)
│   ├── tui.py             # Textual TUI for interactive chat
│   ├── memory.py          # File-based workspace memory (daily logs + long-term)
│   ├── prompt_builder.py  # System prompt assembly (memory, date, etc.)
│   ├── approvals.py       # Human-in-the-loop tool approval logic
│   ├── startup.py         # Secrets validation on startup
│   ├── log.py             # Logging setup (console + JSON modes)
│   ├── quiet_hours.py     # Quiet hours configuration and enforcement
│   ├── channels/
│   │   ├── base.py        # Channel ABC
│   │   ├── stdin.py       # Interactive CLI channel
│   │   ├── imessage.py    # iMessage channel (polls chat.db)
│   │   └── bluebubbles.py # BlueBubbles iMessage channel (REST API)
│   ├── scheduler.py       # APScheduler cron integration
│   ├── llm.py             # Anthropic API calls (direct + container + tools)
│   ├── outputs.py         # Output routing (iMessage, stdout, file)
│   └── secrets.py         # age encryption/decryption
├── executors/
│   ├── weather/           # wttr.in executor
│   ├── gcal/              # Google Calendar (read) executor
│   ├── gcal_write/        # Google Calendar (write) executor
│   ├── gmail_readonly/    # Gmail (read) executor
│   ├── gmail_send/        # Gmail (send) executor
│   ├── gmail_modify/      # Gmail (modify) executor
│   ├── drive/             # Google Drive (read) executor
│   ├── drive_write/       # Google Drive (write) executor
│   ├── bluebubbles/       # BlueBubbles iMessage executor
│   ├── brave_search/      # Brave web search executor
│   ├── fetch_url/         # URL content extractor
│   ├── apple_notes/       # Apple Notes executor (bridge)
│   ├── apple_reminders/   # Apple Reminders executor (bridge)
│   ├── things/            # Things 3 executor (bridge)
│   ├── imessage_bridge/   # iMessage executor (bridge)
│   └── exec/              # Sandboxed shell command executor
├── guardian/
│   ├── core.py            # Guardian class (screen_input, validate_action)
│   ├── types.py           # Data models and config
│   ├── fast_classifier.py # DeBERTa/ONNX prompt-injection detector
│   ├── llm_judge.py       # Haiku-based secondary judge
│   ├── coherence.py       # LLM-based action coherence checker
│   ├── policy.py          # YAML policy engine (allow/review/deny/auto_approve)
│   └── audit.py           # Privacy-preserving JSONL audit logger
├── policies/
│   └── default.yaml       # Default tool action policies
├── llm/                   # Containerized LLM runner + Dockerfile
├── tasks/                 # Task definitions (YAML)
├── sessions/              # Conversation sessions (gitignored)
├── workspace/             # Agent workspace memory (gitignored)
├── secrets/               # Encrypted .env files (gitignored)
├── scripts/
│   ├── encrypt-secret.sh    # age encryption helper
│   └── setup-google-oauth.py# Google OAuth setup (gcal, gmail)
└── tests/
```
