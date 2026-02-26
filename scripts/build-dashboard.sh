#!/usr/bin/env bash
# Build the Creel dashboard frontend and copy output to the daemon's static dir.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DASHBOARD_DIR="$REPO_ROOT/dashboard"
STATIC_DIR="$REPO_ROOT/src/taskrunner/dashboard_static"

echo "==> Installing dependencies..."
cd "$DASHBOARD_DIR"
npm ci

echo "==> Building dashboard..."
npm run build

echo "==> Copying dist/ to $STATIC_DIR..."
rm -rf "$STATIC_DIR"
cp -r "$DASHBOARD_DIR/dist" "$STATIC_DIR"

echo "==> Done. Dashboard built at $STATIC_DIR"
