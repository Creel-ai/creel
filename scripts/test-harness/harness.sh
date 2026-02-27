#!/usr/bin/env bash
# Test harness orchestration script for Creel.
#
# Runs unit tests, integration scenario tests (via harness.py with mock LLM),
# and Playwright e2e tests (when dashboard is configured), then tears down.
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
DAEMON_LOG="$RESULTS_DIR/logs/e2e-daemon.log"

# ── Defaults ───────────────────────────────────────────────────────────────
SKIP_UNIT=false
SKIP_INTEGRATION=false
SKIP_E2E=false
KEEP_RUNNING=false
VERBOSE=false

# ── Counters ───────────────────────────────────────────────────────────────
# *_PASS: -1=not run (skipped internally), 0=failed, 1=passed
UNIT_COUNT=0
UNIT_PASS=-1
INTEGRATION_COUNT=0
INTEGRATION_PASS=-1
E2E_COUNT=0
E2E_PASS=-1
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
    echo "=== Integration Tests (Scenarios) ==="
    local harness_py="$SCRIPT_DIR/harness.py"

    if [[ ! -f "$harness_py" ]]; then
        echo "  Skipping: harness.py not found"
        return 0
    fi

    local scenarios_dir="$SCRIPT_DIR/scenarios"
    if [[ ! -d "$scenarios_dir" ]] || ! ls "$scenarios_dir"/test_*.py &>/dev/null; then
        echo "  Skipping: no scenario tests in $scenarios_dir"
        return 0
    fi

    # Use a sub-directory so harness.py logs don't clobber unit test logs
    local int_results_dir="$RESULTS_DIR/integration"
    mkdir -p "$int_results_dir/logs"
    local log_file="$RESULTS_DIR/logs/integration-tests.log"

    # harness.py manages its own mock LLM server and daemon lifecycle.
    # It starts a mock Anthropic API, points the daemon at it, then runs
    # pytest on the scenario files.
    if cd "$REPO_ROOT" && uv run python "$harness_py" \
           --results-dir "$int_results_dir" > "$log_file" 2>&1; then
        INTEGRATION_PASS=1
    else
        INTEGRATION_PASS=0
        OVERALL_EXIT=1
    fi

    # Parse counts from report.json generated by harness.py
    local report_file="$int_results_dir/report.json"
    local passed=0 failed=0 errors=0
    if [[ -f "$report_file" ]]; then
        passed=$(python3 -c "import json; r=json.load(open('$report_file')); print(r.get('passed',0))" 2>/dev/null || echo 0)
        failed=$(python3 -c "import json; r=json.load(open('$report_file')); print(r.get('failed',0))" 2>/dev/null || echo 0)
        errors=$(python3 -c "import json; r=json.load(open('$report_file')); print(r.get('errors',0))" 2>/dev/null || echo 0)
        INTEGRATION_COUNT=$((passed + failed + errors))
    else
        # Fallback: parse pytest output from log
        INTEGRATION_COUNT=$(grep -oE '[0-9]+ passed' "$log_file" | grep -oE '[0-9]+' || echo 0)
        passed=$INTEGRATION_COUNT
    fi

    if [[ "$INTEGRATION_PASS" -eq 1 ]]; then
        echo "  PASSED ($passed passed, $INTEGRATION_COUNT total)"
    else
        echo "  FAILED ($passed passed, $failed failed, $errors errors)"
        if [[ "$VERBOSE" == "true" ]]; then
            echo "--- integration test output (last 40 lines) ---"
            tail -40 "$log_file"
            echo "--- end ---"
        fi
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
        echo "  Skipping: no playwright.config.ts in dashboard/"
        echo "  (Needs dashboard source with @playwright/test configured)"
        return 0
    fi

    # Start the real daemon for e2e tests (integration tests use harness.py's mock daemon)
    start_daemon

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

    _print_phase() {
        local label="$1" pass="$2" count="$3" skipped_flag="$4"
        if [[ "$skipped_flag" == "true" ]]; then
            printf "  %-20s %s\n" "$label:" "SKIPPED"
        elif [[ "$pass" -eq -1 ]]; then
            printf "  %-20s %s\n" "$label:" "SKIPPED (not configured)"
        elif [[ "$pass" -eq 1 ]]; then
            printf "  %-20s %s (%s tests)\n" "$label:" "PASS" "$count"
        else
            printf "  %-20s %s (%s tests)\n" "$label:" "FAIL" "$count"
        fi
    }

    _print_phase "Unit tests"        "$UNIT_PASS"        "$UNIT_COUNT"        "$SKIP_UNIT"
    _print_phase "Integration tests" "$INTEGRATION_PASS" "$INTEGRATION_COUNT" "$SKIP_INTEGRATION"
    _print_phase "E2E tests"         "$E2E_PASS"         "$E2E_COUNT"         "$SKIP_E2E"

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

    run_unit_tests
    run_integration_tests
    run_e2e_tests  # starts daemon internally only if Playwright is configured

    print_summary
    exit "$OVERALL_EXIT"
}

main
