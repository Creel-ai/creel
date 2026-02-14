# Creel — Roadmap & TODO

> From POC to daily-driver personal agent. Each item marked: ✅ exists, 🟡 partial, 🔴 needs to be built.

---

## Phase 1: Foundation — Get It Running Reliably End-to-End

The goal: `./runner.py serve` runs without crashing, handles messages, executes tools, and recovers from errors.

### 1.1 End-to-End Smoke Test 🟡
- **What exists:** 2066 lines of tests across 15 files, decent coverage of individual modules
- **What's missing:** No integration test that boots the full `serve` pipeline (chat server + scheduler + iMessage channel) against a mock. Tests mock heavily but don't verify the wiring.
- **TODO:**
  - [ ] Add an integration test: `StdinChannel` → `ChatServer` → `run_agent_loop` → mock LLM → verify response flows back
  - [ ] Add a `conftest.py` with shared fixtures (mock Anthropic client, temp sessions dir, temp secrets)
  - [ ] CI pipeline (GitHub Actions) — run tests on push to `agent` branch

### 1.2 Error Handling & Resilience 🟡
- **What exists:** Try/except around LLM calls in `agent.py`, fetcher failures caught in `orchestrator.py`, corrupt session recovery in `session.py`
- **What's missing:** No retry logic anywhere. LLM API 429/500 → immediate failure. iMessage poll errors just log and continue (good) but no backoff. Fetcher timeouts not configurable.
- **TODO:**
  - [ ] Add retry with exponential backoff for LLM calls in `taskrunner/llm.py` (`call_llm` and `run_llm`)
  - [ ] Add configurable timeout per fetcher in `FetcherConfig` (currently hardcoded 60s in `_run_fetcher_container`)
  - [ ] Add exponential backoff to iMessage polling in `channels/imessage.py` on repeated errors
  - [ ] Graceful shutdown in `runner.py cmd_serve` — signal handler for SIGTERM/SIGINT to cleanly stop scheduler + listener

### 1.3 Session Management Hardening 🟡
- **What exists:** JSON file-backed sessions, per-sender, trimming to `max_history`, clear command
- **What's missing:** Session trimming is naive (chops oldest messages) — can break mid-tool-call. No session expiry/TTL. Agent loop mutates the message list in-place, then `chat.py` calls `_save()` directly (leaky abstraction).
- **TODO:**
  - [ ] Smart trimming: ensure trimmed history never starts mid-tool-call (always keep complete request/response pairs)
  - [ ] Session TTL — auto-clear sessions older than N hours (configurable in `SessionConfig`)
  - [ ] Clean up the save flow: `chat.py` currently calls `self._session_mgr._save(session)` (private method) — expose a proper `save_messages(sender_id, messages)` method

### 1.4 Configuration & Secrets 🟡
- **What exists:** `age` encryption via `pyrage`, `.env.enc` files, `AGE_IDENTITY_FILE` fallback
- **What's missing:** No validation that required secrets exist at startup. If `secrets/anthropic.env.enc` is missing, you find out mid-request. `.env` loading in `runner.py` uses `setdefault` (won't override), which is correct but undocumented.
- **TODO:**
  - [ ] Startup validation: verify all referenced `.enc` files exist and are decryptable before starting `serve`/`listen`
  - [ ] Document the secrets setup in README (currently just `.env.example` and `scripts/encrypt-secret.sh`)

### 1.5 Logging & Observability 🔴
- **What exists:** Python `logging` throughout, `guardian_audit.jsonl` for security events
- **What's missing:** No structured logging. No request-level correlation (can't trace a single message through the pipeline). No metrics.
- **TODO:**
  - [ ] Add a `request_id` (UUID) generated per incoming message, threaded through agent loop, tool calls, and audit log
  - [ ] Structured JSON logging option (for production; keep human-readable for dev)
  - [ ] Log response times: LLM call duration, tool execution duration, total request duration

