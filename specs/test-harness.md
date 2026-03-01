# Creel Integration Test Harness

## Problem

Creel has extensive unit tests, but they're almost entirely mocked. Nothing tests the real flow: message arrives → agent processes → tool executes → response sent. A feature can pass all unit tests and still be broken end-to-end.

## Goal

A test harness that agents (Ralph, Claude, any coding agent) can run with **one command** to verify Creel actually works:

```bash
./scripts/test-harness.sh
# Exit code 0 = all good, 1 = failures
# Output: test-results/report.json + test-results/logs/
```

## Entry Points

There are two scripts with different scopes:

| Script | Scope | API key needed? |
|--------|-------|-----------------|
| `scripts/test-harness.sh` | Mock-LLM integration tests only | No |
| `scripts/test-harness/harness.sh` | Full suite: unit + integration + Playwright e2e | Yes (for e2e phase) |

Use `test-harness.sh` for fast CI and agent workflows. Use `harness.sh` when you need the complete picture including browser e2e tests.

## Architecture

```
scripts/
├── test-harness.sh                  # Entry point — mock-LLM integration tests
├── test-harness/
│   ├── harness.py                   # Python orchestrator (setup/teardown/report)
│   ├── harness.sh                   # Full suite runner (unit + integration + e2e)
│   ├── mock_llm_server.py           # Mock Anthropic Messages API server
│   ├── config/
│   │   ├── agent.yaml               # Test agent config (mock LLM, fake creds)
│   │   └── policies/
│   │       └── test-policy.yaml     # Guardian deny/review rules for testing
│   ├── fixtures/
│   │   └── llm_triggers.json        # Scripted LLM responses (regex → response)
│   ├── scenarios/
│   │   ├── conftest.py              # Shared fixtures and helpers
│   │   ├── test_basic_chat.py       # Basic message → response
│   │   ├── test_tool_execution.py   # Tool calls end-to-end
│   │   ├── test_telegram.py         # Telegram webhook flow
│   │   ├── test_cron.py             # Cron job lifecycle
│   │   ├── test_guardian.py         # Guardian security blocking
│   │   └── test_sessions.py         # Session persistence and isolation
│   ├── test-config/
│   │   └── agent.yml                # Real-LLM config (for e2e phase)
│   └── tests/
│       ├── conftest.py              # sys.path setup for mock_llm_server imports
│       └── test_mock_llm_server.py  # Unit tests for the mock server itself
```

## Components

### 1. Mock LLM Server (`mock_llm_server.py`)

A FastAPI server implementing the **Anthropic Messages API** (`POST /v1/messages`). This is the key piece — it lets tests exercise the full pipeline without burning tokens or depending on external APIs.

Runs on `127.0.0.1:18999` by default.

**Behavior modes:**

- **Echo mode** — returns `Echo: {user_message}` for any input not matching a trigger
- **Scripted mode** — returns pre-configured responses when input matches a regex trigger (from `fixtures/llm_triggers.json`)
- **Tool call mode** — returns `tool_use` blocks to trigger tool execution, with configurable followup text after the tool result
- **Streaming mode** — returns SSE events in Anthropic streaming format when `stream: true`
- **Error injection** — `POST /v1/mock/error` makes the next N requests return a given status code

**Mock Telegram Bot API** — the server also mocks Telegram endpoints (`/bot{token}/getMe`, `/bot{token}/sendMessage`, etc.) so the Telegram channel can function without a real bot.

**Control endpoints:**

| Endpoint | Purpose |
|----------|---------|
| `POST /v1/mock/reset` | Reset all state (history, errors, followups, telegram) |
| `GET /v1/mock/history` | Return all recorded LLM request history |
| `POST /v1/mock/error` | Inject errors for next N requests |
| `GET /v1/mock/telegram/messages` | Return all messages sent via mock sendMessage |
| `GET /health` | Health check with trigger count |

**Trigger format** (`fixtures/llm_triggers.json`):

```json
{
  "triggers": [
    {
      "match": "^hello$|^hi$|^hey$",
      "response": {"type": "text", "content": "Hello! I'm the test agent."},
      "description": "Greeting"
    },
    {
      "match": "run echo test",
      "response": {
        "type": "tool_call",
        "tool": "exec",
        "args": {"command": "echo test"}
      },
      "followup": "The command output: {tool_result}",
      "description": "Simple exec tool call"
    },
    {
      "match": ".*",
      "response": {"type": "text", "content": "Echo: {user_message}"},
      "description": "Default echo fallback (must be last)"
    }
  ]
}
```

Triggers support `{user_message}`, `{tool_result}`, and `{match_N}` (regex capture groups) as template placeholders.

### 2. Test Config (`config/agent.yaml`)

A minimal agent config with fake credentials that points LLM calls at the mock server via `ANTHROPIC_BASE_URL` env var:

- **LLM**: Uses the Anthropic SDK with a fake API key; `ANTHROPIC_BASE_URL` redirects to the mock server
- **Tools**: Only `exec` (shell commands) is enabled
- **Guardian**: Policy engine enabled, fast classifier and LLM judge disabled (no ONNX model or real LLM in tests)
- **Telegram**: Webhook mode with fake bot token; `api_base_url` points at the mock server's Telegram endpoints
- **Sessions**: Local file-based storage in `config/sessions/`

### 3. Orchestrator (`harness.py`)

Python class that manages the full lifecycle:

1. **Setup** — starts mock LLM server, waits for health check, starts Creel daemon with test config, waits for daemon health via Unix domain socket
2. **Run** — invokes `pytest` on the scenarios directory, passing `HARNESS_DAEMON_SOCKET` and `HARNESS_MOCK_LLM_URL` env vars
3. **Report** — parses pytest output and writes `test-results/report.json`
4. **Teardown** — stops daemon and mock server, closes log handles, cleans up test data directories

