#!/usr/bin/env bash
# Regenerate pinned requirements.lock files for all executors.
#
# Usage:
#   ./scripts/update-executor-locks.sh
#
# Requires: uv (https://github.com/astral-sh/uv)

set -euo pipefail

EXECUTORS_DIR="src/executors"
UPDATED=0
FAILED=0

for req in "$EXECUTORS_DIR"/*/requirements.txt; do
    dir="$(dirname "$req")"
    name="$(basename "$dir")"
    lock="$dir/requirements.lock"

    echo "Compiling $name..."
    if uv pip compile "$req" -o "$lock" --quiet 2>/dev/null; then
        UPDATED=$((UPDATED + 1))
    else
        echo "  FAILED: $name" >&2
        FAILED=$((FAILED + 1))
    fi
done

echo ""
echo "Done: $UPDATED updated, $FAILED failed."
[ "$FAILED" -eq 0 ] || exit 1
