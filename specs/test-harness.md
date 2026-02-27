# Creel Integration Test Harness

## Problem

Creel has 108 test files and 1700+ unit tests, but they're almost entirely mocked. Nobody tests the real flow: message arrives → agent processes → tool executes → response sent. When Ralph builds a feature, it runs pytest, sees green, and commits — but the feature might not actually work end-to-end.

The integration test that exists (`test_integration.py`) mocks the LLM. The e2e tests for media mock the Telegram API and LLM. Nothing tests with a real (or realistic) daemon running.

## Goal

A test harness that agents (Ralph, Claude, any coding agent) can run with **one command** to verify Creel actually works. The harness:

1. Spins up a fully functional Creel daemon with a test config
2. Sends messages through channels and verifies responses
3. Tests tool execution, cron jobs, Guardian policies
4. Produces a pass/fail report with logs
5. Tears everything down cleanly

```bash
./scripts/test-harness.sh
# Exit code 0 = all good, 1 = failures
# Output: test-results/report.json + test-results/logs/
```

## Architecture

```
scripts/
├── test-harness.sh              # Entry point — orchestrates everything
├── test-harness/
│   ├── config/
│   │   ├── agent.yaml            # Test agent config (mock LLM provider)
│   │   ├── tools/                # Test tool definitions
│   │   └── policies/             # Test guardian policies
│   ├── fixtures/
│   │   ├── messages.json         # Test messages with expected responses
│   │   ├── tool_calls.json       # Expected tool call sequences
│   │   └── blocked_messages.json # Messages Guardian should block
│   ├── mock_llm_server.py        # FastAPI server mimicking OpenAI API
│   ├── scenarios/
│   │   ├── test_basic_chat.py    # Basic message → response
│   │   ├── test_tool_execution.py # Tool calls work end-to-end
│   │   ├── test_telegram.py      # Telegram webhook flow
│   │   ├── test_cron.py          # Cron job scheduling and execution
│   │   ├── test_guardian.py      # Guardian blocks dangerous requests
│   │   ├── test_sessions.py      # Session persistence and isolation
│   │   └── test_media.py         # Image/voice processing pipeline
│   └── harness.py                # Python test runner + report generator
```

## Components

### 1. Mock LLM Server (`mock_llm_server.py`)

A lightweight FastAPI server that mimics the OpenAI chat completions API. This is the key piece — it lets us test the full pipeline without burning tokens or depending on external APIs.

```python
# Runs on localhost:18999
# Handles POST /v1/chat/completions
# Returns deterministic responses based on message content
```

**Behavior modes:**
- **Echo mode**: Returns the user's message back (for basic flow testing)
- **Scripted mode**: Returns pre-configured responses for specific inputs (from fixtures)
- **Tool call mode**: Returns tool_calls responses to trigger tool execution, then responds to tool results
- **Streaming mode**: Returns SSE chunks for streaming tests
- **Error mode**: Returns 500/429/timeout for error handling tests

**Scripted responses example:**
```json
{
  "triggers": [
    {
      "match": "what time is it",
      "response": {"type": "tool_call", "tool": "exec", "args": {"command": "date"}},
      "followup": "The current time is {tool_result}"
    },
    {
      "match": "hello",
      "response": {"type": "text", "content": "Hello! I'm the test agent."}
    },
    {
      "match": ".*",
      "response": {"type": "text", "content": "Echo: {user_message}"}
    }
  ]
}
```

### 2. Test Agent Config (`config/agent.yaml`)

A minimal agent config that points at the mock LLM server:

```yaml
name: test-agent
llm:
  provider: openai
  model: gpt-4
  api_key: test-key-not-real
  base_url: http://localhost:18999/v1  # Points at mock server

tools:
  - name: exec
    executor: host  # Direct execution, no Docker needed for tests
    description: "Run shell commands"
    
guardian:
  enabled: true
  policies:
    - policies/test-policy.yaml

channels:
  telegram:
    enabled: true
    bot_token: "000000:fake-token"
    mode: webhook
    webhook_path: /webhooks/telegram
    allowed_senders: ["test-user-123"]

media:
  enabled: true
  transcription:
    backend: openai
    api_key: test-key
  vision:
    max_pixels: 512
```

### 3. Test Scenarios

#### Scenario 1: Basic Chat (`test_basic_chat.py`)
Tests the fundamental message → response pipeline.

```
GIVEN the daemon is running with test config
WHEN I send a POST to /v1/chat with {"sender": "test-user", "text": "hello"}
THEN I receive a 200 response with the agent's reply
AND the response contains text from the mock LLM
AND a session was created for the sender
AND the message appears in session history
```