The daemon runs with `CREEL_STATE_DIR` set to a test-local directory (`scripts/test-harness/run/state/`) so it never touches `~/.creel/`.

### 4. Shared Test Fixtures (`scenarios/conftest.py`)

Provides session-scoped `httpx` clients for the daemon (via UDS) and mock server, plus common helpers:

- `send_message(client, text, sender_id, session_id, auto_approve)` — POST to `/v1/messages`
- `get_tool_result_from_followup_call(history)` — extract the `tool_result` block from the mock LLM's second call in a tool-use flow
- `read_audit_entries(path)` — parse the Guardian JSONL audit log

An `autouse` fixture resets mock LLM state and truncates the audit log before each test.

## Test Scenarios

### Basic Chat (`test_basic_chat.py`)

Tests the message → LLM → response pipeline.

- Greeting returns scripted response from trigger
- Unmatched messages fall through to echo
- Mock LLM history confirms the daemon forwarded the message
- Session created automatically for new senders
- Session history contains user + assistant messages
- Second message includes prior history in LLM context
- `/clear` resets history (LLM no longer sees prior messages)
- Empty message returns 422 validation error
- Different senders get isolated sessions with separate history

### Tool Execution (`test_tool_execution.py`)

Tests the tool call → execute → result → followup pipeline.

- `run echo test` triggers exec tool, output appears in final response
- Mock LLM history contains `tool_result` with actual command output
- Tool result is JSON with `exit_code`, `stdout`, `stderr`, `success`
- Failing command (`false`) has non-zero `exit_code` and `success: false`
- Guardian-blocked commands (`rm -rf /`, `curl | bash`) produce denial in `tool_result` with `is_error: true`
- Unknown tool name handled gracefully (no daemon crash)

### Telegram (`test_telegram.py`)

Tests the Telegram webhook flow end-to-end.

- Text update via webhook → agent processes → mock `sendMessage` called with response
- Greeting trigger produces expected response text
- Photo with caption is processed; photo without caption is silently ignored
- Voice message without text is silently ignored
- Unknown sender (not in `allowed_senders`) is ignored — no LLM call, no sendMessage
- Malformed updates (empty body, missing `message` field) return 200 OK (no crash)
- Valid webhook secret accepted; invalid/missing secret returns 403

### Cron Jobs (`test_cron.py`)

Tests the cron job CRUD lifecycle via the daemon API.

- Create job returns 201 with ID, appears in list
- Get single job, 404 for nonexistent
- Manual trigger returns 202, creates run record with timestamps and `status: success`
- Update job prompt/name, verify persistence
- Delete job removes from list
- Disable/enable toggles `enabled` field
- Disabled job can still be manually triggered
- Update prompt → re-trigger → LLM receives updated prompt

### Guardian (`test_guardian.py`)

Tests that Guardian blocks dangerous commands and logs events.

- `rm -rf /` blocked: `tool_result` contains denial, `is_error: true`, command not executed
- `curl | bash` blocked similarly
- Safe command (`echo hello`) allowed: `tool_result` contains actual output, `is_error` not set
- Prompt injection in tool args (`ignore previous instructions`) blocked by `deny_when` pattern
- Audit log contains `validate_action` entries with `verdict=deny` and `matched_rule`
- Audit log contains `action_outcome` with `outcome=denied_by_policy`
- Allowed commands produce `verdict=allow` audit entries

### Sessions (`test_sessions.py`)

Tests session persistence and isolation.

- Different senders get different session IDs
- Same sender reuses the same session across messages
- History API returns user + assistant messages, reflects multiple exchanges
- History is isolated between senders (A's history has no B's messages)
- LLM receives full prior history on subsequent messages
- `/clear` only affects the targeted sender; other senders' history is untouched
- Session list API is scoped to sender
- Session metadata includes `created_at`, `last_active`, `message_count`
- Active session endpoint returns current session

## Environment Variables

| Variable | Set by | Purpose |
|----------|--------|---------|
| `ANTHROPIC_API_KEY` | `harness.py` | Fake key (`test-key-not-real`) for the Anthropic SDK |
| `ANTHROPIC_BASE_URL` | `harness.py` | Redirects SDK calls to mock server (`http://127.0.0.1:18999`) |
| `CREEL_STATE_DIR` | `harness.py` | Isolates daemon state to test-local dir (avoids touching `~/.creel/`) |
| `HARNESS_DAEMON_SOCKET` | `harness.py` | Unix socket path, consumed by scenario `conftest.py` fixtures |
| `HARNESS_MOCK_LLM_URL` | `harness.py` | Mock server base URL, consumed by scenario `conftest.py` fixtures |

## Output

After a run, `test-results/` contains:

```
test-results/
├── report.json              # Summary: timestamp, duration, pass/fail counts
└── logs/
    ├── mock-llm.log         # Mock LLM server stdout/stderr
    ├── daemon.log           # Creel daemon stdout/stderr
    └── test-output.log      # pytest output
```

## Not Yet Implemented

- **Media pipeline tests** (`test_media.py`) — image/voice processing scenarios are specced but not built
- **Streaming integration tests** — mock server supports streaming but no scenario tests exercise the daemon's `/v1/messages/stream` endpoint
- **Error injection integration tests** — mock server supports error injection but no scenarios test daemon behavior on LLM 500/429 responses
- **Concurrent request tests** — sender isolation is tested serially, not under actual concurrency