### 1.6 Fetcher Output Structuring 🔴
- **What exists:** Fetchers print raw text to stdout. The entire stdout is returned as the tool result. Stderr is now captured and logged (PR #7).
- **What's missing:** No structured protocol between container and host. No way to distinguish data output from log/debug output. No metadata (timing, status codes, content type). No streaming support.
- **TODO:**
  - [ ] Define a lightweight output protocol (e.g., JSON envelope: `{"status": "ok", "data": "...", "meta": {...}}`)
  - [ ] Support mixed output: fetcher can emit structured logs to stderr + final result to stdout via envelope
  - [ ] Return metadata alongside result (HTTP status codes, API rate limits, response times from the fetcher's perspective)
  - [ ] Consider content-type awareness: fetchers that return JSON vs plain text vs binary (base64)
  - [ ] Backward-compatible: plain stdout still works (treated as raw text result), envelope is opt-in
  - [ ] Schema validation on envelope responses (reject malformed output early)
  - [ ] Add `CREEL_OUTPUT_FORMAT=envelope` env var so fetchers know to use the structured protocol
  - [ ] Update `_run_fetcher_container` to detect and parse envelope responses

---

## Phase 2: Security — Guardian Fully Operational

The goal: All three Guardian stages working, with a real human-in-the-loop review flow.

### 2.1 Fast Classifier (Stage 1) 🟡
- **What exists:** `guardian/fast_classifier.py` — loads DeBERTa via `optimum` ONNX or bare `transformers`, lazy-loaded, configurable threshold
- **What's missing:** No ONNX model export script. Falls back to PyTorch (slow, ~200ms vs ~10ms). No model warm-up at startup.
- **TODO:**
  - [ ] Add `scripts/export-onnx.py` to convert the HuggingFace model to ONNX format for `optimum`
  - [ ] Warm up the classifier on first `serve` startup (call `classify("test")` to trigger lazy load)
  - [ ] Add latency logging to `classify()` to track inference time
  - [ ] Consider CoreML export for Apple Silicon (would be fastest on Mac Mini)

### 2.2 LLM Judge (Stage 2) 🟡
- **What exists:** `guardian/llm_judge.py` — calls Haiku with a security-classifier system prompt, parses JSON response. Config exists in `agent.yaml` but `enabled: false`.
- **What's missing:** Never been tested in production. Falls through silently on failure (correct design, but no alerting). No cost tracking.
- **TODO:**
  - [ ] Enable in `agent.yaml` and test against real prompt injection attempts
  - [ ] Add latency/cost tracking to `judge()` (Haiku is cheap but not free)
  - [ ] Consider making Stage 2 conditional: only run if Stage 1 score is in the "uncertain" range (e.g., 0.5–0.85) to save cost
  - [ ] Add a `--no-judge` CLI flag for development (avoid burning API calls during testing)

### 2.3 Policy Engine (Stage 3) ✅ (but needs review flow)
- **What exists:** `guardian/policy.py` — YAML glob patterns, deny→review→allow evaluation order, unknown defaults to review. `policies/default.yaml` has sensible defaults.
- **What's missing:** The REVIEW verdict has no real approval mechanism over iMessage. In `cmd_chat` there's a `_confirm_action` via stdin prompt, but `cmd_listen`/`cmd_serve` pass `confirm_fn=None` to `ChatServer`, which means REVIEW actions **silently proceed** (see `agent.py` line: `if confirm_action is not None and not confirm_action(...)`).
- **TODO:**
  - [ ] **Critical:** Implement iMessage-based approval flow for REVIEW actions:
    1. Send a confirmation message: "⚠️ Approve: send_email(to: foo@bar.com, subject: ...)? Reply Y/N"
    2. Wait for response (with timeout, default deny)
    3. Continue or deny based on response
  - [ ] Add `auto_approve_review` config option (default `false`) for users who want to live dangerously
  - [ ] Add per-tool review overrides (e.g., `mark_read` could auto-approve but `send_email` always asks)

### 2.4 Audit Log Enhancement 🟡
- **What exists:** `guardian/audit.py` — JSONL append-only, SHA-256 hashed inputs, key-only tool args. Logs `screen_input` and `validate_action` events.
- **What's missing:** No log rotation. No audit of actual tool *results*. No way to review/query audit logs.
- **TODO:**
  - [ ] Add audit entry for tool execution results (success/failure, duration, output length — not output content)
  - [ ] Log rotation: daily files or size-based rotation
  - [ ] Simple CLI command: `./runner.py audit [--tail N] [--blocked] [--denied]` to review recent events

---

## Phase 3: Daily Driver — Features Needed to Actually Use It

The goal: Replace OpenClaw for daily personal agent tasks.

### 3.1 Memory & Context Management 🔴
- **What exists:** Session history (last 50 messages per sender). System prompt has `{date}` only.
- **What's missing:** No long-term memory across sessions. No user profile/preferences. No summarization of old context. Each `clear` wipes everything.
- **TODO:**
  - [ ] **Memory store:** `memory/` directory with structured JSONL or SQLite — facts, preferences, past decisions
  - [ ] **Session summarization:** When trimming old messages, summarize them into a compact context block instead of dropping
  - [ ] **System prompt enrichment:** Inject relevant memories into system prompt (RAG-lite — keyword match on user message → retrieve relevant memories)
  - [ ] **Memory tools:** `remember` and `recall` tools the LLM can call to explicitly store/retrieve information
  - [ ] Model: `taskrunner/memory.py` with `MemoryStore` class

### 3.2 System Prompt & Personality 🟡
- **What exists:** Bare-bones system prompt in `agent.yaml`: "You are a personal assistant. Be concise and helpful."
- **What's missing:** No personality, no user context, no tool usage guidance, no behavioral rules.
- **TODO:**
  - [ ] Write a proper system prompt (personality, tone, user context, tool descriptions, behavioral guidelines)
  - [ ] Support `system_prompt_file` in `agent.yaml` to keep it in a separate `.md` file
  - [ ] Include memory context injection point in system prompt template

### 3.3 Cron/Scheduling Improvements 🟡
- **What exists:** APScheduler with cron triggers, `BlockingScheduler` in background thread, task YAML with 5-part cron expressions
- **What's missing:** No way to schedule from chat ("remind me at 3pm"). Scheduler runs in a daemon thread — if it crashes, no restart. No way to list/cancel scheduled jobs at runtime.
- **TODO:**
  - [ ] Add `schedule_task` and `cancel_task` tools so the LLM can create one-off reminders
  - [ ] Use `BackgroundScheduler` instead of `BlockingScheduler` in `cmd_serve` (current threading approach works but is fragile)
  - [ ] Add `./runner.py jobs` command to list active scheduled jobs
  - [ ] Persistent job store (SQLite via APScheduler's built-in support) so scheduled items survive restarts

### 3.4 Web Fetching / Browsing 🔴
- **What exists:** Nothing
- **TODO:**
  - [ ] Add `web_search` fetcher (Brave Search API or similar)
  - [ ] Add `web_fetch` fetcher (fetch URL → markdown via `trafilatura` or `readability`)
  - [ ] Add corresponding tools in `agent.yaml`
  - [ ] Policy: `web_search` → allow, `web_fetch` → allow (read-only)

### 3.5 Notification System / Proactive Outreach 🔴
- **What exists:** Tasks can send output via iMessage (`outputs.py`), but only triggered by cron schedule. No proactive agent-initiated messages.
- **What's missing:** No way for the agent to reach out unprompted (e.g., "you have a meeting in 30 minutes").
- **TODO:**
  - [ ] Add a `notify` tool that sends an iMessage to the user (uses existing `_send_imessage` from `outputs.py`)
  - [ ] Scheduled check tasks: "every 30 min, check calendar for upcoming events, notify if <1hr away"
  - [ ] Rate limiting on notifications (don't spam)
  - [ ] Quiet hours configuration

### 3.6 Multiple Channels 🟡
- **What exists:** `Channel` ABC, `IMessageChannel`, `StdinChannel`. Only one channel active at a time.
- **What's missing:** No way to run multiple channels simultaneously. No channel abstraction for responses (the `ChatServer.handle_message` returns a string, but doesn't know *which* channel to reply on).
- **TODO:**
  - [ ] Multi-channel support in `cmd_serve`: run iMessage + potential future channels concurrently
  - [ ] Channel-aware responses: include channel context so the agent knows if it's talking via iMessage vs CLI
  - [ ] Future channels to consider: Signal, Telegram, SMS (Twilio), webhook/HTTP API

### 3.7 File & Media Handling 🔴
- **What exists:** `upload_file` tool (text to Google Drive). iMessage channel sends plain text only.
- **What's missing:** Can't receive or send images/files via iMessage. No local file read/write tools.
- **TODO:**
  - [ ] iMessage attachment support: detect and extract attachments from `chat.db` (they're in `~/Library/Messages/Attachments/`)
  - [ ] `read_file` / `write_file` tools for local filesystem (sandboxed to a specific directory)
  - [ ] Image handling: pass images to Claude's vision API when received via iMessage

### 3.8 Things 3 / Task Management Integration 🔴
- **What exists:** Nothing (but Ross uses Things 3 heavily per TOOLS.md)
- **TODO:**
  - [ ] Add `things3` fetcher using Things URL scheme or AppleScript
  - [ ] Tools: `create_task`, `list_tasks`, `complete_task`
  - [ ] Policy: `create_task` → review, `list_tasks` → allow, `complete_task` → review

### 3.9 Deployment & Daemonization 🔴
- **What exists:** `./runner.py serve` runs in foreground. No process management, no auto-restart.
- **TODO:**
  - [ ] `launchd` plist for macOS — auto-start on boot, restart on crash
  - [ ] `scripts/install-service.sh` to install/uninstall the launchd service
  - [ ] Stdout/stderr redirect to log files with rotation
  - [ ] Health check endpoint or file (touch a file every N seconds, alert if stale)
  - [ ] PID file to prevent double-starts

---

## Phase 4: Polish — Optimization & Nice-to-Haves

### 4.1 ONNX / CoreML Optimization for Fast Classifier 🔴
- **What exists:** ONNX path exists in code but no exported model
- **TODO:**
  - [ ] Export DeBERTa to ONNX (`optimum-cli export onnx`)
  - [ ] Benchmark: PyTorch vs ONNX vs CoreML on M-series
  - [ ] If CoreML wins, add `coremltools` export path in `fast_classifier.py`

### 4.2 Streaming Responses 🔴
- **What exists:** `call_llm` returns a complete `Message` object. No streaming.
- **TODO:**
  - [ ] Add `call_llm_stream` using `client.messages.stream()` 
  - [ ] Stream partial responses to iMessage (tricky — may need to buffer and send in chunks)
  - [ ] Streaming for `StdinChannel` (easy — print tokens as they arrive)

### 4.3 Cost & Usage Tracking 🔴
- **TODO:**
  - [ ] Track token usage per request (input/output tokens from Anthropic response)
  - [ ] Daily/weekly cost summaries
  - [ ] Budget alerts (configurable max spend per day/month)

### 4.4 Multi-Model Support 🔴
- **What exists:** Model name is configurable in `agent.yaml`, but code is Anthropic-only
- **TODO:**
  - [ ] Abstract LLM interface so fetchers/tools don't depend on Anthropic SDK types
  - [ ] Add OpenAI-compatible backend (for local models via Ollama, or GPT-4)
  - [ ] Model routing: use cheaper models for simple queries, expensive for complex

### 4.5 Testing & Quality 🟡
- **What exists:** 15 test files, ~2000 lines. Good unit test structure. Fixtures for benign/injection inputs.
- **TODO:**
  - [ ] Increase coverage: `channels/imessage.py`, `chat.py`, `scheduler.py` have no tests
  - [ ] Property-based tests for `_sanitize_sender_id` and `_parse_env`
  - [ ] Load test: simulate rapid messages to verify session file locking (currently none — concurrent writes could corrupt)
  - [ ] Add file locking to `SessionManager._save()` (use `fcntl.flock` or `filelock`)

### 4.6 Developer Experience 🟡
- **TODO:**
  - [ ] `./runner.py doctor` — check all dependencies, secrets, permissions (Full Disk Access for chat.db, etc.)
  - [ ] Better error messages when secrets are missing or decryption fails
  - [ ] Hot-reload `agent.yaml` and `policies/default.yaml` without restarting `serve`
  - [ ] `./runner.py replay <session_file>` — replay a session for debugging

---

## Phase 5: Web UI — Configuration & Dashboard

The goal: A local web interface that replaces YAML editing and provides operational visibility.

### 5.1 Approval Queue UI 🔴 (do this first)
- **What exists:** iMessage Y/N flow (clunky, async but awkward UX)
- **TODO:**
  - [ ] Local web server (FastAPI or similar) serving a simple approval dashboard
  - [ ] Pending actions list with approve/deny buttons
  - [ ] Action details: tool name, args, policy rule that triggered REVIEW, timestamp
  - [ ] History of past approvals/denials
  - [ ] Optional: push notifications to browser when new action is pending

### 5.2 Guardian Dashboard 🔴
- **TODO:**
  - [ ] Audit log viewer with filtering (blocked, denied, by tool, by date)
  - [ ] Classifier stats: hit rate, average confidence, false positive tracking
  - [ ] LLM judge usage: calls, tokens, cost
  - [ ] Real-time feed of guardian events

### 5.3 Configuration Editor 🔴
- **TODO:**
  - [ ] Executor management: add/edit/toggle executors, secrets mapping, timeout config
  - [ ] Policy editor: visual deny/review/allow rules (drag to reorder, glob pattern helper)
  - [ ] Tool editor: map tools to executors, set fixed args, parameter schemas
  - [ ] Guardian settings: thresholds, enable/disable stages, model selection
  - [ ] System prompt editor with live preview
  - [ ] Validate config before saving (schema check + dry run)

### 5.4 Operational Dashboard 🔴
- **TODO:**
  - [ ] Active sessions and conversation history
  - [ ] Scheduled tasks overview (cron jobs, next run times)
  - [ ] Token usage and cost tracking (daily/weekly/monthly)
  - [ ] Executor health: last run, success rate, average duration
  - [ ] Channel status (connected/disconnected)

### 5.5 Tech Stack Considerations
- Local-only by default (bind to 127.0.0.1)
- Auth: simple token or local-only (no auth needed if localhost-only)
- Lightweight: FastAPI + htmx or similar (no heavy SPA framework)
- Config changes write back to YAML files (source of truth stays in files)
- Dashboard reads from audit log + session files (no separate database needed initially)

---

## Priority Order (Suggested)

If doing this incrementally alongside OpenClaw:

1. **Phase 2.3** — Fix the REVIEW silent-proceed bug (security-critical)
2. **Phase 1.2** — Retry logic for LLM calls (reliability)
3. **Phase 1.1** — Integration test (confidence to change things)
4. **Phase 3.2** — System prompt (makes it *feel* like a real agent)
5. **Phase 3.1** — Memory (what makes it *your* agent)
6. **Phase 3.9** — Daemonization (run it 24/7)
7. **Phase 3.5** — Notifications (proactive = useful)
8. **Phase 3.4** — Web fetching (broadens capability)
9. **Phase 2.1** — ONNX export (speed)
10. Everything else as needed

---

## Architecture Notes

**What's well-designed:**
- Clean separation: fetchers → tools → agent loop → channels
- Guardian pipeline is properly layered (fast→expensive→policy)
- Audit log is privacy-preserving by default (hashes, key-only)
- Docker container security is solid (read-only, cap-drop, memory limits)
- `age` encryption for secrets is a good choice

**What needs rethinking:**
- `orchestrator.py` has a growing `_run_fetcher_inline` switch statement — needs a registry pattern
- `_load_secrets_to_env` mutates `os.environ` globally — not safe for concurrent requests
- The `ChatServer` → `run_agent_loop` flow mutates the message list in-place, then saves — fragile
- No dependency injection — Guardian, session manager, etc. are created inside `ChatServer.__init__`
