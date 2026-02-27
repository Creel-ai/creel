#!/usr/bin/env bash
# Test harness orchestration script for Creel.
#
# Sets up an isolated test environment, starts the daemon with real LLM calls,
# runs unit tests, API integration tests, and Playwright e2e tests, then tears
# down cleanly.
#
# Requires: ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN set in environment.
#
# Usage:
#   ./scripts/test-harness/harness.sh [OPTIONS]
#
# Options:
#   --skip-unit         Skip existing unit tests
#   --skip-integration  Skip API integration tests
#   --skip-e2e          Skip Playwright e2e tests
#   --keep-running      Leave daemon up after tests for debugging
#   --verbose           Show daemon log output on failure

set -euo pipefail

# ── Paths ──────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TEST_HOME="$HOME/.creel-test"
TEST_CONFIG_SRC="$SCRIPT_DIR/test-config"
RESULTS_DIR="$REPO_ROOT/test-results"

DAEMON_SOCKET="$TEST_HOME/daemon.sock"
DAEMON_PID_FILE="$TEST_HOME/daemon.pid"
DAEMON_LOG="$RESULTS_DIR/logs/daemon.log"

# ── Defaults ───────────────────────────────────────────────────────────────
SKIP_UNIT=false
SKIP_INTEGRATION=false
SKIP_E2E=false
KEEP_RUNNING=false
VERBOSE=false

# ── Counters ───────────────────────────────────────────────────────────────
UNIT_COUNT=0
UNIT_PASS=0
INTEGRATION_COUNT=0
INTEGRATION_PASS=0
E2E_COUNT=0
E2E_PASS=0
OVERALL_EXIT=0

# ── Parse args ─────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-unit)        SKIP_UNIT=true ;;
        --skip-integration) SKIP_INTEGRATION=true ;;
        --skip-e2e)         SKIP_E2E=true ;;
        --keep-running)     KEEP_RUNNING=true ;;
        --verbose)          VERBOSE=true ;;
        -h|--help)
            head -20 "$0" | grep '^#' | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
    shift
done

# ── Pre-flight checks ─────────────────────────────────────────────────────
if [[ -z "${ANTHROPIC_API_KEY:-}" && -z "${ANTHROPIC_AUTH_TOKEN:-}" ]]; then
    echo "ERROR: ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is not set. Required for real LLM calls." >&2
    exit 1
fi

if [[ ! -f "$TEST_CONFIG_SRC/agent.yml" ]]; then
    echo "ERROR: Test config not found at $TEST_CONFIG_SRC/agent.yml" >&2
    echo "  Run HARNESS-002 first to create the test configuration." >&2
    exit 1
fi

# ── Cleanup function ──────────────────────────────────────────────────────
cleanup() {
    local exit_code=$?
    echo ""
    echo "Cleaning up..."

    # Stop daemon if running
    if [[ -f "$DAEMON_PID_FILE" ]]; then
        local pid
        pid=$(cat "$DAEMON_PID_FILE" 2>/dev/null || true)
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            if [[ "$KEEP_RUNNING" == "true" ]]; then
                echo "  --keep-running: Daemon left running (PID $pid)"
                echo "  Socket: $DAEMON_SOCKET"
                echo "  Stop manually: kill $pid"
                return "$exit_code"
            fi
            echo "  Stopping daemon (PID $pid)..."
            kill "$pid" 2>/dev/null || true
            # Wait up to 5s for graceful shutdown
            for _ in $(seq 1 50); do
                if ! kill -0 "$pid" 2>/dev/null; then
                    break
                fi
                sleep 0.1
            done
            # Force kill if still running
            if kill -0 "$pid" 2>/dev/null; then
                echo "  SIGTERM timed out, sending SIGKILL..."
                kill -9 "$pid" 2>/dev/null || true
            fi
        fi
    fi

    # Remove daemon runtime files and test home
    rm -f "$DAEMON_SOCKET" "$DAEMON_PID_FILE"
    rm -rf "$TEST_HOME"

    echo "  Done."
    return "$exit_code"
}

trap cleanup EXIT INT TERM

