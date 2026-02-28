#!/bin/bash
# Creel Integration Test Harness — MOCK LLM entry point.
#
# Starts a mock Anthropic LLM server and a Creel daemon with test config,
# runs pytest integration scenarios, produces a test report, and cleans up.
# No real API key is needed — all LLM calls go to the mock server.
#
# For the full test suite (unit + integration + Playwright e2e) with a
# real LLM, use scripts/test-harness/harness.sh instead.
#
# Usage:
#   ./scripts/test-harness.sh              # Run all scenarios
#   ./scripts/test-harness.sh --help       # Show harness.py help
#
# Exit codes:
#   0 = all tests passed (or no scenarios to run)
#   1 = test failures or infrastructure error

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HARNESS_DIR="$SCRIPT_DIR/test-harness"

# Clean up on exit / signal
cleanup() {
    local harness_pid_file="$HARNESS_DIR/run/daemon.pid"
    local harness_socket="$HARNESS_DIR/run/daemon.sock"

    # Kill any lingering mock LLM or daemon processes started by harness.py
    # (harness.py handles its own cleanup, but belt-and-suspenders for signals)
    if [ -f "$harness_pid_file" ]; then
        local pid
        pid=$(cat "$harness_pid_file" 2>/dev/null || true)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
        rm -f "$harness_pid_file"
    fi
    rm -f "$harness_socket"

    # Kill mock LLM by port if still lingering
    lsof -ti:18999 2>/dev/null | xargs kill 2>/dev/null || true
}

trap cleanup EXIT INT TERM

echo "=== Creel Integration Test Harness ==="
echo ""

# Run the Python orchestrator which handles everything
cd "$REPO_ROOT"
exec uv run python "$HARNESS_DIR/harness.py" "$@"
