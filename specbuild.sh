#!/bin/bash
# Specbuild loop — runs Claude against a spec, one phase per iteration

PROMPT_FILE="specbuild-prompt.txt"
MAX_ITERATIONS="${1:-7}"
LOG_DIR="devlogs"
LOG_FILE="$LOG_DIR/specbuild-$(date +%Y%m%d-%H%M%S).log"

mkdir -p "$LOG_DIR"

prompt=$(<"$PROMPT_FILE")

# Allow ctrl-c to kill the whole thing
trap 'echo ""; echo "Interrupted."; exit 130' INT

log() {
  echo "$1" | tee -a "$LOG_FILE"
}

log "=== Specbuild started at $(date) ==="
log "Max iterations: $MAX_ITERATIONS"
log ""

for ((i=1; i<=MAX_ITERATIONS; i++)); do
  log "=== Phase $i of $MAX_ITERATIONS — started $(date +%H:%M:%S) ==="

  start_time=$SECONDS
  claude -p "$prompt" --dangerously-skip-permissions --output-format text 2>&1 | tee -a "$LOG_FILE"
  exit_code=${PIPESTATUS[0]}
  elapsed=$(( SECONDS - start_time ))
  mins=$(( elapsed / 60 ))
  secs=$(( elapsed % 60 ))

  log ""
  log "--- Phase $i finished in ${mins}m${secs}s (exit $exit_code) ---"

  if [[ $exit_code -ne 0 && $exit_code -ne 1 ]]; then
    log "Claude exited with $exit_code — stopping loop"
    exit $exit_code
  fi

  if grep -q '<promise>COMPLETE</promise>' "$LOG_FILE"; then
    log ""
    log "=== Spec complete! $(date) ==="
    exit 0
  fi

  log ""
done

log "=== Reached max iterations ($MAX_ITERATIONS) at $(date) ==="
exit 0