**Test cases:**
- [ ] Simple text message gets a response
- [ ] Multiple messages maintain conversation context (session)
- [ ] `/clear` command resets the session
- [ ] `/new` command starts a new session
- [ ] Empty message is handled gracefully
- [ ] Very long message (10k chars) is handled
- [ ] Concurrent messages from different senders are isolated

#### Scenario 2: Tool Execution (`test_tool_execution.py`)
Tests that the agent can call tools and process results.

```
GIVEN the mock LLM returns a tool_call for "exec" with {"command": "echo hello"}
WHEN I send a message that triggers this tool call
THEN the exec tool runs "echo hello"
AND the tool output "hello" is sent back to the LLM
AND the final response includes the tool result
```

**Test cases:**
- [ ] Simple exec tool call executes and returns output
- [ ] Tool output is passed back to LLM for final response
- [ ] Tool execution timeout is enforced
- [ ] Tool with non-zero exit code reports error
- [ ] Multiple sequential tool calls work
- [ ] Guardian-blocked tool call is rejected (see Scenario 5)
- [ ] Unknown tool name returns error

#### Scenario 3: Telegram Channel (`test_telegram.py`)
Tests the Telegram webhook flow end-to-end.

```
GIVEN the daemon is running with Telegram webhook enabled
WHEN I POST a Telegram-formatted update to /webhooks/telegram
THEN the message is processed by the agent
AND a response is sent back via the Telegram sendMessage API (mocked)
```

**Test cases:**
- [ ] Text message via webhook gets processed and responded to
- [ ] Photo message: downloaded, saved to media store, passed to LLM with image content block
- [ ] Voice message: downloaded, transcribed (mock Whisper), text sent to LLM
- [ ] Message from unknown sender is rejected (not in allowed_senders)
- [ ] Webhook verification (GET with challenge) works
- [ ] Malformed update body returns 400

#### Scenario 4: Cron Jobs (`test_cron.py`)
Tests cron job lifecycle.

```
GIVEN the daemon is running
WHEN I create a cron job via POST /v1/cron/jobs
AND the schedule triggers (or I manually trigger via POST /v1/cron/jobs/{id}/run)
THEN the job executes with the mock LLM
AND run history is recorded
AND delivery is attempted
```

**Test cases:**
- [ ] Create a cron job via API
- [ ] List cron jobs includes the new job
- [ ] Manual trigger (run) executes the job
- [ ] Run history records success/failure/duration
- [ ] Update a cron job (change schedule, prompt)
- [ ] Delete a cron job
- [ ] Disabled job doesn't execute
- [ ] Job with invalid cron expression is rejected
- [ ] One-shot job executes once then disables itself

#### Scenario 5: Guardian (`test_guardian.py`)
Tests that dangerous requests are blocked.

```
GIVEN the daemon is running with Guardian enabled
WHEN I send a message that triggers a dangerous tool call (e.g., "rm -rf /")
THEN the Guardian blocks the tool execution
AND the response indicates the action was blocked
AND the block is logged in the audit log
```

**Test cases:**
- [ ] Exec tool with `rm -rf` is blocked
- [ ] Exec tool with `curl | bash` is blocked
- [ ] Allowed command (e.g., `echo hello`) passes through
- [ ] Prompt injection attempt in tool arguments is detected
- [ ] Block event is recorded in audit log with reason
- [ ] Guardian disabled = everything passes through

#### Scenario 6: Sessions (`test_sessions.py`)
Tests session persistence and isolation.

```
GIVEN two different senders are chatting
WHEN sender A says "my name is Alice"
AND sender B says "my name is Bob"  
THEN sender A's session only contains Alice's messages
AND sender B's session only contains Bob's messages
AND sessions persist across daemon restart
```

**Test cases:**
- [ ] Messages from different senders go to different sessions
- [ ] Session history is retrievable via API
- [ ] Session context is maintained across multiple messages
- [ ] Session clear removes history
- [ ] Session list shows all active sessions

#### Scenario 7: Media Pipeline (`test_media.py`)
Tests image and voice processing end-to-end (with mock external APIs).

```
GIVEN media processing is enabled
WHEN an image attachment arrives
THEN it's saved to the media store
AND resized if > max_pixels
AND converted to base64 content block
AND included in the LLM message
```