# ── Setup test environment ─────────────────────────────────────────────────
setup_test_home() {
    echo "Setting up test environment at $TEST_HOME..."

    # Create test home and required subdirectories
    mkdir -p "$TEST_HOME"/{sessions,logs,tasks,workspace}
    mkdir -p "$RESULTS_DIR/logs"

    # Copy test agent config
    cp "$TEST_CONFIG_SRC/agent.yml" "$TEST_HOME/agent.yml"

    # Generate dashboard token (for future use)
    if [[ ! -f "$TEST_HOME/dashboard-token" ]]; then
        python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "$TEST_HOME/dashboard-token"
    fi

    echo "  Test home ready."
}

# ── Start daemon ───────────────────────────────────────────────────────────
start_daemon() {
    echo "Starting Creel daemon..."

    # Clean up stale socket/pid
    rm -f "$DAEMON_SOCKET" "$DAEMON_PID_FILE"

    # Start daemon in background, pointing at test config
    # CWD is set to test home so relative paths in agent.yml resolve there
    uv run python -m taskrunner \
        --agent-config "$TEST_HOME/agent.yml" \
        --tasks-dir "$TEST_HOME/tasks" \
        --no-judge \
        daemon run \
        --socket-path "$DAEMON_SOCKET" \
        --pid-file "$DAEMON_PID_FILE" \
        --channel none \
        --no-scheduler \
        > "$DAEMON_LOG" 2>&1 &

    local daemon_pid=$!
    echo "  Daemon started (PID $daemon_pid), waiting for health check..."

    # Wait for the daemon to become healthy (up to 20s)
    local deadline=$((SECONDS + 20))
    while [[ $SECONDS -lt $deadline ]]; do
        if [[ -S "$DAEMON_SOCKET" ]]; then
            # Try health check via Unix socket
            local health
            health=$(curl -s --unix-socket "$DAEMON_SOCKET" http://daemon/health 2>/dev/null || true)
            if echo "$health" | grep -q '"status":"ok"'; then
                echo "  Daemon healthy."
                return 0
            fi
        fi
        sleep 0.3
    done

    echo "ERROR: Daemon failed to become healthy within 20s." >&2
    if [[ -f "$DAEMON_LOG" ]]; then
        echo "--- daemon.log (last 50 lines) ---"
        tail -50 "$DAEMON_LOG"
        echo "--- end ---"
    fi
    return 1
}

# ── Run unit tests ─────────────────────────────────────────────────────────
run_unit_tests() {
    if [[ "$SKIP_UNIT" == "true" ]]; then
        echo "Skipping unit tests (--skip-unit)"
        return 0
    fi

    echo ""
    echo "=== Unit Tests ==="
    local log_file="$RESULTS_DIR/logs/unit-tests.log"

    if cd "$REPO_ROOT" && uv run pytest tests/ -x -q -m "not smoke" > "$log_file" 2>&1; then
        UNIT_PASS=1
        # Extract count from pytest output
        UNIT_COUNT=$(grep -oE '[0-9]+ passed' "$log_file" | grep -oE '[0-9]+' || echo 0)
        echo "  PASSED ($UNIT_COUNT tests)"
    else
        UNIT_PASS=0
        UNIT_COUNT=$(grep -oE '[0-9]+ (passed|failed|error)' "$log_file" | head -1 | grep -oE '[0-9]+' || echo 0)
        echo "  FAILED"
        tail -20 "$log_file"
        OVERALL_EXIT=1
    fi
}

# ── Run integration tests ─────────────────────────────────────────────────
run_integration_tests() {
    if [[ "$SKIP_INTEGRATION" == "true" ]]; then
        echo "Skipping integration tests (--skip-integration)"
        return 0
    fi

    echo ""
    echo "=== Integration Tests ==="
    local test_script="$SCRIPT_DIR/test-api.py"

    if [[ ! -f "$test_script" ]]; then
        echo "  Skipping: test-api.py not found (implement HARNESS-003)"
        return 0
    fi

    local log_file="$RESULTS_DIR/logs/integration-tests.log"

    if HARNESS_DAEMON_SOCKET="$DAEMON_SOCKET" \
       HARNESS_TEST_HOME="$TEST_HOME" \
       uv run python "$test_script" > "$log_file" 2>&1; then
        INTEGRATION_PASS=1
        INTEGRATION_COUNT=$(grep -c 'PASS' "$log_file" || echo 0)
        echo "  PASSED ($INTEGRATION_COUNT tests)"
    else
        INTEGRATION_PASS=0
        INTEGRATION_COUNT=$(grep -cE 'PASS|FAIL' "$log_file" || echo 0)
        echo "  FAILED"
        tail -20 "$log_file"
        OVERALL_EXIT=1
    fi
}

# ── Run e2e tests ──────────────────────────────────────────────────────────
run_e2e_tests() {
    if [[ "$SKIP_E2E" == "true" ]]; then
        echo "Skipping e2e tests (--skip-e2e)"
        return 0
    fi

    echo ""
    echo "=== E2E Tests (Playwright) ==="
    local dashboard_dir="$REPO_ROOT/dashboard"

    if [[ ! -f "$dashboard_dir/playwright.config.ts" ]]; then
        echo "  Skipping: Playwright not configured (implement HARNESS-004)"
        return 0
    fi

    local log_file="$RESULTS_DIR/logs/e2e-tests.log"

    if cd "$dashboard_dir" && \
       HARNESS_DAEMON_SOCKET="$DAEMON_SOCKET" \
       HARNESS_TEST_HOME="$TEST_HOME" \
       npx playwright test > "$log_file" 2>&1; then
        E2E_PASS=1
        E2E_COUNT=$(grep -oE '[0-9]+ passed' "$log_file" | grep -oE '[0-9]+' || echo 0)
        echo "  PASSED ($E2E_COUNT tests)"
    else
        E2E_PASS=0
        E2E_COUNT=$(grep -oE '[0-9]+ (passed|failed)' "$log_file" | head -1 | grep -oE '[0-9]+' || echo 0)
        echo "  FAILED"
        tail -20 "$log_file"
        OVERALL_EXIT=1
    fi
}

# ── Print summary ──────────────────────────────────────────────────────────
print_summary() {
    echo ""
    echo "═══════════════════════════════════════════════"
    echo "  Test Harness Summary"
    echo "═══════════════════════════════════════════════"

    if [[ "$SKIP_UNIT" != "true" ]]; then
        local unit_status="FAIL"
        [[ "$UNIT_PASS" -eq 1 ]] && unit_status="PASS"
        printf "  %-20s %s (%s tests)\n" "Unit tests:" "$unit_status" "$UNIT_COUNT"
    else
        printf "  %-20s %s\n" "Unit tests:" "SKIPPED"
    fi

    if [[ "$SKIP_INTEGRATION" != "true" ]]; then
        local int_status="PASS"
        [[ "$INTEGRATION_PASS" -eq 0 ]] && int_status="FAIL"
        printf "  %-20s %s (%s tests)\n" "Integration tests:" "$int_status" "$INTEGRATION_COUNT"
    else
        printf "  %-20s %s\n" "Integration tests:" "SKIPPED"
    fi

    if [[ "$SKIP_E2E" != "true" ]]; then
        local e2e_status="PASS"
        [[ "$E2E_PASS" -eq 0 ]] && e2e_status="FAIL"
        printf "  %-20s %s (%s tests)\n" "E2E tests:" "$e2e_status" "$E2E_COUNT"
    else
        printf "  %-20s %s\n" "E2E tests:" "SKIPPED"
    fi

    echo "═══════════════════════════════════════════════"

    if [[ "$OVERALL_EXIT" -eq 0 ]]; then
        echo "  Result: ALL PASSED"
    else
        echo "  Result: FAILURES DETECTED"
    fi
    echo "═══════════════════════════════════════════════"
    echo ""
    echo "  Logs: $RESULTS_DIR/logs/"
}

# ── Main ───────────────────────────────────────────────────────────────────
main() {
    echo "=== Creel E2E Test Harness ==="
    echo ""

    setup_test_home
    start_daemon

    run_unit_tests
    run_integration_tests
    run_e2e_tests

    print_summary
    exit "$OVERALL_EXIT"
}

main