**Test cases:**
- [ ] JPEG image saved, resized, encoded, sent to LLM as content block
- [ ] PNG image same flow
- [ ] Voice message (.ogg) saved, transcribed (mock API), text prepended to message
- [ ] Large image is resized to max_pixels
- [ ] Corrupt image file handled gracefully (warning log, text-only message sent)
- [ ] Media store files organized by channel/date/uuid
- [ ] Media store cleanup removes files older than retention_days

### 4. Test Runner (`harness.py`)

Python script that orchestrates everything:

```python
class TestHarness:
    def setup(self):
        """Start mock LLM server, start daemon with test config."""
        
    def teardown(self):
        """Stop daemon, stop mock LLM, clean up test data."""
        
    def run_scenarios(self):
        """Run all scenario test files, collect results."""
        
    def generate_report(self):
        """Write test-results/report.json with pass/fail/timing."""
```

**Report format:**
```json
{
  "timestamp": "2026-02-26T17:30:00Z",
  "duration_seconds": 45,
  "total": 42,
  "passed": 40,
  "failed": 2,
  "scenarios": [
    {
      "name": "basic_chat",
      "tests": 7,
      "passed": 7,
      "duration_ms": 1200
    },
    {
      "name": "tool_execution",
      "tests": 7,
      "passed": 5,
      "failed": 2,
      "failures": [
        {"test": "test_tool_timeout", "error": "Timeout not enforced — tool ran for 45s"}
      ]
    }
  ]
}
```

### 5. Entry Script (`test-harness.sh`)

```bash
#!/bin/bash
set -e

HARNESS_DIR="$(cd "$(dirname "$0")/test-harness" && pwd)"
REPO_ROOT="$(cd "$HARNESS_DIR/../.." && pwd)"
RESULTS_DIR="$REPO_ROOT/test-results"

mkdir -p "$RESULTS_DIR/logs"

echo "=== Creel Integration Test Harness ==="

# 1. Start mock LLM server
echo "Starting mock LLM server..."
python "$HARNESS_DIR/mock_llm_server.py" &
MOCK_PID=$!
sleep 2

# 2. Start daemon with test config
echo "Starting Creel daemon..."
CREEL_HOME="$HARNESS_DIR/config" creel daemon run &
DAEMON_PID=$!
sleep 3

# 3. Run scenarios
echo "Running test scenarios..."
cd "$REPO_ROOT"
uv run pytest "$HARNESS_DIR/scenarios/" \
  --tb=short \
  -q \
  --json-report --json-report-file="$RESULTS_DIR/report.json" \
  2>&1 | tee "$RESULTS_DIR/logs/test-output.log"

EXIT_CODE=$?

# 4. Cleanup
echo "Cleaning up..."
kill $DAEMON_PID 2>/dev/null || true
kill $MOCK_PID 2>/dev/null || true
rm -rf "$HARNESS_DIR/config/media" "$HARNESS_DIR/config/sessions" 2>/dev/null || true

echo ""
if [ $EXIT_CODE -eq 0 ]; then
  echo "✅ All integration tests passed!"
else
  echo "❌ Some tests failed. See $RESULTS_DIR/report.json"
fi

exit $EXIT_CODE
```

## Agent Usage

In Ralph's CLAUDE.md or any agent instructions:

```markdown
## Integration Testing

After implementing a feature that changes message flow, channels, tools, 
cron, Guardian, or media processing, run the integration test harness:

    ./scripts/test-harness.sh

This starts a real daemon with a mock LLM and tests the full pipeline.
If any scenarios fail, fix them before committing.

The harness is NOT a substitute for unit tests — run both:
    uv run pytest tests/ -x -q          # Unit tests
    ./scripts/test-harness.sh           # Integration tests
```

## Implementation Order

1. **Mock LLM server** — the foundation; without this nothing else works
2. **Test config + harness.py runner** — orchestration
3. **Scenario 1: Basic chat** — proves the pipeline works
4. **Scenario 2: Tool execution** — proves tools work
5. **test-harness.sh** — entry script
6. **Scenario 3: Telegram** — channel integration
7. **Scenario 4: Cron** — scheduling
8. **Scenario 5: Guardian** — security
9. **Scenario 6: Sessions** — persistence
10. **Scenario 7: Media** — image/voice pipeline

## Open Questions

- **Docker tools in CI?** The test config uses `executor: host` to avoid Docker dependency. If we want to test container execution, we need Docker available. Maybe a `--with-docker` flag?
- **Real LLM test?** A separate `--live` flag that uses a real API key for a small number of smoke tests? Expensive but catches API compatibility issues.
- **Pytest plugin vs standalone?** Could integrate with pytest as a plugin (`pytest --integration`) instead of a separate script. More discoverable but more complex.
